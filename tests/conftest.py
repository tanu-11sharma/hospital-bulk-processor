import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fake_upstream import FakeHospitalDirectory  # noqa: E402


def make_settings(**overrides) -> Settings:
    base = dict(
        hospital_api_base_url="http://upstream.test",
        request_timeout_seconds=5.0,
        max_concurrency=5,
        max_retries=2,
        retry_backoff_seconds=0.0,
        rate_limit_backoff_seconds=0.0,
        max_csv_rows=20,
        max_upload_bytes=64 * 1024,
        max_stored_jobs=100,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def upstream() -> FakeHospitalDirectory:
    return FakeHospitalDirectory()


@pytest.fixture
def make_client(upstream):
    clients = []

    def _make(**overrides) -> TestClient:
        app = create_app(make_settings(**overrides), transport=upstream.transport(), warm_upstream=False)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def csv_file(text: str, filename: str = "hospitals.csv"):
    return {"file": (filename, text.encode(), "text/csv")}


def rows(n: int, header: bool = True) -> str:
    lines = ["name,address,phone"] if header else []
    lines += [f"Hospital {i},{i} Main St,555-{i:04d}" for i in range(1, n + 1)]
    return "\n".join(lines) + "\n"
