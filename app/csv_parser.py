"""CSV parsing and validation.

Accepted format: ``name,address,phone`` where ``phone`` is optional.
A header row is optional and detected automatically. Row numbers in results
are 1-based data-row numbers (the header is not counted), so they line up with
the ``row`` field in the processing response.
"""
import csv
import io
import re
from typing import List

from .models import CsvRowError, HospitalInput, ValidationReport

EXPECTED_HEADER = ["name", "address", "phone"]
MAX_NAME_LEN = 255
MAX_ADDRESS_LEN = 500
MAX_PHONE_LEN = 30
PHONE_RE = re.compile(r"^[0-9+()\-.\s/xX]{3,}$")


class CsvFormatError(ValueError):
    """The upload is not a readable CSV at all (as opposed to per-row errors)."""


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvFormatError("File must be UTF-8 encoded text") from exc


def _is_header(cells: List[str]) -> bool:
    lowered = [c.strip().lower() for c in cells]
    return lowered[:2] == EXPECTED_HEADER[:2]


def parse_csv(raw: bytes, max_rows: int) -> ValidationReport:
    """Parse and validate a CSV upload.

    Never raises for per-row problems; they are collected in the report so the
    caller can show every issue at once. Raises ``CsvFormatError`` only when the
    file is unusable as a whole (empty, not text, too many rows).
    """
    text = _decode(raw)
    if not text.strip():
        raise CsvFormatError("CSV file is empty")

    try:
        records = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise CsvFormatError(f"Malformed CSV: {exc}") from exc

    if records and _is_header(records[0]):
        header = [c.strip().lower() for c in records[0]]
        if header not in (EXPECTED_HEADER, EXPECTED_HEADER[:2]):
            raise CsvFormatError(
                "Unexpected header; expected 'name,address,phone' (phone optional)"
            )
        records = records[1:]

    if not records:
        raise CsvFormatError("CSV contains no hospital rows")
    if len(records) > max_rows:
        raise CsvFormatError(
            f"CSV has {len(records)} hospitals; the maximum per upload is {max_rows}"
        )

    hospitals: List[HospitalInput] = []
    errors: List[CsvRowError] = []
    seen: dict = {}

    for idx, cells in enumerate(records, start=1):
        row_errors = _validate_row(idx, cells)
        if row_errors:
            errors.extend(row_errors)
            continue

        name, address = cells[0].strip(), cells[1].strip()
        phone = cells[2].strip() if len(cells) > 2 and cells[2].strip() else None

        key = (name.lower(), address.lower())
        if key in seen:
            errors.append(
                CsvRowError(
                    row=idx,
                    message=f"Duplicate of row {seen[key]} (same name and address)",
                )
            )
            continue
        seen[key] = idx
        hospitals.append(HospitalInput(row=idx, name=name, address=address, phone=phone))

    return ValidationReport(
        valid=not errors, total_rows=len(records), errors=errors, hospitals=hospitals
    )


def _validate_row(idx: int, cells: List[str]) -> List[CsvRowError]:
    errs: List[CsvRowError] = []
    if len(cells) < 2:
        return [CsvRowError(row=idx, message="Expected at least 2 columns: name,address")]
    if len(cells) > 3:
        return [
            CsvRowError(
                row=idx,
                message=f"Expected at most 3 columns (name,address,phone), got {len(cells)}",
            )
        ]

    name, address = cells[0].strip(), cells[1].strip()
    phone = cells[2].strip() if len(cells) > 2 else ""

    for field_name, value, limit in (
        ("name", name, MAX_NAME_LEN),
        ("address", address, MAX_ADDRESS_LEN),
    ):
        if not value:
            errs.append(CsvRowError(row=idx, field=field_name, message=f"{field_name} is required"))
        elif len(value) > limit:
            errs.append(
                CsvRowError(
                    row=idx, field=field_name, message=f"{field_name} exceeds {limit} characters"
                )
            )

    if phone and (len(phone) > MAX_PHONE_LEN or not PHONE_RE.match(phone)):
        errs.append(CsvRowError(row=idx, field="phone", message=f"Invalid phone number: {phone!r}"))
    return errs
