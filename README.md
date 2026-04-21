# FFmpeg Processing Worker

Small **FastAPI + Uvicorn** HTTP service for **n8n Cloud** (or any client) to upload **1–2 MP4 clips** via **`multipart/form-data`** (`POST /`, `POST /process`) **or** supply **HTTPS URLs** (`POST /process-url`), normalize and optionally concatenate them with FFmpeg, **hard-trim** to a maximum duration, extract a **JPEG thumbnail** from the **final** output, and return **`application/zip`** (**streamed from disk**, not fully buffered in RAM) containing:

- `final.mp4`
- `thumb.jpg`

Authentication: **`Authorization: Bearer <token>`** or **`X-API-Key: <token>`** matching **`API_KEY`**.

## Trim / pad behavior

- The final encode uses **`-t targetSeconds`** so duration is **never longer than** `targetSeconds`.
- If the combined source is **shorter** than `targetSeconds`, the output stays **shorter** — **no padding** (no frozen-frame pad). This keeps behavior simple and predictable.

## Thumbnail

- JPEG (`-q:v 2`), extracted **after** concat + trim from `final.mp4`.
- Seek time: **midpoint** of the final file when duration is known from `ffprobe`; otherwise **`1` second**.

## Environment variables

| Variable | Description |
|----------|-------------|
| **`API_KEY`** | **Required.** Shared secret for Bearer / `X-API-Key`. |
| **`MAX_UPLOAD_MB`** | Max multipart upload size (default **200** MB). Checked via `Content-Length` when present; per-file and total checks after save. Also the default for **`MAX_DOWNLOAD_MB`** if unset. |
| **`MAX_DOWNLOAD_MB`** | Optional. Max bytes per URL flow (`POST /process-url`): same semantics as upload cap (per clip + combined total). Defaults to **`MAX_UPLOAD_MB`**. |
| **`MAX_PROCESS_SECONDS`** | Optional timeout (seconds) for **each** `ffmpeg` / `ffprobe` subprocess. Omit for no timeout. |
| **`FFMPEG_PRESET`** | Optional x264 preset (default **`medium`**). Use **`veryfast`** or **`ultrafast`** on small Render instances to reduce CPU time (larger files / lower quality tradeoff). |
| **`MAX_OUTPUT_WIDTH`** | Optional. If set (pixels), the **first** normalize step scales down so width ≤ this value (lighter RAM/CPU on 4K sources). Second clip is padded to match the first output. |
| **`PORT`** | Listen port (default **8080**). **Render** sets this automatically. |

## Local run

```bash
export API_KEY=test-secret
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

### Docker

```bash
docker build -t ffmpeg-worker .
docker run --rm -p 8080:8080 -e API_KEY=test-secret ffmpeg-worker
```

### Docker Compose

```bash
export API_KEY=test-secret
docker compose up --build
```

Health:

```bash
curl -s http://localhost:8080/health
```

## `curl` examples

Replace `YOUR_API_KEY`, paths, and host as needed.

### One clip (`POST /`)

```bash
ffmpeg -y -f lavfi -i testsrc=duration=8:size=640x360:rate=30 -pix_fmt yuv420p /tmp/clip1.mp4

curl -sS -X POST "http://localhost:8080/" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Expect:" \
  -F "targetSeconds=10" \
  -F "clip1=@/tmp/clip1.mp4;type=video/mp4" \
  --output /tmp/out.zip \
  -w "\nHTTP %{http_code}\n"

unzip -l /tmp/out.zip
```

### Two clips (`POST /process`)

```bash
ffmpeg -y -f lavfi -i testsrc=duration=3:size=640x360:rate=30 -pix_fmt yuv420p /tmp/a.mp4
ffmpeg -y -f lavfi -i smptebars=duration=4:size=640x360:rate=30 -pix_fmt yuv420p /tmp/b.mp4

curl -sS -X POST "http://localhost:8080/process" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Expect:" \
  -F "targetSeconds=15" \
  -F "clip1=@/tmp/a.mp4;type=video/mp4" \
  -F "clip2=@/tmp/b.mp4;type=video/mp4" \
  --output /tmp/out2.zip \
  -w "\nHTTP %{http_code}\n"

unzip -l /tmp/out2.zip
```

### URL-based (`POST /process-url`, JSON body)

Streams each URL to a temp file with the same size limits as uploads (`MAX_DOWNLOAD_MB` or `MAX_UPLOAD_MB`). Only **`http://`** and **`https://`** URLs are accepted (enforced by validation).

```bash
curl -sS -X POST "http://localhost:8080/process-url" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"clip1Url":"https://example.com/clip1.mp4","clip2Url":"https://example.com/clip2.mp4","targetSeconds":15}' \
  --output /tmp/out-url.zip \
  -w "\nHTTP %{http_code}\n"

unzip -l /tmp/out-url.zip
```

### Example successful response

- **Status:** `200`
- **`Content-Type`:** `application/zip`
- **Body:** ZIP streamed from a temp file (client saves with `--output` as usual).
- **Optional headers:** `X-Video-Duration-Seconds`, `X-Request-ID`

Unauthorized:

```bash
curl -sS -o /dev/null -w "%{http_code}" http://localhost:8080/process -X POST -F "targetSeconds=10" -F "clip1=@/tmp/clip1.mp4"
# 401
```

## n8n Cloud (HTTP Request node)

### Multipart (upload files)

Use **POST** with **multipart/form-data**:

- **Endpoints:** **`POST /`** (root), **`POST /process`** — same behavior.
- **Health:** **`GET /health`** (no auth).

| Field | Type | Required | Value |
|-------|------|----------|--------|
| `targetSeconds` | Text | **Yes** | Integer seconds (e.g. `15`). |
| `clip1` | **Binary** | **Yes** | First video file. |
| `clip2` | **Binary** | No | Second video, if present. |

