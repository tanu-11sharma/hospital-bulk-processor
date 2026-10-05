"""HTTP/WebSocket API for the Hospital Bulk Processing System."""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Union

import httpx
from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__
from .config import Settings, get_settings
from .csv_parser import CsvFormatError, parse_csv
from .models import (
    RESUMABLE_STATUSES,
    BulkResult,
    ErrorResponse,
    JobAccepted,
    JobStatus,
    ValidationReport,
)
from .processor import BulkProcessor
from .store import Job, JobStore
from .upstream import HospitalDirectoryClient, UpstreamError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hospital_bulk")

ACCEPTED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "text/plain",
    "application/vnd.ms-excel",
    "application/octet-stream",
}


def create_app(
    settings: Optional[Settings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    warm_upstream: bool = True,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = HospitalDirectoryClient(settings, transport=transport)
        store = JobStore(max_jobs=settings.max_stored_jobs)
        app.state.settings = settings
        app.state.client = client
        app.state.store = store
        app.state.processor = BulkProcessor(client, store, settings)
        warmup = None
        if warm_upstream:
            # The upstream sleeps on Render's free tier; start waking it now so
            # the first upload doesn't pay the full cold-start penalty.
            warmup = asyncio.create_task(client.health())
        yield
        if warmup:
            warmup.cancel()
        await app.state.processor.shutdown()
        await client.aclose()

    app = FastAPI(
        title="Hospital Bulk Processing API",
        version=__version__,
        description=(
            "Bulk-creates hospitals from a CSV upload via the Hospital Directory API, "
            "then activates the batch. Supports sync and async processing, progress "
            "polling, WebSocket progress streaming, resume and rollback."
        ),
        lifespan=lifespan,
    )

    # ---- helpers ---------------------------------------------------------

    def _processor(request_or_ws) -> BulkProcessor:
        return request_or_ws.app.state.processor

    def _job_or_404(request_or_ws, batch_id: str) -> Job:
        job = request_or_ws.app.state.store.get(batch_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Batch {batch_id} not found")
        return job

    async def _read_upload(file: UploadFile) -> bytes:
        name = (file.filename or "").lower()
        ctype = (file.content_type or "").split(";")[0].strip().lower()
        if not name.endswith(".csv") and ctype not in ACCEPTED_CONTENT_TYPES:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload must be a .csv file"
            )
        raw = await file.read(settings.max_upload_bytes + 1)
        if len(raw) > settings.max_upload_bytes:
            raise HTTPException(
                413,
                f"File exceeds {settings.max_upload_bytes} bytes",
            )
        return raw

    def _parse(raw: bytes) -> ValidationReport:
        try:
            return parse_csv(raw, max_rows=settings.max_csv_rows)
        except CsvFormatError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    def _accepted(request: Request, job: Job) -> JSONResponse:
        base = str(request.base_url).rstrip("/")
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        body = JobAccepted(
            batch_id=job.batch_id,
            status=job.status,
            total_hospitals=len(job.results),
            status_url=f"{base}/hospitals/bulk/{job.batch_id}",
            websocket_url=f"{ws_base}/hospitals/bulk/{job.batch_id}/ws",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body.model_dump(mode="json"))

    # ---- routes ----------------------------------------------------------

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root():
        return RedirectResponse(url="/docs")

    @app.api_route("/health", methods=["GET", "HEAD"], tags=["meta"])
    async def health(request: Request, check_upstream: bool = False):
        body = {"status": "ok", "version": __version__}
        if check_upstream:
            body["upstream_reachable"] = await request.app.state.client.health()
        return body

    @app.post(
        "/hospitals/bulk",
        response_model=BulkResult,
        tags=["bulk"],
        responses={
            202: {"model": JobAccepted, "description": "Accepted (async_mode=true)"},
            400: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            415: {"model": ErrorResponse},
            422: {"model": ValidationReport, "description": "CSV rows failed validation"},
        },
    )
    async def bulk_create(
        request: Request,
        file: UploadFile = File(..., description="CSV: name,address,phone (phone optional)"),
        async_mode: bool = Query(
            False,
            description="Return 202 immediately and process in the background; "
            "track via the status URL or WebSocket.",
        ),
    ):
        """Bulk-create hospitals from a CSV, then activate the batch.

        The whole file is validated first; if any row is invalid nothing is sent
        upstream and a 422 lists every problem.
        """
        report = _parse(await _read_upload(file))
        if not report.valid:
            return JSONResponse(
                status_code=422,
                content=report.model_dump(mode="json"),
            )

        processor = _processor(request)
        job = processor.create_job(report.hospitals)
        log.info("Batch %s: %d hospitals (async=%s)", job.batch_id, len(report.hospitals), async_mode)

        if async_mode:
            processor.run_in_background(job)
            return _accepted(request, job)

        await processor.run(job)
        return job.snapshot()

    @app.post("/hospitals/bulk/validate", response_model=ValidationReport, tags=["bulk"])
    async def validate_csv(file: UploadFile = File(...)):
        """Validate a CSV without creating anything. Always 200; check ``valid``."""
        return _parse(await _read_upload(file))

    @app.get("/hospitals/bulk", response_model=List[BulkResult], tags=["bulk"])
    async def list_batches(request: Request, limit: int = Query(20, ge=1, le=200)):
        """Most recent bulk jobs first."""
        jobs = request.app.state.store.all()[-limit:]
        return [j.snapshot() for j in reversed(jobs)]

    @app.get("/hospitals/bulk/{batch_id}", response_model=BulkResult, tags=["bulk"])
    async def get_batch(request: Request, batch_id: str):
        """Progress and results for a batch (polling endpoint)."""
        return _job_or_404(request, batch_id).snapshot()

    @app.post(
        "/hospitals/bulk/{batch_id}/resume",
        response_model=BulkResult,
        tags=["bulk"],
        responses={202: {"model": JobAccepted}, 409: {"model": ErrorResponse}},
    )
    async def resume_batch(
        request: Request, batch_id: str, async_mode: bool = Query(False)
    ) -> Union[BulkResult, JSONResponse]:
        """Retry failed rows (and/or activation) of a batch, reusing its batch ID."""
        job = _job_or_404(request, batch_id)
        if job.lock.locked():
            raise HTTPException(status.HTTP_409_CONFLICT, "Batch is currently processing")
        if job.status not in RESUMABLE_STATUSES:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Batch is '{job.status.value}' and cannot be resumed"
            )
        processor = _processor(request)
        if async_mode:
            processor.run_in_background(job, resume=True)
            return _accepted(request, job)
        await processor.run(job, resume=True)
        return job.snapshot()

    @app.delete(
        "/hospitals/bulk/{batch_id}",
        response_model=BulkResult,
        tags=["bulk"],
        responses={409: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    )
    async def rollback_batch(request: Request, batch_id: str):
        """Roll back a batch: delete every hospital it created upstream."""
        job = _job_or_404(request, batch_id)
        if job.lock.locked():
            raise HTTPException(status.HTTP_409_CONFLICT, "Batch is currently processing")
        try:
            await _processor(request).rollback(job)
        except UpstreamError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Rollback failed: {exc}")
        return job.snapshot()

    @app.websocket("/hospitals/bulk/{batch_id}/ws")
    async def batch_progress_ws(websocket: WebSocket, batch_id: str):
        """Streams a BulkResult snapshot on every change until the batch finishes."""
        job = websocket.app.state.store.get(batch_id)
        await websocket.accept()
        if job is None:
            await websocket.send_json({"error": f"Batch {batch_id} not found"})
            await websocket.close(code=4404)
            return
        try:
            while True:
                version = job.version
                await websocket.send_json(job.snapshot().model_dump(mode="json"))
                if job.is_terminal:
                    break
                await job.wait_for_change(version, timeout=15)
            await websocket.close()
        except WebSocketDisconnect:
            pass

    return app


app = create_app()
