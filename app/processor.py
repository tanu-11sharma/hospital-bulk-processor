"""Bulk processing workflow.

    validate CSV -> new batch UUID -> POST /hospitals/ per row (concurrently,
    bounded, with retries) -> if every row succeeded: PATCH .../activate

Failure semantics
-----------------
* A row that still fails after retries is marked ``failed``; the batch is
  **not** activated, so no partial batch ever goes live. The job ends in
  ``partially_failed`` and can be resumed.
* If every row is created but activation fails, the job ends in
  ``activation_failed``; resuming only re-tries activation.
* Resume first reconciles against ``GET /hospitals/batch/{id}``: a create
  that timed out on our side may still have succeeded upstream, so we adopt
  those records instead of creating duplicates.
"""
import asyncio
import logging
import uuid
from typing import Dict, List, Tuple

from .config import Settings
from .models import HospitalInput, JobStatus, RowStatus
from .store import Job, JobStore
from .upstream import HospitalDirectoryClient, UpstreamError

log = logging.getLogger(__name__)


class BulkProcessor:
    def __init__(self, client: HospitalDirectoryClient, store: JobStore, settings: Settings):
        self._client = client
        self._store = store
        # Shared across all batches: caps total in-flight upstream calls for the
        # whole process, not just per upload.
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._background: set = set()

    # ---- entry points ----------------------------------------------------

    def create_job(self, hospitals: List[HospitalInput]) -> Job:
        job = Job(batch_id=str(uuid.uuid4()), hospitals=hospitals)
        self._store.add(job)
        return job

    async def run(self, job: Job, resume: bool = False) -> Job:
        async with job.lock:
            job.start_run()
            try:
                if resume:
                    await self._reconcile(job)
                await self._create_rows(job)
                if job.rows_with(RowStatus.FAILED, RowStatus.PENDING):
                    job.finish_run(JobStatus.PARTIALLY_FAILED)
                else:
                    await self._activate(job)
            except Exception:  # never leave a job stuck in "processing"
                log.exception("Unexpected error processing batch %s", job.batch_id)
                for r in job.rows_with(RowStatus.PENDING):
                    job.update_row(r.row, status=RowStatus.FAILED, error="Internal error")
                job.finish_run(JobStatus.PARTIALLY_FAILED)
        return job

    def run_in_background(self, job: Job, resume: bool = False) -> None:
        # Mark non-terminal synchronously so a second resume request arriving
        # before the task starts is rejected rather than double-scheduled.
        job.set_status(JobStatus.QUEUED)
        task = asyncio.create_task(self.run(job, resume=resume))
        self._background.add(task)  # keep a strong ref so the task isn't GC'd
        task.add_done_callback(self._background.discard)

    async def rollback(self, job: Job) -> Job:
        async with job.lock:
            await self._client.delete_batch(job.batch_id)
            job.batch_activated = False
            job.set_status(JobStatus.ROLLED_BACK)
        return job

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    # ---- steps -----------------------------------------------------------

    async def _create_rows(self, job: Job) -> None:
        todo = job.rows_with(RowStatus.PENDING, RowStatus.FAILED)
        await asyncio.gather(*(self._create_one(job, job.inputs[r.row]) for r in todo))

    async def _create_one(self, job: Job, h: HospitalInput) -> None:
        prior_attempts = job.results[h.row].attempts
        job.update_row(h.row, status=RowStatus.PENDING, error=None)
        async with self._semaphore:
            try:
                res = await self._client.create_hospital(h.name, h.address, h.phone, job.batch_id)
            except UpstreamError as exc:
                job.update_row(
                    h.row,
                    status=RowStatus.FAILED,
                    error=str(exc),
                    attempts=prior_attempts + exc.attempts,
                )
                return
        hospital_id = (res.data or {}).get("id") if isinstance(res.data, dict) else None
        job.update_row(
            h.row,
            status=RowStatus.CREATED,
            hospital_id=hospital_id,
            attempts=prior_attempts + res.attempts,
        )

    async def _activate(self, job: Job) -> None:
        job.set_status(JobStatus.ACTIVATING)
        try:
            await self._client.activate_batch(job.batch_id)
        except UpstreamError as exc:
            job.activation_error = str(exc)
            job.finish_run(JobStatus.ACTIVATION_FAILED)
            return
        job.activation_error = None
        job.batch_activated = True
        for r in job.rows_with(RowStatus.CREATED):
            job.update_row(r.row, status=RowStatus.CREATED_AND_ACTIVATED)
        job.finish_run(JobStatus.COMPLETED)

    async def _reconcile(self, job: Job) -> None:
        """Adopt hospitals that exist upstream but we recorded as failed."""
        unresolved = job.rows_with(RowStatus.FAILED, RowStatus.PENDING)
        if not unresolved:
            return
        try:
            existing = await self._client.list_batch(job.batch_id)
        except UpstreamError as exc:
            log.warning("Reconcile skipped for %s: %s", job.batch_id, exc)
            return

        known_ids = {r.hospital_id for r in job.results.values() if r.hospital_id is not None}
        by_key: Dict[Tuple[str, str], List[dict]] = {}
        for h in existing:
            if h.get("id") in known_ids:
                continue
            key = (str(h.get("name", "")).lower(), str(h.get("address", "")).lower())
            by_key.setdefault(key, []).append(h)

        for r in unresolved:
            inp = job.inputs[r.row]
            matches = by_key.get((inp.name.lower(), inp.address.lower()))
            if matches:
                found = matches.pop(0)
                job.update_row(
                    r.row, status=RowStatus.CREATED, hospital_id=found.get("id"), error=None
                )
