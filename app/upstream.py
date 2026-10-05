"""Async client for the Hospital Directory API, with retries.

One ``httpx.AsyncClient`` is shared for the whole process so TCP/TLS
connections are pooled and reused across all rows and all batches.
"""
import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class UpstreamError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, attempts: int = 0):
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


@dataclass
class CallResult:
    data: Any
    attempts: int


class HospitalDirectoryClient:
    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.hospital_api_base_url,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=httpx.Limits(
                max_connections=settings.max_concurrency * 2,
                max_keepalive_connections=settings.max_concurrency,
            ),
            transport=transport,
            headers={"User-Agent": "hospital-bulk-processor/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- endpoints -------------------------------------------------------

    async def create_hospital(
        self, name: str, address: str, phone: Optional[str], batch_id: str
    ) -> CallResult:
        payload: Dict[str, Any] = {"name": name, "address": address, "creation_batch_id": batch_id}
        if phone:
            payload["phone"] = phone
        return await self._request("POST", "/hospitals/", json=payload)

    async def list_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        try:
            result = await self._request("GET", f"/hospitals/batch/{batch_id}")
        except UpstreamError as exc:
            if exc.status_code == 404:  # upstream may 404 on an empty batch
                return []
            raise
        return result.data or []

    async def activate_batch(self, batch_id: str) -> CallResult:
        return await self._request("PATCH", f"/hospitals/batch/{batch_id}/activate")

    async def delete_batch(self, batch_id: str) -> CallResult:
        return await self._request("DELETE", f"/hospitals/batch/{batch_id}")

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/docs", timeout=10)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    # ---- plumbing --------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> CallResult:
        max_attempts = 1 + max(0, self._settings.max_retries)
        last_error: Optional[UpstreamError] = None

        for attempt in range(1, max_attempts + 1):
            retry_after: Optional[float] = None
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:  # timeouts, connection resets, DNS…
                last_error = UpstreamError(
                    f"{type(exc).__name__}: {exc or 'network error'}", attempts=attempt
                )
            else:
                if resp.status_code < 400:
                    data = resp.json() if resp.content else None
                    return CallResult(data=data, attempts=attempt)
                last_error = UpstreamError(
                    f"Upstream {method} {path} returned {resp.status_code}: {_short(resp.text)}",
                    status_code=resp.status_code,
                    attempts=attempt,
                )
                if resp.status_code not in RETRYABLE_STATUS:
                    raise last_error  # 4xx = our input is wrong; retrying won't help
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if resp.status_code == 429 and retry_after is None:
                    # Rate limited without a hint: back off much harder than for a 5xx.
                    retry_after = self._backoff(
                        attempt, base_seconds=self._settings.rate_limit_backoff_seconds
                    )

            if attempt < max_attempts:
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                log.warning("%s %s failed (attempt %d), retrying in %.2fs: %s",
                            method, path, attempt, delay, last_error)
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int, base_seconds: Optional[float] = None) -> float:
        if base_seconds is None:
            base_seconds = self._settings.retry_backoff_seconds
        base = base_seconds * (2 ** (attempt - 1))
        return base + random.uniform(0, base * 0.25)  # jitter avoids thundering herd


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return min(float(value), 30.0)
    except ValueError:
        return None


def _short(text: str, limit: int = 200) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
