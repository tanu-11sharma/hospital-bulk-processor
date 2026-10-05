"""In-process fake of the Hospital Directory API with failure injection.

Plugged into the real ``httpx.AsyncClient`` via ``httpx.MockTransport``, so the
service's HTTP code paths (status handling, retries, JSON parsing) are all
exercised without network access.
"""
import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

import httpx


class FakeHospitalDirectory:
    def __init__(self, latency: float = 0.0):
        self.hospitals: Dict[int, dict] = {}
        self._next_id = 1
        self.latency = latency
        # name -> queue of behaviours applied to successive POSTs for that name:
        #   "500" / "503" / "429" / "400" -> respond with that status
        #   "timeout"                     -> raise ReadTimeout, nothing created
        #   "created_then_timeout"        -> create the record, then raise ReadTimeout
        self.create_faults: Dict[str, Deque[str]] = defaultdict(deque)
        self.activate_faults: Deque[str] = deque()
        self.calls: List[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    # ---- fault injection helpers ----------------------------------------
    def fail_create(self, name: str, *behaviours: str) -> None:
        self.create_faults[name].extend(behaviours)

    def fail_activate(self, *behaviours: str) -> None:
        self.activate_faults.extend(behaviours)

    def batch(self, batch_id: str) -> List[dict]:
        return [h for h in self.hospitals.values() if h["creation_batch_id"] == batch_id]

    # ---- transport -------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency:
                await asyncio.sleep(self.latency)
            return self._route(request)
        finally:
            self.in_flight -= 1

    def _route(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append(f"{method} {path}")
        parts = [p for p in path.split("/") if p]

        if method == "GET" and path == "/docs":
            return httpx.Response(200, text="ok")
        if method == "POST" and parts == ["hospitals"]:
            return self._create(request)
        if method == "GET" and parts == ["hospitals"]:
            return httpx.Response(200, json=list(self.hospitals.values()))
        if len(parts) >= 3 and parts[:2] == ["hospitals", "batch"]:
            batch_id = parts[2]
            if method == "GET" and len(parts) == 3:
                return httpx.Response(200, json=self.batch(batch_id))
            if method == "PATCH" and parts[3:] == ["activate"]:
                return self._activate(request, batch_id)
            if method == "DELETE" and len(parts) == 3:
                doomed = [h["id"] for h in self.batch(batch_id)]
                for hid in doomed:
                    del self.hospitals[hid]
                return httpx.Response(200, json={"deleted": len(doomed)})
        return httpx.Response(404, json={"detail": "Not Found"})

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("name") or not body.get("address"):
            return httpx.Response(422, json={"detail": "name and address required"})

        fault = self._next_fault(self.create_faults.get(body["name"]))
        if fault == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if fault == "429bare":
            return httpx.Response(429, json={"detail": "rate limited"})
        if fault and fault.isdigit():
            headers = {"Retry-After": "0"} if fault == "429" else {}  # "429bare" = no header
            return httpx.Response(int(fault), json={"detail": "injected"}, headers=headers)

        record = self._store(body)
        if fault == "created_then_timeout":
            raise httpx.ReadTimeout("simulated timeout after write", request=request)
        return httpx.Response(200, json=record)

    def _activate(self, request: httpx.Request, batch_id: str) -> httpx.Response:
        fault = self._next_fault(self.activate_faults)
        if fault == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if fault:
            return httpx.Response(int(fault), json={"detail": "injected"})
        for h in self.batch(batch_id):
            h["active"] = True
        return httpx.Response(200, json={"activated": len(self.batch(batch_id))})

    def _store(self, body: dict) -> dict:
        record = {
            "id": self._next_id,
            "name": body["name"],
            "address": body["address"],
            "phone": body.get("phone"),
            "creation_batch_id": body.get("creation_batch_id"),
            "active": body.get("creation_batch_id") is None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.hospitals[self._next_id] = record
        self._next_id += 1
        return record

    @staticmethod
    def _next_fault(queue: Optional[Deque[str]]) -> Optional[str]:
        return queue.popleft() if queue else None