Optional form field **`hasScene2`** may be sent; logic uses **whether `clip2` is attached**, not that flag.

### JSON URLs (avoid huge multipart via n8n → Render)

- **Endpoint:** **`POST /process-url`**
- **Body:** JSON `{ "clip1Url": "<https URL>", "clip2Url": "<optional>", "targetSeconds": <number> }`

**Authentication:** **`Authorization`:** `Bearer <API_KEY>` or **`X-API-Key`:** `<API_KEY>`.

### n8n HTTP Request node tips

- **Multipart:** Body content type **Form-Data**; map `clip1` / `clip2` as **binary**.
- **URLs:** Body **JSON**, endpoint **`/process-url`**; ensure previous node exposes **public HTTPS** URLs Cloud Run / Render can fetch (signed URLs OK).
- **Timeout:** Set high enough for upload + FFmpeg (often **`120–600s`** depending on length and instance).
- **Retries:** Low count with backoff for **502** / network only — avoid retrying huge uploads many times.

## Deployment on Render (Web Service)

### Suggested Render settings

| Setting | Value |
|--------|--------|
| **Service type** | Web Service |
| **Deploy** | Docker (`Dockerfile` at repo root), **or** connect repo and let Render auto-detect Docker |
| **Build command** | *(Docker deploy)* Usually empty / default — Render builds the image from `Dockerfile`. |
| **Start command** | *(Docker deploy)* Empty — use the image **default command** (see below). |
| **Health check path** | **`/health`** (expects JSON like `{"ok":true}`) |

**Container listen address (Render killer #1):** this image runs Uvicorn on **`0.0.0.0`** and **`$PORT`**:

```dockerfile
CMD sh -c 'exec uvicorn main:app --host 0.0.0.0 --port "${PORT}"'
```

Do **not** override `PORT` in the dashboard unless you know what you’re doing — Render injects **`PORT`** automatically.

### Environment variables (Render)

| Variable | Required | Notes |
|----------|----------|--------|
| **`API_KEY`** | Yes | Strong secret; use in n8n as Bearer or `X-API-Key`. |
| **`MAX_UPLOAD_MB`** | No | Default `200`. Multipart: per-part + total; also default cap for URL downloads if **`MAX_DOWNLOAD_MB`** unset. |
| **`MAX_DOWNLOAD_MB`** | No | Cap for **`POST /process-url`** streamed downloads. |
| **`MAX_PROCESS_SECONDS`** | No | Per-subprocess timeout (`ffmpeg` / `ffprobe`). |
| **`FFMPEG_PRESET`** | No | e.g. **`veryfast`** on **512MB–1GB** RAM instances. |
| **`MAX_OUTPUT_WIDTH`** | No | e.g. **`1280`** to cap input width on the first normalize pass. |
| **`PORT`** | No | Leave unset so Render sets it. |

### RAM recommendation (Render)

- **512MB instances** often **OOM** or get killed during **long** x264 jobs on large files. Prefer **≥ 2GB RAM** for reliable production FFmpeg, or lower load via **`FFMPEG_PRESET=veryfast`** / **`MAX_OUTPUT_WIDTH`**, **`POST /process-url`** (skip duplicate multipart buffering), and **serial** n8n execution.
- Zip responses are **file-backed** and streamed to reduce peak RAM vs holding the whole ZIP in memory.

### Upload limits vs the platform

- This app enforces **`MAX_UPLOAD_MB`** / **`MAX_DOWNLOAD_MB`** on the worker (multipart after save; URL streams while counting bytes).
- **Render** may still impose **proxy body size**, **idle timeouts**, or **cold start** limits. **`502`** during long requests often means **platform timeout** or **process killed (OOM)** — increase instance RAM, timeouts, or use **URL-based** ingestion + **`FFMPEG_PRESET`**.

### Memory, disk, and concurrency

- FFmpeg is heavy: **RAM**, **CPU**, and **temp disk** scale with resolution, duration, and **parallel requests**.
- This service is **stateless** and does not queue jobs — under load, **multiple overlapping FFmpeg runs** can exhaust a small instance. Prefer a larger instance type or limit concurrency upstream (e.g. n8n / API gateway) if you see OOM or timeouts.

### Before you call it “production-ready” (~15 minutes)

Run the **Docker + curl** proof locally (needs Docker):

```bash
docker build -t ffmpeg-worker .
docker run --rm -p 8080:8080 -e API_KEY=test -e PORT=8080 ffmpeg-worker

curl -sS http://localhost:8080/health

ffmpeg -y -f lavfi -i testsrc=duration=5:size=640x360:rate=30 -pix_fmt yuv420p /tmp/proof.mp4

curl -sS -X POST "http://localhost:8080/process" \
  -H "Authorization: Bearer test" \
  -H "Expect:" \
  -F "targetSeconds=10" \
  -F "clip1=@/tmp/proof.mp4;type=video/mp4" \
  --output /tmp/out.zip

unzip -l /tmp/out.zip   # expect final.mp4 and thumb.jpg
```

Until this passes on your machine, treat cloud deploy as **“likely OK”**, not proven.

## Errors

| Code | Meaning |
|------|---------|
| **401** | Missing or invalid API key |
| **400** | Missing `targetSeconds` / `clip1`, invalid values |
| **413** | Multipart or download over configured MB limit |
| **502** | Upstream URL download failed (`POST /process-url`) |
| **500** | FFmpeg failure or misconfiguration (`API_KEY` unset server-side) |

JSON error bodies avoid leaking filesystem paths; FFmpeg details appear in **structured server logs** (stderr tail on failure, written to temp files during runs to limit RAM).
