"""Integration tests: real FastAPI app + real httpx client, fake upstream."""
import time

from tests.conftest import csv_file, rows


def post_bulk(client, text, **params):
    return client.post("/hospitals/bulk", files=csv_file(text), params=params)


# ---- happy path ------------------------------------------------------------


def test_bulk_create_happy_path(client, upstream):
    resp = post_bulk(client, rows(3))
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "completed"
    assert body["total_hospitals"] == 3
    assert body["processed_hospitals"] == 3
    assert body["failed_hospitals"] == 0
    assert body["batch_activated"] is True
    assert body["processing_time_seconds"] >= 0
    assert [h["row"] for h in body["hospitals"]] == [1, 2, 3]
    assert all(h["status"] == "created_and_activated" for h in body["hospitals"])
    assert all(isinstance(h["hospital_id"], int) for h in body["hospitals"])

    created = upstream.batch(body["batch_id"])
    assert len(created) == 3 and all(h["active"] for h in created)
    assert {h["phone"] for h in created} == {"555-0001", "555-0002", "555-0003"}
    assert upstream.calls.count(f"PATCH /hospitals/batch/{body['batch_id']}/activate") == 1


def test_each_upload_gets_its_own_batch(client, upstream):
    a = post_bulk(client, rows(2)).json()["batch_id"]
    b = post_bulk(client, rows(2)).json()["batch_id"]
    assert a != b
    assert len(upstream.batch(a)) == len(upstream.batch(b)) == 2


def test_max_20_rows_accepted(client):
    assert post_bulk(client, rows(20)).json()["processed_hospitals"] == 20


# ---- input validation ------------------------------------------------------


def test_invalid_rows_reject_whole_file_before_any_upstream_call(client, upstream):
    resp = post_bulk(client, "name,address\nGood,1 St\n,2 St\n")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["row"] == 2
    assert not any(c.startswith("POST") for c in upstream.calls)


def test_too_many_rows(client):
    resp = post_bulk(client, rows(21))
    assert resp.status_code == 400 and "maximum" in resp.json()["detail"]


def test_non_csv_rejected(client):
    resp = client.post("/hospitals/bulk", files={"file": ("x.png", b"\x89PNG", "image/png")})
    assert resp.status_code == 415


def test_upload_size_limit(make_client):
    small = make_client(max_upload_bytes=50)
    resp = post_bulk(small, rows(5))
    assert resp.status_code == 413


def test_missing_file_field(client):
    assert client.post("/hospitals/bulk").status_code == 422


