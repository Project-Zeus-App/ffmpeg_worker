"""
FFmpeg Processing Worker — HTTP API for n8n Cloud multipart uploads.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, HttpUrl
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from ffmpeg_service import (
    get_max_download_bytes,
    get_max_process_seconds,
    process_clips_to_zip,
)

# --- Structured JSON logging -------------------------------------------------

_handler = logging.StreamHandler(sys.stdout)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, self.datefmt),
        }
        if hasattr(record, "extra_json") and isinstance(record.extra_json, dict):
            payload.update(record.extra_json)
        return json.dumps(payload, default=str)


_handler.setFormatter(JsonFormatter())
_root = logging.getLogger()
_root.handlers.clear()
_root.addHandler(_handler)
_root.setLevel(logging.INFO)

log = logging.getLogger("ffmpeg_worker")


def log_extra(**kwargs: object) -> dict:
    return {"extra_json": kwargs}


app = FastAPI(title="FFmpeg Worker", version="1.0.0")
security = HTTPBearer(auto_error=False)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = rid
    start = time.perf_counter()
    log.info(
        "request_start",
        extra=log_extra(
            request_id=rid,
            method=request.method,
            path=request.url.path,
        ),
    )
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.exception(
            "request_error",
            extra=log_extra(request_id=rid, duration_ms=round(elapsed_ms, 2)),
        )
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    log.info(
        "request_end",
        extra=log_extra(
            request_id=rid,
            status_code=response.status_code,
            duration_ms=round(elapsed_ms, 2),
        ),
    )
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def enforce_max_body(request: Request, call_next):
    if request.method != "POST" or request.url.path not in {"/process", "/"}:
        return await call_next(request)
    max_mb = int(os.environ.get("MAX_UPLOAD_MB", "200"))
    max_bytes = max(0, max_mb) * 1024 * 1024
    cl = request.headers.get("content-length")
    if cl:
        try:
            n = int(cl)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if n > max_bytes:
            log.warning(
                "payload_too_large",
                extra=log_extra(
                    request_id=getattr(request.state, "request_id", None),
                    content_length=n,
                    max_bytes=max_bytes,
                ),
            )
            raise HTTPException(
                status_code=413,
                detail={"error": "Request body too large", "max_upload_mb": max_mb},
            )
    return await call_next(request)


def require_api_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> None:
    expected = os.environ.get("API_KEY", "")
    if not expected:
        log.error("API_KEY is not set")
        raise HTTPException(status_code=500, detail="Server misconfiguration")
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials.strip()
    if not token:
        token = request.headers.get("X-API-Key", "").strip()
    if not token or token != expected:
        log.warning(
            "unauthorized",
            extra=log_extra(request_id=getattr(request.state, "request_id", None)),
        )
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _save_upload(upload: UploadFile, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    chunk = 1024 * 1024
    with dest.open("wb") as f:
        while True:
            block = await upload.read(chunk)
            if not block:
                break
            total += len(block)
            f.write(block)
    return total


def _safe_unlink(path_str: str) -> None:
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError:
        pass


def _zip_file_response(zip_path: Path, duration_sec: float | None) -> FileResponse:
    headers: dict[str, str] = {}
    if duration_sec is not None:
        headers["X-Video-Duration-Seconds"] = f"{duration_sec:.4f}"
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename="output.zip",
        headers=headers,
        background=BackgroundTask(_safe_unlink, str(zip_path)),
    )


def _max_download_mb_for_errors() -> int:
    raw = os.environ.get("MAX_DOWNLOAD_MB", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return int(os.environ.get("MAX_UPLOAD_MB", "200"))


async def _stream_download_url(
    url: str,
    dest: Path,
    max_bytes: int,
    *,
    request_id: str,
) -> int:
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total = 0
                with dest.open("wb") as f:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            max_mb = _max_download_mb_for_errors()
                            log.warning(
                                "download_too_large",
                                extra=log_extra(
                                    request_id=request_id,
                                    url_host=url.split("/")[2] if "://" in url else "",
                                ),
                            )
                            raise HTTPException(
                                status_code=413,
                                detail={
                                    "error": "Download exceeded size limit",
                                    "max_download_mb": max_mb,
                                },
                            )
                        f.write(chunk)
                return total
        except httpx.HTTPStatusError as e:
            log.warning(
                "download_http_error",
                extra=log_extra(
                    request_id=request_id,
                    status_code=e.response.status_code,
                ),
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Failed to download clip URL",
                    "upstream_status": e.response.status_code,
                },
            ) from e
        except httpx.RequestError as e:
            log.warning(
                "download_request_error",
                extra=log_extra(request_id=request_id, exc_type=type(e).__name__),
            )
            raise HTTPException(
                status_code=502,
                detail={"error": "Failed to download clip URL", "message": str(e)},
            ) from e


class ProcessUrlBody(BaseModel):
    clip1Url: HttpUrl
    clip2Url: HttpUrl | None = None
    targetSeconds: int | float


@app.get("/health")
def health():
    return {"ok": True}


async def _handle_process_request(
    request: Request,
    _: Annotated[None, Depends(require_api_key)],
    targetSeconds: Annotated[str | None, Form()] = None,
    clip1: Annotated[UploadFile | None, File()] = None,
    clip2: Annotated[UploadFile | None, File()] = None,
    hasScene2: Annotated[str | None, Form()] = None,
):
    _ = hasScene2  # n8n may send; presence of clip2 file is what matters
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    if clip1 is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "clip1 file is required"},
        )

    raw_ts = targetSeconds if isinstance(targetSeconds, str) else str(targetSeconds or "")
    if not raw_ts.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "targetSeconds is required"},
        )
    try:
        ts = int(float(raw_ts.strip()))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"error": "targetSeconds must be a positive integer (seconds)"},
        )
    if ts <= 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "targetSeconds must be positive"},
        )

    max_mb = int(os.environ.get("MAX_UPLOAD_MB", "200"))
    max_bytes = max(0, max_mb) * 1024 * 1024

    work_in = Path(tempfile.mkdtemp(prefix="ffw_in_"))
    p1 = work_in / "clip1.mp4"
    p2_path = work_in / "clip2.mp4"
    try:
        n1 = await _save_upload(clip1, p1)
        if n1 <= 0:
            raise HTTPException(
                status_code=400,
                detail={"error": "clip1 file is required"},
            )
        if max_bytes and n1 > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={"error": "clip1 exceeds upload limit", "max_upload_mb": max_mb},
            )

        p2: Path | None = None
        if clip2 is not None:
            size2 = await _save_upload(clip2, p2_path)
            if size2 > 0:
                if max_bytes and size2 > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={"error": "clip2 exceeds upload limit", "max_upload_mb": max_mb},
                    )
                if max_bytes and (n1 + size2) > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={"error": "Total upload exceeds limit", "max_upload_mb": max_mb},
                    )
                p2 = p2_path

        t0 = time.perf_counter()
        zip_path, duration_sec, err = process_clips_to_zip(
            p1,
            p2,
            ts,
            logger=log,
            request_id=request_id,
            ffmpeg_timeout_sec=get_max_process_seconds(),
        )
        proc_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "process_done",
            extra=log_extra(
                request_id=request_id,
                processing_ms=round(proc_ms, 2),
                two_clips=p2 is not None,
                target_seconds=ts,
            ),
        )

        if err or zip_path is None:
            log.error(
                "process_failed",
                extra=log_extra(request_id=request_id, safe_message=err),
            )
            raise HTTPException(
                status_code=500,
                detail={"error": "Processing failed", "message": err or "unknown"},
            )

        return _zip_file_response(zip_path, duration_sec)
    finally:
        shutil.rmtree(work_in, ignore_errors=True)


@app.post("/process")
async def process(
    request: Request,
    _: Annotated[None, Depends(require_api_key)],
    targetSeconds: Annotated[str | None, Form()] = None,
    clip1: Annotated[UploadFile | None, File()] = None,
    clip2: Annotated[UploadFile | None, File()] = None,
    hasScene2: Annotated[str | None, Form()] = None,
):
    return await _handle_process_request(
        request=request,
        _=_,
        targetSeconds=targetSeconds,
        clip1=clip1,
        clip2=clip2,
        hasScene2=hasScene2,
    )


@app.post("/")
async def process_root_alias(
    request: Request,
    _: Annotated[None, Depends(require_api_key)],
    targetSeconds: Annotated[str | None, Form()] = None,
    clip1: Annotated[UploadFile | None, File()] = None,
    clip2: Annotated[UploadFile | None, File()] = None,
    hasScene2: Annotated[str | None, Form()] = None,
):
    log.info(
        "root_process_alias_used",
        extra=log_extra(request_id=getattr(request.state, "request_id", None)),
    )
    return await _handle_process_request(
        request=request,
        _=_,
        targetSeconds=targetSeconds,
        clip1=clip1,
        clip2=clip2,
        hasScene2=hasScene2,
    )


@app.post("/process-url")
async def process_url(
    request: Request,
    _: Annotated[None, Depends(require_api_key)],
    body: ProcessUrlBody,
):
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        ts = int(body.targetSeconds)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"error": "targetSeconds must be a positive integer (seconds)"},
        )
    if ts <= 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "targetSeconds must be positive"},
        )

    max_bytes = get_max_download_bytes()
    max_mb = _max_download_mb_for_errors()

    work_in = Path(tempfile.mkdtemp(prefix="ffw_in_url_"))
    p1 = work_in / "clip1.mp4"
    p2_path = work_in / "clip2.mp4"
    try:
        n1 = await _stream_download_url(
            str(body.clip1Url),
            p1,
            max_bytes,
            request_id=request_id,
        )
        if n1 <= 0:
            raise HTTPException(
                status_code=400,
                detail={"error": "clip1 download was empty"},
            )

        p2: Path | None = None
        if body.clip2Url is not None:
            remaining = max_bytes - n1
            if remaining <= 0:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error": "Total download exceeds limit after clip1",
                        "max_download_mb": max_mb,
                    },
                )
            cap2 = min(max_bytes, remaining)
            n2 = await _stream_download_url(
                str(body.clip2Url),
                p2_path,
                cap2,
                request_id=request_id,
            )
            if n2 > 0:
                p2 = p2_path

        t0 = time.perf_counter()
        zip_path, duration_sec, err = process_clips_to_zip(
            p1,
            p2,
            ts,
            logger=log,
            request_id=request_id,
            ffmpeg_timeout_sec=get_max_process_seconds(),
        )
        proc_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "process_url_done",
            extra=log_extra(
                request_id=request_id,
                processing_ms=round(proc_ms, 2),
                two_clips=p2 is not None,
                target_seconds=ts,
            ),
        )

        if err or zip_path is None:
            log.error(
                "process_failed",
                extra=log_extra(request_id=request_id, safe_message=err),
            )
            raise HTTPException(
                status_code=500,
                detail={"error": "Processing failed", "message": err or "unknown"},
            )

        return _zip_file_response(zip_path, duration_sec)
    finally:
        shutil.rmtree(work_in, ignore_errors=True)
