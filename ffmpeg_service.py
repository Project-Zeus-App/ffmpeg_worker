"""
FFmpeg pipeline: normalize clips (H.264 + AAC), optional concat, trim, thumbnail.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile


@dataclass
class FFResult:
    ok: bool
    exit_code: int | None = None
    stderr_tail: str = ""


def _stderr_tail(stderr: str, max_chars: int = 4000) -> str:
    if not stderr:
        return ""
    s = stderr.strip()
    if len(s) <= max_chars:
        return s
    return "..." + s[-max_chars:]


def run_ffmpeg(
    args: list[str],
    *,
    logger: logging.Logger,
    request_id: str,
    timeout_sec: float | None,
) -> FFResult:
    """
    Run ffmpeg with args (without 'ffmpeg' prefix — first element should be 'ffmpeg').
    Logs full command; on failure logs stderr tail.
    """
    cmd_str = " ".join(shlex_quote(a) for a in args)
    logger.info(
        json.dumps(
            {
                "event": "ffmpeg_start",
                "request_id": request_id,
                "cmd": cmd_str,
            }
        )
    )
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        logger.error(
            json.dumps(
                {
                    "event": "ffmpeg_missing",
                    "request_id": request_id,
                    "cmd": cmd_str,
                }
            )
        )
        return FFResult(
            ok=False,
            exit_code=None,
            stderr_tail="ffmpeg executable not available",
        )
    except subprocess.TimeoutExpired as e:
        err = (e.stderr or "") if hasattr(e, "stderr") else ""
        tail = _stderr_tail(err)
        logger.error(
            json.dumps(
                {
                    "event": "ffmpeg_timeout",
                    "request_id": request_id,
                    "cmd": cmd_str,
                    "stderr_tail": tail,
                }
            )
        )
        return FFResult(ok=False, exit_code=None, stderr_tail=tail or "ffmpeg timeout")

    stderr = proc.stderr or ""
    tail = _stderr_tail(stderr)
    if proc.returncode != 0:
        logger.error(
            json.dumps(
                {
                    "event": "ffmpeg_error",
                    "request_id": request_id,
                    "exit_code": proc.returncode,
                    "cmd": cmd_str,
                    "stderr_tail": tail,
                }
            )
        )
        return FFResult(ok=False, exit_code=proc.returncode, stderr_tail=tail)

    logger.info(
        json.dumps(
            {
                "event": "ffmpeg_done",
                "request_id": request_id,
                "exit_code": 0,
                "cmd": cmd_str,
            }
        )
    )
    return FFResult(ok=True, exit_code=0, stderr_tail="")


def shlex_quote(s: str) -> str:
    """Minimal safe quoting for log lines."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "._/-:" for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def ffprobe_json(path: Path, logger: logging.Logger, request_id: str) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        logger.error(
            json.dumps({"event": "ffprobe_missing", "request_id": request_id})
        )
        return {}
    if proc.returncode != 0:
        logger.error(
            json.dumps(
                {
                    "event": "ffprobe_error",
                    "request_id": request_id,
                    "exit_code": proc.returncode,
                    "stderr_tail": _stderr_tail(proc.stderr or ""),
                }
            )
        )
        return {}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def probe_video_size(data: dict[str, Any]) -> tuple[int, int] | None:
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            w = stream.get("width")
            h = stream.get("height")
            if isinstance(w, int) and isinstance(h, int):
                return w, h
    return None


def probe_duration_seconds(data: dict[str, Any]) -> float | None:
    fmt = data.get("format") or {}
    d = fmt.get("duration")
    if d is None:
        return None
    try:
        return float(d)
    except (TypeError, ValueError):
        return None


def build_normalize_cmd_1(
    input_path: Path,
    output_path: Path,
    *,
    target_w: int | None,
    target_h: int | None,
) -> list[str]:
    """
    Re-encode to H.264 + AAC, stable for concat.
    If target_w/h set, scale+pad to that frame (second clip matching first).
    """
    if target_w and target_h:
        vf = (
            f"fps=30,"
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
            f"format=yuv420p"
        )
    else:
        vf = "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"

    return [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def build_concat_cmd(
    n1: Path,
    n2: Path,
    output_path: Path,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(n1),
        "-i",
        str(n2),
        "-filter_complex",
        "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def build_final_trim_cmd(
    input_path: Path,
    output_path: Path,
    target_seconds: int,
) -> list[str]:
    """Hard trim to <= target_seconds (no padding if source is shorter)."""
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-t",
        str(target_seconds),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def build_thumbnail_cmd(
    final_mp4: Path,
    thumb_path: Path,
    seek_seconds: float,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek_seconds:.4f}",
        "-i",
        str(final_mp4),
        "-vframes",
        "1",
        "-q:v",
        "2",
        str(thumb_path),
    ]


def pick_thumbnail_seek(duration: float | None) -> float:
    if duration is not None and duration > 0 and math.isfinite(duration):
        half = duration / 2.0
        # stay inside (0, duration) for extract
        return min(max(half, 0.0), max(duration - 0.01, 0.0))
    return 1.0


def process_clips_to_zip(
    clip1_path: Path,
    clip2_path: Path | None,
    target_seconds: int,
    *,
    logger: logging.Logger,
    request_id: str,
    ffmpeg_timeout_sec: float | None,
) -> tuple[bytes, float | None, str | None]:
    """
    Returns (zip_bytes, final_duration_seconds or None, error_message or None).
    """
    work = Path(tempfile.mkdtemp(prefix="ffw_"))
    final_mp4 = work / "final.mp4"
    thumb_jpg = work / "thumb.jpg"
    n1 = work / "normalized1.mp4"
    n2 = work / "normalized2.mp4"
    concat_out = work / "concat.mp4"

    try:
        # Normalize clip1
        cmd = build_normalize_cmd_1(clip1_path, n1, target_w=None, target_h=None)
        r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
        if not r.ok:
            return b"", None, "Video processing failed."

        meta1 = ffprobe_json(n1, logger, request_id)
        size1 = probe_video_size(meta1)
        if not size1:
            return b"", None, "Could not read video dimensions after normalize."

        w1, h1 = size1

        if clip2_path is None:
            pre_final = n1
        else:
            # Normalize clip2 to match clip1 frame size (pad + same fps/audio layout)
            cmd = build_normalize_cmd_1(
                clip2_path, n2, target_w=w1, target_h=h1
            )
            r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
            if not r.ok:
                return b"", None, "Video processing failed."

            cmd = build_concat_cmd(n1, n2, concat_out)
            r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
            if not r.ok:
                return b"", None, "Video processing failed."
            pre_final = concat_out

        # Final encode with hard cap -t (if input shorter, output is shorter — no pad)
        cmd = build_final_trim_cmd(pre_final, final_mp4, target_seconds)
        r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
        if not r.ok:
            return b"", None, "Video processing failed."

        final_meta = ffprobe_json(final_mp4, logger, request_id)
        dur = probe_duration_seconds(final_meta)
        seek = pick_thumbnail_seek(dur)

        cmd = build_thumbnail_cmd(final_mp4, thumb_jpg, seek)
        r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
        if not r.ok:
            return b"", None, "Thumbnail extraction failed."

        buf = io.BytesIO()
        with ZipFile(buf, "w", compression=ZIP_DEFLATED) as zf:
            zf.write(final_mp4, arcname="final.mp4")
            zf.write(thumb_jpg, arcname="thumb.jpg")
        return buf.getvalue(), dur, None
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass


def get_max_process_seconds() -> float | None:
    raw = os.environ.get("MAX_PROCESS_SECONDS", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None
