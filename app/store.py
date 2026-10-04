"""In-memory job store.

Each bulk upload is a ``Job`` keyed by its batch ID. Jobs publish a change
notification on every update so the WebSocket endpoint can push progress
without polling. Swapping this for Redis/Postgres only means reimplementing
``JobStore``; the processor and API talk to it through a narrow interface.
"""
import asyncio
import time
from collections import OrderedDict
from typing import Dict, List, Optional

from .models import (
    TERMINAL_STATUSES,
    BulkResult,
    HospitalInput,
    HospitalResult,
    JobStatus,
    RowStatus,
)


class Job:
    def __init__(self, batch_id: str, hospitals: List[HospitalInput]):
        self.batch_id = batch_id
        self.inputs: Dict[int, HospitalInput] = {h.row: h for h in hospitals}
        self.results: Dict[int, HospitalResult] = {
            h.row: HospitalResult(row=h.row, name=h.name, status=RowStatus.PENDING)
            for h in hospitals
        }
        self.status = JobStatus.QUEUED
        self.batch_activated = False
        self.activation_error: Optional[str] = None
        self.created_at = time.time()
        self.lock = asyncio.Lock()  # one run (initial or resume) at a time
        self._elapsed = 0.0
        self._run_started: Optional[float] = None
        self._changed = asyncio.Event()
        self.version = 0

    # ---- lifecycle -------------------------------------------------------

    def start_run(self) -> None:
        self._run_started = time.perf_counter()
        self.set_status(JobStatus.PROCESSING)

    def finish_run(self, status: JobStatus) -> None:
        if self._run_started is not None:
            self._elapsed += time.perf_counter() - self._run_started
            self._run_started = None
        self.set_status(status)

    def set_status(self, status: JobStatus) -> None:
        self.status = status
        self.touch()

    def update_row(self, row: int, **changes) -> None:
        current = self.results[row]
        self.results[row] = current.model_copy(update=changes)
        self.touch()

    def touch(self) -> None:
        """Wake everyone waiting for a change, then arm a fresh event."""
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_for_change(self, since_version: int, timeout: float) -> None:
        if self.version != since_version:
            return
        event = self._changed
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    # ---- views -----------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def processing_time_seconds(self) -> float:
        running = time.perf_counter() - self._run_started if self._run_started else 0.0
        return round(self._elapsed + running, 3)

    def rows_with(self, *statuses: RowStatus) -> List[HospitalResult]:
        return [r for r in self.results.values() if r.status in statuses]

    def snapshot(self) -> BulkResult:
        rows = sorted(self.results.values(), key=lambda r: r.row)
        created = sum(
            r.status in (RowStatus.CREATED, RowStatus.CREATED_AND_ACTIVATED) for r in rows
        )
        failed = sum(r.status == RowStatus.FAILED for r in rows)
        return BulkResult(
            batch_id=self.batch_id,
            status=self.status,
            total_hospitals=len(rows),
            processed_hospitals=created,
            failed_hospitals=failed,
            processing_time_seconds=self.processing_time_seconds,
            batch_activated=self.batch_activated,
            activation_error=self.activation_error,
            hospitals=rows,
        )


class JobStore:
    def __init__(self, max_jobs: int = 1000):
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._max_jobs = max_jobs

    def add(self, job: Job) -> None:
        self._jobs[job.batch_id] = job
        self._evict()

    def get(self, batch_id: str) -> Optional[Job]:
        return self._jobs.get(batch_id)

    def all(self) -> List[Job]:
        return list(self._jobs.values())

    def _evict(self) -> None:
        # Drop the oldest *finished* jobs first; never evict a running job.
        while len(self._jobs) > self._max_jobs:
            victim = next((k for k, j in self._jobs.items() if j.is_terminal), None)
            if victim is None:
                break
            del self._jobs[victim]
