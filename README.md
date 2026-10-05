# Hospital Bulk Processing System

A FastAPI service that takes a CSV of hospitals, creates each one through the
[Hospital Directory API](https://hospital-directory.onrender.com/docs) under a
fresh batch ID, and activates the batch once every row has been created.

**Live API (Swagger UI):** https://hospital-bulk-processor-qgt6.onrender.com/docs  
**Repo:** https://github.com/tanu-11sharma/hospital-bulk-processor

> Hosted on Render's free tier: the first request after ~15 min idle takes up to a minute while the instance wakes.

---

## Quick start

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload            # http://localhost:8000/docs
pytest -q                                # 42 tests, ~1.5s, no network needed
```

Docker:

```bash
docker compose up --build                # API on :8000
docker compose run --rm tests            # test suite inside the image
```

Try it:

```bash
curl -F file=@samples/hospitals.csv http://localhost:8000/hospitals/bulk
```

Offline demo against a fake upstream (with a slow API and one injected failure):

```bash
FAKE_LATENCY=0.3 FAKE_FAIL="Riverside Clinic:500,500,500,500" python scripts/fake_upstream_server.py &
HOSPITAL_API_BASE_URL=http://127.0.0.1:9000 uvicorn app.main:app
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/hospitals/bulk` | **Required endpoint.** Upload CSV, process, return results. `?async_mode=true` returns `202` immediately. |
| `POST` | `/hospitals/bulk/validate` | Validate a CSV without creating anything. |
| `GET` | `/hospitals/bulk/{batch_id}` | Progress / result of a batch (polling). |
| `WS` | `/hospitals/bulk/{batch_id}/ws` | Pushes a snapshot on every change until the batch finishes. |
| `POST` | `/hospitals/bulk/{batch_id}/resume` | Retry failed rows and/or activation, reusing the same batch ID. |
| `DELETE` | `/hospitals/bulk/{batch_id}` | Roll back: delete the batch upstream. |
| `GET` | `/hospitals/bulk` | Recent batches. |
| `GET` | `/health` | Liveness (`?check_upstream=true` also pings the upstream). |

### CSV format

`name,address,phone` — phone optional, header row optional, max **20** rows.
Quoted fields (`"12 Main St, Apt 4"`), UTF‑8 BOM and blank lines are handled.
The **whole file is validated before anything is sent upstream**; if any row is
bad the response is `422` listing every problem (row, field, message), so the
user fixes everything in one pass and no half-created batch is left behind.

| Status | When |
|---|---|
| `400` | Empty / unreadable file, wrong header, more than 20 rows |
| `413` | File larger than `MAX_UPLOAD_BYTES` |
| `415` | Not a CSV |
| `422` | Row-level validation errors (missing name/address, bad phone, duplicate rows, wrong column count) |

### Response

Matches the spec, plus `status`, per-row `attempts`/`error` and `activation_error`:

```json
{
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "total_hospitals": 5,
  "processed_hospitals": 5,
  "failed_hospitals": 0,
  "processing_time_seconds": 1.21,
  "batch_activated": true,
  "activation_error": null,
  "hospitals": [
    {"row": 1, "hospital_id": 101, "name": "General Hospital",
     "status": "created_and_activated", "attempts": 1, "error": null}
  ]
}
```

`processed_hospitals` counts rows successfully created upstream.

---

## Design

```
            ┌──────────── FastAPI (app/main.py) ────────────┐
CSV ──────▶ │ upload limits → csv_parser (validate all rows)│
            │            │                                  │
            │            ▼                                  │
            │   BulkProcessor (app/processor.py)            │      Hospital Directory API
            │   ├─ new UUID batch, Job in JobStore          │      ──────────────────────
            │   ├─ POST /hospitals/  × N  ── semaphore ─────┼────▶ POST /hospitals/
            │   │   concurrent, retries, backoff+jitter     │
            │   └─ all ok? ── PATCH …/activate ─────────────┼────▶ PATCH /hospitals/batch/{id}/activate
            │                                               │
            │   JobStore (in-memory) ── change events ──▶ WS / polling
            └───────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `app/csv_parser.py` | Parsing + validation, collects all errors |
| `app/upstream.py` | Shared pooled `httpx.AsyncClient`, retry policy |
| `app/processor.py` | Workflow, concurrency, activation, resume, rollback |
| `app/store.py` | Job state, progress events, bounded in-memory storage |
| `app/main.py` | HTTP/WebSocket layer only |

### Failure semantics (the important part)

* **A batch only goes live when every row succeeded.** If any row still fails
  after retries, activation is skipped and the job ends `partially_failed`. The
  rows that did get created stay inactive upstream, so the directory never shows
  half an upload.
* **Retries only where they can help.** Network errors, timeouts, `408/425/429/5xx`
  are retried with exponential backoff + jitter (honouring `Retry-After`).
  The upstream does rate-limit, so a `429` without `Retry-After` backs off
  much harder (2 s, 4 s, 8 s, …) than a `5xx`.
  Other `4xx` mean the data is wrong, so they fail immediately.
* **Resume** (`POST /hospitals/bulk/{id}/resume`) re-runs only the failed rows
  under the *same* batch ID, then activates. If creation already succeeded but
  activation failed (`activation_failed`), resume only retries activation.
* **No duplicates on resume.** A create can succeed upstream while our request
  times out. Before retrying, resume reads `GET /hospitals/batch/{id}` and adopts
  any matching record instead of creating it twice.
* **Rollback** (`DELETE`) removes the whole batch upstream for uploads you
  don't want to resume.
* A per-job lock prevents two runs (e.g. concurrent resumes) on one batch; an
  unexpected exception can never leave a job stuck in `processing`.

### Performance & scalability

* **Concurrent creation.** Rows are created in parallel with `asyncio.gather`,
  bounded by a semaphore (`MAX_CONCURRENCY`, default 5, because the upstream rate-limits). With
  300 ms upstream latency, 20 rows take ~1.2 s instead of ~6 s sequentially. A test asserts the
  bound is never exceeded.
* **The semaphore is process-wide**, not per upload, so many simultaneous uploads
  can't overwhelm the upstream.
* **One pooled HTTP client** for the process: keep-alive connections, no
  TLS handshake per row.
* **Cold-start mitigation.** The upstream runs on Render's free tier and sleeps;
  the service pings it at startup so the first upload doesn't pay the full wake-up,
  and the request timeout defaults to 60 s.
* **Async mode** decouples request lifetime from processing time, so slow upstream
  responses don't hit proxy/client timeouts.
* **Bounded memory.** The job store evicts the oldest *finished* jobs past
  `MAX_STORED_JOBS`.

**Scaling beyond one instance:** jobs live in process memory (acceptable per the
brief), so the container runs a single worker. To scale horizontally, move
`JobStore` to Redis/Postgres and run processing on a queue (RQ/Celery/Arq); the
processor and API only touch the store through its small interface, so that's a
contained change.

---

## Configuration

| Env var | Default | |
|---|---|---|
| `HOSPITAL_API_BASE_URL` | `https://hospital-directory.onrender.com` | Upstream API |
| `MAX_CONCURRENCY` | `5` | Max in-flight upstream calls (process-wide) |
| `MAX_RETRIES` | `4` | Retries after the first attempt |
| `RETRY_BACKOFF_SECONDS` | `0.5` | Base backoff (doubles each retry) |
| `RATE_LIMIT_BACKOFF_SECONDS` | `2.0` | Base wait after a `429` with no `Retry-After` (doubles each retry) |
| `REQUEST_TIMEOUT_SECONDS` | `60` | Per upstream request |
| `MAX_CSV_ROWS` | `20` | Rows per upload |
| `MAX_UPLOAD_BYTES` | `1048576` | Upload size cap |
| `MAX_STORED_JOBS` | `1000` | In-memory job history |

---

## Testing

`pytest -q` runs 42 tests in about 1.5 s with no network (93% line coverage). The upstream is
replaced by `tests/fake_upstream.py`, plugged into the real `httpx` client via
`MockTransport`, so status handling, retries and JSON parsing are all exercised.
It can inject `5xx`, `429`, `4xx`, timeouts, "created but the response was lost",
and activation failures.

Covered: CSV edge cases, happy path, validation-before-upload, retry and
non-retry behaviour, partial failure without activation, resume, resume
reconciliation (no duplicates), activation failure + resume, concurrency bound
and speed-up, async mode + polling, WebSocket streaming, rollback, health.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.9 and 3.12 and builds
the Docker image.

---

## Deploying to Render

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo. `render.yaml` defines a free
   Docker web service with `/health` as the health check.
   (Or **New → Web Service**, runtime *Docker*, no other settings needed.)
3. Open `https://<service>.onrender.com/docs`.

---

## Observed in production

Verified end to end against the real Hospital Directory API: a 5-row CSV is
created and activated in ~5 s, and `GET /hospitals/batch/{id}` upstream shows
every record `active=true`.

Right after deploying, the upstream answered **every** call from the Render
instance with `429 Too Many Requests` (Render free-tier services share outbound
IPs). The service behaved as designed: rows were retried with backoff, the
batch was **not** activated, and the job ended `partially_failed`. A few
minutes later `POST /hospitals/bulk/{id}/resume` completed the same batch and
activated it. No duplicates were created.

## Assumptions

* `POST /hospitals/` takes the batch ID in the JSON body as `creation_batch_id`
  and returns the created hospital with its `id` (confirmed against the
  upstream OpenAPI schema).
* Hospitals with the same name *and* address in one file are treated as an
  input mistake and rejected.
* Phone format is validated loosely (digits, spaces, `+ - ( ) . /` and
  extensions) since the upstream doesn't define one.
