"""Domain and API models."""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class RowStatus(str, Enum):
    PENDING = "pending"
    CREATED = "created"  # created upstream, batch not activated yet
    CREATED_AND_ACTIVATED = "created_and_activated"
    FAILED = "failed"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    ACTIVATING = "activating"
    COMPLETED = "completed"  # every row created and the batch activated
    PARTIALLY_FAILED = "partially_failed"  # some rows failed -> batch NOT activated; resumable
    ACTIVATION_FAILED = "activation_failed"  # all rows created but activate call failed; resumable
    ROLLED_BACK = "rolled_back"  # batch deleted upstream


TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.PARTIALLY_FAILED,
    JobStatus.ACTIVATION_FAILED,
    JobStatus.ROLLED_BACK,
}
RESUMABLE_STATUSES = {JobStatus.PARTIALLY_FAILED, JobStatus.ACTIVATION_FAILED}


class HospitalInput(BaseModel):
    row: int
    name: str
    address: str
    phone: Optional[str] = None


class HospitalResult(BaseModel):
    row: int
    hospital_id: Optional[int] = None
    name: str
    status: RowStatus
    attempts: int = 0
    error: Optional[str] = None


class BulkResult(BaseModel):
    """Shape of POST /hospitals/bulk (matches the spec, plus a few extra fields)."""

    batch_id: str
    status: JobStatus
    total_hospitals: int
    processed_hospitals: int = Field(description="Rows successfully created upstream")
    failed_hospitals: int
    processing_time_seconds: float
    batch_activated: bool
    activation_error: Optional[str] = None
    hospitals: List[HospitalResult]


class JobAccepted(BaseModel):
    batch_id: str
    status: JobStatus
    total_hospitals: int
    status_url: str
    websocket_url: str


class CsvRowError(BaseModel):
    row: int
    field: Optional[str] = None
    message: str


class ValidationReport(BaseModel):
    valid: bool
    total_rows: int
    errors: List[CsvRowError]
    hospitals: List[HospitalInput]


class ErrorResponse(BaseModel):
    detail: str
