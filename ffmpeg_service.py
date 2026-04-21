"""
FFmpeg pipeline: normalize clips (H.264 + AAC), optional concat, trim, thumbnail.
"""

from __future__ import annotations

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


def _read_stderr_tail(stderr_path: Path, max_chars: int = 4000) -> str:
    try:
        raw = stderr_path.read_bytes()
        return _stderr_tail(raw.decode(errors="replace"), max_chars=max_chars)
    except OSError:
        return ""


def run_ffmpeg(
    args: list[str],
    *,
    logger: logging.Logger,
    request_id: str,
    timeout_sec: float | None,
) -> FFResult:
    """
    Run ffmpeg; stderr streamed to a temp file (avoids buffering huge stderr in RAM).
    Logs exit code always; on failure logs stderr tail from file.
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

    fd, stderr_str = tempfile.mkstemp(prefix="ffw_stderr_", suffix=".log")
    stderr_path = Path(stderr_str)
    os.close(fd)
    proc = None
    try:
        try:
            with stderr_path.open("wb") as err_f:
                proc = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=err_f,
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
        except subprocess.TimeoutExpired:
            tail = _read_stderr_tail(stderr_path)
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
            logger.info(
                json.dumps(
                    {
                        "event": "ffmpeg_exit",
                        "request_id": request_id,
                        "exit_code": None,
                        "timed_out": True,
                    }
                )
            )
            return FFResult(ok=False, exit_code=None, stderr_tail=tail or "ffmpeg timeout")

        exit_code = proc.returncode
        logger.info(
            json.dumps(
                {
                    "event": "ffmpeg_exit",
                    "request_id": request_id,
                    "exit_code": exit_code,
                }
            )
        )

        if exit_code != 0:
            tail = _read_stderr_tail(stderr_path)
            logger.error(
                json.dumps(
                    {
                        "event": "ffmpeg_error",
                        "request_id": request_id,
                        "exit_code": exit_code,
                        "cmd": cmd_str,
                        "stderr_tail": tail,
                    }
                )
            )
            return FFResult(ok=False, exit_code=exit_code, stderr_tail=tail)

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
    finally:
        try:
            stderr_path.unlink(missing_ok=True)
        except OSError:
            pass


def shlex_quote(s: str) -> str:
    """Minimal safe quoting for log lines."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "._/-:" for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def get_ffmpeg_preset() -> str:
    v = os.environ.get("FFMPEG_PRESET", "medium").strip()
    return v if v else "medium"


def get_max_output_width() -> int | None:
    raw = os.environ.get("MAX_OUTPUT_WIDTH", "").strip()
    if not raw:
        return None
    try:
        w = int(raw)
        return w if w > 0 else None
    except ValueError:
        return None


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
    fd_out, out_path_str = tempfile.mkstemp(prefix="ffw_ffprobe_", suffix=".json")
    fd_err, err_path_str = tempfile.mkstemp(prefix="ffw_ffprobe_", suffix=".err")
    os.close(fd_out)
    os.close(fd_err)
    out_path = Path(out_path_str)
    err_path = Path(err_path_str)
    try:
        try:
            with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
                proc = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                    timeout=60,
                )
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
                        "stderr_tail": _read_stderr_tail(err_path),
                    }
                )
            )
            return {}
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
            return json.loads(text or "{}")
        except json.JSONDecodeError:
            return {}
    finally:
        try:
            out_path.unlink(missing_ok=True)
            err_path.unlink(missing_ok=True)
        except OSError:
            pass


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


def _normalize_vf_first_clip(max_w: int | None) -> str:
    """Video filter for first clip: optional max width, even dimensions, yuv420p."""
    parts = ["fps=30"]
    if max_w is not None:
        parts.append(
            f"scale=min(iw\\,{max_w}):-2:force_original_aspect_ratio=decrease"
        )
    parts.extend(["scale=trunc(iw/2)*2:trunc(ih/2)*2", "format=yuv420p"])
    return ",".join(parts)


def build_normalize_cmd_1(
    input_path: Path,
    output_path: Path,
    *,
    target_w: int | None,
    target_h: int | None,
    preset: str,
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
        vf = _normalize_vf_first_clip(get_max_output_width())

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
        preset,
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
    *,
    preset: str,
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
        preset,
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
    *,
    preset: str,
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
        preset,
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
) -> tuple[Path | None, float | None, str | None]:
    """
    Returns (path to temp zip file on disk, final_duration_seconds or None, error_message or None).
    Caller must delete the zip path after sending (e.g. BackgroundTask).
    """
    work = Path(tempfile.mkdtemp(prefix="ffw_"))
    final_mp4 = work / "final.mp4"
    thumb_jpg = work / "thumb.jpg"
    n1 = work / "normalized1.mp4"
    n2 = work / "normalized2.mp4"
    concat_out = work / "concat.mp4"
    preset = get_ffmpeg_preset()

    fd_zip, zip_str = tempfile.mkstemp(prefix="ffw_out_", suffix=".zip")
    os.close(fd_zip)
    zip_path = Path(zip_str)

    try:
        cmd = build_normalize_cmd_1(
            clip1_path, n1, target_w=None, target_h=None, preset=preset
        )
        r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
        if not r.ok:
            zip_path.unlink(missing_ok=True)
            return None, None, "Video processing failed."

        meta1 = ffprobe_json(n1, logger, request_id)
        size1 = probe_video_size(meta1)
        if not size1:
            zip_path.unlink(missing_ok=True)
            return None, None, "Could not read video dimensions after normalize."

        w1, h1 = size1

        if clip2_path is None:
            pre_final = n1
        else:
            cmd = build_normalize_cmd_1(
                clip2_path, n2, target_w=w1, target_h=h1, preset=preset
            )
            r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
            if not r.ok:
                zip_path.unlink(missing_ok=True)
                return None, None, "Video processing failed."

            cmd = build_concat_cmd(n1, n2, concat_out, preset=preset)
            r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
            if not r.ok:
                zip_path.unlink(missing_ok=True)
                return None, None, "Video processing failed."
            pre_final = concat_out

        cmd = build_final_trim_cmd(
            pre_final, final_mp4, target_seconds, preset=preset
        )
        r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
        if not r.ok:
            zip_path.unlink(missing_ok=True)
            return None, None, "Video processing failed."

        final_meta = ffprobe_json(final_mp4, logger, request_id)
        dur = probe_duration_seconds(final_meta)
        seek = pick_thumbnail_seek(dur)

        cmd = build_thumbnail_cmd(final_mp4, thumb_jpg, seek)
        r = run_ffmpeg(cmd, logger=logger, request_id=request_id, timeout_sec=ffmpeg_timeout_sec)
        if not r.ok:
            zip_path.unlink(missing_ok=True)
            return None, None, "Thumbnail extraction failed."

        with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
            zf.write(final_mp4, arcname="final.mp4")
            zf.write(thumb_jpg, arcname="thumb.jpg")

        return zip_path, dur, None
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
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


def get_max_download_bytes() -> int:
    """Per-request byte cap for URL downloads; falls back to MAX_UPLOAD_MB."""
    raw = os.environ.get("MAX_DOWNLOAD_MB", "").strip()
    if raw:
        try:
            mb = int(raw)
            return max(0, mb) * 1024 * 1024
        except ValueError:
            pass
    max_mb = int(os.environ.get("MAX_UPLOAD_MB", "200"))
    return max(0, max_mb) * 1024 * 1024
