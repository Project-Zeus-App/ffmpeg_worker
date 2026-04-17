# FFmpeg Processing Worker

Small **FastAPI + Uvicorn** HTTP service for **n8n Cloud** (or any client) to upload **1–2 MP4 clips** via `multipart/form-data`, normalize and optionally concatenate them with FFmpeg, **hard-trim** to a maximum duration, extract a **JPEG thumbnail** from the **final** output, and return **`application/zip`** containing:

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
| **`MAX_UPLOAD_MB`** | Max upload size hint (default **200**). Checked via `Content-Length` when present; per-file and total checks after save. |
| **`PORT`** | Listen port (default **8080**). **Render** sets this automatically. |
| **`MAX_PROCESS_SECONDS`** | Optional timeout (seconds) for **each** `ffmpeg` subprocess. Omit for no timeout. |

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

### One clip

```bash
ffmpeg -y -f lavfi -i testsrc=duration=8:size=640x360:rate=30 -pix_fmt yuv420p /tmp/clip1.mp4

curl -sS -X POST "http://localhost:8080/process" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Expect:" \
  -F "targetSeconds=10" \
  -F "clip1=@/tmp/clip1.mp4;type=video/mp4" \
  --output /tmp/out.zip \
  -w "\nHTTP %{http_code}\n"

unzip -l /tmp/out.zip
```

### Two clips

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

### Example successful response

- **Status:** `200`
- **`Content-Type`:** `application/zip`
- **Body:** Raw ZIP bytes (save with `--output`, as above).
- **Optional headers:** `X-Video-Duration-Seconds`, `X-Request-ID`

Unauthorized:

```bash
curl -sS -o /dev/null -w "%{http_code}" http://localhost:8080/process -X POST -F "targetSeconds=10" -F "clip1=@/tmp/clip1.mp4"
# 401
```

## n8n Cloud (HTTP Request node)

Use **POST** with **multipart/form-data**:

| Field | Type | Value |
|-------|------|--------|
| `targetSeconds` | Text | e.g. `15` |
| `clip1` | **Binary** | Map from previous node binary property (the first video) |
| `clip2` | **Binary** (optional) | Second video, if present |

**Authentication:** Add header **`Authorization`** = `Bearer <API_KEY>` or **`X-API-Key`** = your key.

Optional `hasScene2` may be sent for UI logic; this service uses **whether `clip2` binary is attached**, not that flag.

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
| **`MAX_UPLOAD_MB`** | No | Default `200`. App enforces this **after** saving parts (and via `Content-Length` when the client sends it). |
| **`MAX_PROCESS_SECONDS`** | No | Per-`ffmpeg` subprocess timeout. |
| **`PORT`** | No | Leave unset so Render sets it. |

### Upload limits vs the platform

- This app enforces **`MAX_UPLOAD_MB`** (early reject when **`Content-Length`** is present and trustworthy; **always** checks each saved file and **combined** size for two clips).
- **Render (and any reverse proxy)** may impose **their own** body-size or timeout limits. Large MP4 uploads can still fail at the edge even if **`MAX_UPLOAD_MB`** is high — check Render/plan docs and test with realistic file sizes.

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
| **413** | Payload over `MAX_UPLOAD_MB` (when enforced) |
| **500** | FFmpeg failure or misconfiguration (`API_KEY` unset server-side) |

JSON error bodies avoid leaking filesystem paths; FFmpeg details appear in **structured server logs** (`stderr` tail on failure).
