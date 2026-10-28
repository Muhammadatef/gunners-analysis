"""Probe video metadata and optional 720p proxy for MX350-safe inference."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import cv2

from tracking.paths import meta_path, proxy_path

logger = logging.getLogger(__name__)


def file_sha256(path: Path, limit_mb: int = 64) -> str:
    """Hash first `limit_mb` of the file (full hash of 5GB is unnecessary)."""
    h = hashlib.sha256()
    cap = limit_mb * 1024 * 1024
    with open(path, 'rb') as f:
        remaining = cap
        while remaining > 0:
            chunk = f.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def probe_video(video: Path) -> dict[str, Any]:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f'cannot open video: {video}')
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration_s = frames / fps if fps > 0 else 0.0
    cap.release()
    return {
        'path': str(video),
        'fps': fps,
        'frame_count': frames,
        'width': width,
        'height': height,
        'duration_s': round(duration_s, 3),
        'size_bytes': video.stat().st_size if video.is_file() else None,
        'sha256_prefix': file_sha256(video) if video.is_file() else None,
    }


def write_meta(match_id: int, meta: dict[str, Any]) -> Path:
    path = meta_path(match_id)
    path.write_text(json.dumps(meta, indent=2, default=str) + '\n', encoding='utf-8')
    return path


def load_meta(match_id: int) -> dict[str, Any]:
    path = meta_path(match_id)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def ensure_proxy(
    match_id: int,
    video: Path,
    *,
    width: int = 1280,
    start_s: float | None = None,
    duration_s: float | None = None,
    force: bool = False,
) -> Path:
    """Build a narrower H.264 proxy with ffmpeg when available, else OpenCV."""
    out = proxy_path(match_id)
    if start_s is not None or duration_s is not None:
        stem = f'proxy_{int(start_s or 0)}_{int(duration_s or 0)}.mp4'
        out = proxy_path(match_id).parent / stem

    if out.is_file() and not force:
        return out

    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error']
        if start_s is not None:
            cmd += ['-ss', str(start_s)]
        cmd += ['-i', str(video)]
        if duration_s is not None:
            cmd += ['-t', str(duration_s)]
        cmd += [
            '-vf', f'scale={width}:-2',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28',
            '-an', str(out),
        ]
        logger.info('ffmpeg proxy -> %s', out)
        subprocess.run(cmd, check=True)
        return out

    logger.warning('ffmpeg not found; writing proxy with OpenCV (slower)')
    return _opencv_proxy(video, out, width=width, start_s=start_s, duration_s=duration_s)


def _opencv_proxy(
    video: Path,
    out: Path,
    *,
    width: int,
    start_s: float | None,
    duration_s: float | None,
) -> Path:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f'cannot open {video}')
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or width)
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or int(width * 9 / 16))
    scale = width / max(src_w, 1)
    height = max(2, int(src_h * scale) // 2 * 2)
    if start_s:
        cap.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000.0)
    writer = cv2.VideoWriter(
        str(out),
        cv2.VideoWriter_fourcc(*'mp4v'),
        fps,
        (width, height),
    )
    end_s = None if duration_s is None else (start_s or 0.0) + duration_s
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if end_s is not None and t > end_s + 0.05:
            break
        resized = cv2.resize(frame, (width, height))
        writer.write(resized)
    writer.release()
    cap.release()
    return out
