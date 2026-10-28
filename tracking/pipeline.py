"""Importable tracking pipeline stages."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import cv2

from tracking.cv.detector import detect_frame, load_model
from tracking.cv.homography import feet_point, load_or_default, pixel_to_pitch
from tracking.cv.teams import assign_teams
from tracking.cv.tracker import ball_centroid, make_byte_tracker, update_tracks
from tracking.insights.match_metrics import team_metrics
from tracking.insights.phases import seed_set_pieces_from_events, segment_phases
from tracking.insights.player_metrics import player_metrics
from tracking.paths import (
    PILOT_KICKOFF_OFFSET_S,
    PILOT_MATCH_ID,
    match_dir,
    tracks_path,
)
from tracking.probe import ensure_proxy, load_meta, probe_video, write_meta
from tracking.store.load_db import (
    replace_phases,
    replace_player_match,
    replace_team_match,
    upsert_job,
    utcnow,
)
from tracking.store.parquet_io import read_tracks, write_phases, write_tracks

logger = logging.getLogger(__name__)


def run_probe(
    match_id: int,
    video: Path,
    *,
    kickoff_offset_s: float = PILOT_KICKOFF_OFFSET_S,
    start_s: float | None = None,
    duration_s: float | None = None,
    model: str = 'yolov8n.pt',
) -> dict[str, Any]:
    match_dir(match_id)
    info = probe_video(video)
    meta = {
        **info,
        'match_id': match_id,
        'kickoff_offset_s': kickoff_offset_s,
        'start_s': start_s,
        'duration_s': duration_s,
        'model': model,
    }
    write_meta(match_id, meta)
    upsert_job(
        match_id,
        status='probed',
        video_path=str(video),
        model=model,
        kickoff_offset_s=kickoff_offset_s,
        start_s=start_s,
        duration_s=duration_s,
        meta_path=str(match_dir(match_id) / 'meta.json'),
        started_at=utcnow(),
    )
    return meta


def run_proxy(
    match_id: int,
    video: Path,
    *,
    start_s: float | None = None,
    duration_s: float | None = None,
    width: int = 1280,
) -> Path:
    path = ensure_proxy(
        match_id, video, width=width, start_s=start_s, duration_s=duration_s)
    meta = load_meta(match_id)
    meta['proxy_path'] = str(path)
    write_meta(match_id, meta)
    return path


def run_detect_track(
    match_id: int,
    video: Path,
    *,
    kickoff_offset_s: float = PILOT_KICKOFF_OFFSET_S,
    start_s: float | None = None,
    duration_s: float | None = None,
    stride: int = 5,
    imgsz: int = 640,
    weights: str = 'yolov8n.pt',
    device: str | None = None,
    home_team: str = 'Arsenal',
    away_team: str = 'Fulham',
    use_proxy: bool = True,
) -> Path:
    """Detect + track a window; write tracks.parquet with pitch coords."""
    upsert_job(match_id, status='tracking', started_at=utcnow())
    src = video
    video_time_base = 0.0
    if use_proxy:
        import shutil
        if shutil.which('ffmpeg'):
            if start_s is not None or duration_s is not None:
                src = run_proxy(
                    match_id, video, start_s=start_s, duration_s=duration_s)
                video_time_base = float(start_s or 0.0)
            else:
                src = run_proxy(match_id, video)
        else:
            # OpenCV re-encode of a multi-GB MKV is too slow; seek the source.
            logger.warning(
                'ffmpeg missing — skipping proxy; seeking source with stride')
            if start_s is not None:
                video_time_base = float(start_s)

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f'cannot open {src}')
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)

    # If reading the full source without proxy slice, seek to start_s
    if src == video and start_s:
        cap.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000.0)
        video_time_base = float(start_s)

    try:
        model = load_model(weights, device=device or 'cuda')
    except Exception:  # noqa: BLE001
        logger.warning('CUDA load failed; using CPU')
        model = load_model(weights, device='cpu')

    tracker = make_byte_tracker(frame_rate=max(fps / stride, 1.0))
    M = load_or_default(match_id, width, height)

    rows: list[dict] = []
    frame_i = 0
    end_limit = None
    if duration_s is not None and src == video:
        end_limit = video_time_base + duration_s

    team_cache: dict[int, str | None] = {}

    while True:
        if frame_i % stride != 0:
            # Decode-skip: grab without full decode when possible
            if not cap.grab():
                break
            frame_i += 1
            continue

        ok, frame = cap.read()
        if not ok:
            break
        # Time in original video timeline
        if src == video:
            t_video = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        else:
            t_video = video_time_base + (frame_i / fps)
        if end_limit is not None and t_video > end_limit:
            break

        try:
            dets = detect_frame(model, frame, imgsz=imgsz)
        except Exception as exc:  # noqa: BLE001
            logger.warning('detect failed on frame %s (%s); falling back to CPU', frame_i, exc)
            model = load_model(weights, device='cpu')
            dets = detect_frame(model, frame, imgsz=imgsz)

        tracked = update_tracks(tracker, dets, frame.shape[:2])
        frame_tracks: list[dict] = []
        if tracked is not None and len(tracked) > 0:
            for i in range(len(tracked)):
                xyxy = tracked.xyxy[i].tolist()
                tid = int(tracked.tracker_id[i]) if tracked.tracker_id is not None else i
                conf = float(tracked.confidence[i]) if tracked.confidence is not None else 0.0
                fx, fy = feet_point(xyxy)
                xm, ym = pixel_to_pitch(M, fx, fy)
                frame_tracks.append({
                    'track_id': tid,
                    'role': 'player',
                    'xyxy': xyxy,
                    'conf': conf,
                    'x_px': fx,
                    'y_px': fy,
                    'x_m': xm,
                    'y_m': ym,
                    'team': team_cache.get(tid),
                })

        # Refresh team colours every ~2s of source time
        if frame_tracks and (frame_i // stride) % max(int(fps / stride), 1) == 0:
            assign_teams(frame, frame_tracks, home_name=home_team, away_name=away_team)
            for t in frame_tracks:
                if t.get('team'):
                    team_cache[t['track_id']] = t['team']
        else:
            for t in frame_tracks:
                t['team'] = team_cache.get(t['track_id'])

        t_match = t_video - kickoff_offset_s
        for t in frame_tracks:
            rows.append({
                't_video_s': round(t_video, 3),
                't_match_s': round(t_match, 3),
                'track_id': t['track_id'],
                'team': t.get('team'),
                'role': 'player',
                'x_px': t['x_px'],
                'y_px': t['y_px'],
                'x_m': t['x_m'],
                'y_m': t['y_m'],
                'conf': t['conf'],
            })

        bc = ball_centroid(dets)
        if bc is not None:
            xm, ym = pixel_to_pitch(M, bc[0], bc[1])
            rows.append({
                't_video_s': round(t_video, 3),
                't_match_s': round(t_match, 3),
                'track_id': -1,
                'team': None,
                'role': 'ball',
                'x_px': bc[0],
                'y_px': bc[1],
                'x_m': xm,
                'y_m': ym,
                'conf': 1.0,
            })

        frame_i += 1
        if frame_i % (stride * 50) == 0:
            logger.info('processed frame %s (t_video=%.1fs, rows=%s)', frame_i, t_video, len(rows))

    cap.release()
    out = write_tracks(match_id, rows)
    meta = load_meta(match_id)
    meta['tracks_path'] = str(out)
    meta['track_rows'] = len(rows)
    write_meta(match_id, meta)
    upsert_job(match_id, status='tracked', tracks_path=str(out))
    return out


def run_insights(match_id: int, *, kickoff_offset_s: float = PILOT_KICKOFF_OFFSET_S) -> dict[str, Any]:
    tracks = read_tracks(match_id)
    players = player_metrics(tracks)
    teams = team_metrics(tracks)
    phases = segment_phases(tracks)
    phases.extend(seed_set_pieces_from_events(match_id, offset_s=kickoff_offset_s))
    write_phases(match_id, phases)
    try:
        replace_player_match(match_id, players)
        replace_team_match(match_id, teams)
        replace_phases(match_id, phases)
        upsert_job(match_id, status='loaded', finished_at=utcnow())
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning('DB load failed (parquet still written): %s', exc)
        upsert_job(match_id, status='tracked_no_db', error=str(exc))
        db_ok = False
    return {
        'players': players,
        'teams': teams,
        'phases': phases,
        'db_ok': db_ok,
        'tracks_path': str(tracks_path(match_id)),
    }


def run_pipeline(
    match_id: int = PILOT_MATCH_ID,
    video: Path | None = None,
    *,
    kickoff_offset_s: float = PILOT_KICKOFF_OFFSET_S,
    start_s: float | None = 453.0,
    duration_s: float | None = 300.0,
    full: bool = False,
    weights: str = 'yolov8n.pt',
    device: str | None = None,
    skip_detect: bool = False,
) -> dict[str, Any]:
    if video is None:
        raise ValueError('video path required')
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)

    if full:
        start_s = kickoff_offset_s
        duration_s = None

    run_probe(
        match_id, video,
        kickoff_offset_s=kickoff_offset_s,
        start_s=start_s,
        duration_s=duration_s,
        model=weights,
    )
    if not skip_detect:
        run_detect_track(
            match_id, video,
            kickoff_offset_s=kickoff_offset_s,
            start_s=start_s,
            duration_s=duration_s,
            weights=weights,
            device=device,
        )
    insights = run_insights(match_id, kickoff_offset_s=kickoff_offset_s)
    from tracking.fuse.report_sections import write_tracking_report
    report = write_tracking_report(match_id, insights)
    insights['report_path'] = str(report)
    return insights
