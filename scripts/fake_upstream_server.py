"""Run the fake Hospital Directory API as a real HTTP server for local demos.

    python scripts/fake_upstream_server.py            # http://127.0.0.1:9000
    HOSPITAL_API_BASE_URL=http://127.0.0.1:9000 uvicorn app.main:app

Set FAKE_LATENCY=0.5 to simulate a slow upstream, FAKE_FAIL="Name:500,500"
to inject failures for a hospital with that name.
"""
import os
import sys
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.fake_upstream import FakeHospitalDirectory  # noqa: E402

fake = FakeHospitalDirectory(latency=float(os.getenv("FAKE_LATENCY", "0")))
for spec in filter(None, os.getenv("FAKE_FAIL", "").split(";")):
    name, behaviours = spec.split(":", 1)
    fake.fail_create(name, *behaviours.split(","))


async def proxy(request: Request) -> Response:
    req = httpx.Request(request.method, str(request.url), content=await request.body())
    try:
        resp = await fake._handle(req)
    except httpx.TimeoutException:
        return Response(status_code=504)
    return Response(resp.content, status_code=resp.status_code, media_type="application/json")


app = Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "POST", "PATCH", "DELETE"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("FAKE_PORT", "9000")))