def test_validate_endpoint_creates_nothing(client, upstream):
    resp = client.post("/hospitals/bulk/validate", files=csv_file("A,1 St\n,2 St\n"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False and body["total_rows"] == 2 and len(body["hospitals"]) == 1
    assert upstream.calls == []


# ---- resilience ------------------------------------------------------------


def test_transient_failures_are_retried(client, upstream):
    upstream.fail_create("Hospital 1", "503", "timeout")  # 2 failures, 3rd attempt ok
    upstream.fail_create("Hospital 2", "429")
    upstream.fail_activate("502")
    body = post_bulk(client, rows(3)).json()

    assert body["status"] == "completed" and body["batch_activated"]
    attempts = {h["name"]: h["attempts"] for h in body["hospitals"]}
    assert attempts == {"Hospital 1": 3, "Hospital 2": 2, "Hospital 3": 1}


def test_client_errors_are_not_retried(client, upstream):
    upstream.fail_create("Hospital 2", "400")
    body = post_bulk(client, rows(2)).json()
    bad = body["hospitals"][1]
    assert bad["status"] == "failed" and bad["attempts"] == 1 and "400" in bad["error"]


def test_partial_failure_does_not_activate_batch(client, upstream):
    upstream.fail_create("Hospital 2", "500", "500", "500")  # exhausts retries
    body = post_bulk(client, rows(3)).json()

    assert body["status"] == "partially_failed"
    assert body["processed_hospitals"] == 2 and body["failed_hospitals"] == 1
    assert body["batch_activated"] is False
    statuses = [h["status"] for h in body["hospitals"]]
    assert statuses == ["created", "failed", "created"]
    assert not any(c.endswith("/activate") for c in upstream.calls)
    assert all(not h["active"] for h in upstream.batch(body["batch_id"]))


def test_resume_retries_only_failed_rows_then_activates(client, upstream):
    upstream.fail_create("Hospital 2", "500", "500", "500")
    first = post_bulk(client, rows(3)).json()
    posts_before = sum(c == "POST /hospitals/" for c in upstream.calls)

    resp = client.post(f"/hospitals/bulk/{first['batch_id']}/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"] == first["batch_id"]
    assert body["status"] == "completed" and body["batch_activated"]
    assert all(h["status"] == "created_and_activated" for h in body["hospitals"])
    assert sum(c == "POST /hospitals/" for c in upstream.calls) == posts_before + 1
    assert len(upstream.batch(first["batch_id"])) == 3


def test_resume_reconciles_instead_of_duplicating(client, upstream):
    # Upstream stores the record but our client times out on every attempt.
    upstream.fail_create("Hospital 1", "created_then_timeout", "timeout", "timeout")
    first = post_bulk(client, rows(2)).json()
    assert first["status"] == "partially_failed"
    assert len(upstream.batch(first["batch_id"])) == 2  # the "failed" one exists upstream

    body = client.post(f"/hospitals/bulk/{first['batch_id']}/resume").json()
    assert body["status"] == "completed"
    assert len(upstream.batch(first["batch_id"])) == 2  # no duplicate created
    assert body["hospitals"][0]["hospital_id"] == 1


def test_activation_failure_then_resume(client, upstream):
    upstream.fail_activate("500", "500", "500")
    first = post_bulk(client, rows(2)).json()
    assert first["status"] == "activation_failed"
    assert first["batch_activated"] is False and first["activation_error"]
    assert all(h["status"] == "created" for h in first["hospitals"])

    posts_before = sum(c == "POST /hospitals/" for c in upstream.calls)
    body = client.post(f"/hospitals/bulk/{first['batch_id']}/resume").json()
    assert body["status"] == "completed" and body["activation_error"] is None
    assert sum(c == "POST /hospitals/" for c in upstream.calls) == posts_before


def test_resume_rejected_for_completed_or_unknown_batch(client):
    done = post_bulk(client, rows(1)).json()
    assert client.post(f"/hospitals/bulk/{done['batch_id']}/resume").status_code == 409
    assert client.post("/hospitals/bulk/nope/resume").status_code == 404


# ---- performance -----------------------------------------------------------


def test_rows_are_processed_concurrently_and_bounded(make_client, upstream):
    upstream.latency = 0.1
    client = make_client(max_concurrency=5)
    start = time.perf_counter()
    body = post_bulk(client, rows(20)).json()
    elapsed = time.perf_counter() - start

    assert body["processed_hospitals"] == 20
    assert upstream.max_in_flight == 5  # never exceeds the limit…
    assert elapsed < 1.5  # …and is far faster than 20 x 0.1s sequential + activate


# ---- async mode, polling, websocket ---------------------------------------


def _wait_terminal(client, batch_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/hospitals/bulk/{batch_id}").json()
        if body["status"] not in ("queued", "processing", "activating"):
            return body
        time.sleep(0.02)
    raise AssertionError("batch did not finish")


def test_async_mode_returns_202_and_can_be_polled(client, upstream):
    upstream.latency = 0.05
    resp = post_bulk(client, rows(5), async_mode="true")
    assert resp.status_code == 202
    accepted = resp.json()
    assert accepted["status_url"].endswith(f"/hospitals/bulk/{accepted['batch_id']}")
    assert accepted["websocket_url"].startswith("ws://")

    final = _wait_terminal(client, accepted["batch_id"])
    assert final["status"] == "completed" and final["processed_hospitals"] == 5


def test_websocket_streams_progress_until_done(client, upstream):
    upstream.latency = 0.05
    batch_id = post_bulk(client, rows(4), async_mode="true").json()["batch_id"]

    snapshots = []
    with client.websocket_connect(f"/hospitals/bulk/{batch_id}/ws") as ws:
        while True:
            try:
                snapshots.append(ws.receive_json())
            except Exception:
                break
            if snapshots[-1]["status"] == "completed":
                break

    assert snapshots[-1]["status"] == "completed"
    progress = [s["processed_hospitals"] for s in snapshots]
    assert progress == sorted(progress)  # monotonic
    assert len(snapshots) > 2  # intermediate updates were pushed


def test_websocket_unknown_batch(client):
    with client.websocket_connect("/hospitals/bulk/missing/ws") as ws:
        assert "not found" in ws.receive_json()["error"]


# ---- other endpoints -------------------------------------------------------


def test_get_and_list_batches(client):
    a = post_bulk(client, rows(1)).json()["batch_id"]
    b = post_bulk(client, rows(2)).json()["batch_id"]
    assert client.get(f"/hospitals/bulk/{a}").json()["total_hospitals"] == 1
    assert client.get("/hospitals/bulk/unknown").status_code == 404
    listed = [j["batch_id"] for j in client.get("/hospitals/bulk").json()]
    assert listed[:2] == [b, a]


def test_rollback_deletes_batch_upstream(client, upstream):
    batch_id = post_bulk(client, rows(3)).json()["batch_id"]
    resp = client.delete(f"/hospitals/bulk/{batch_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rolled_back" and resp.json()["batch_activated"] is False
    assert upstream.batch(batch_id) == []


def test_health_and_root(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/health", params={"check_upstream": True}).json()["upstream_reachable"]
    assert client.get("/", follow_redirects=False).status_code in (302, 307)


def test_rate_limit_without_retry_after_is_retried(client, upstream):
    upstream.fail_create("Hospital 1", "429bare", "429bare")
    body = post_bulk(client, rows(2)).json()
    assert body["status"] == "completed"
    assert body["hospitals"][0]["attempts"] == 3


def test_head_requests_supported(client):
    assert client.head("/health").status_code == 200
    assert client.head("/", follow_redirects=False).status_code in (302, 307)
