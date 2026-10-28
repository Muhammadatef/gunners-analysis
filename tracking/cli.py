"""CLI: python -m tracking.cli ..."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from tracking.paths import PILOT_KICKOFF_OFFSET_S, PILOT_MATCH_ID, PILOT_VIDEO
from tracking.pipeline import run_insights, run_pipeline
from tracking.fuse.report_sections import write_tracking_report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    ap = argparse.ArgumentParser(description='GunnersAnalysis video tracking pipeline')
    ap.add_argument('--match-id', type=int, default=PILOT_MATCH_ID)
    ap.add_argument('--video', type=str, default=PILOT_VIDEO)
    ap.add_argument('--offset-s', type=float, default=PILOT_KICKOFF_OFFSET_S,
                    help='Kickoff in video timeline (GW35 default 453 = 7:33)')
    ap.add_argument('--start-s', type=float, default=None,
                    help='Video time to start (default: offset-s for slice)')
    ap.add_argument('--duration-s', type=float, default=300.0,
                    help='Seconds of play to process (ignored with --full)')
    ap.add_argument('--full', action='store_true',
                    help='Process from kickoff to end of file')
    ap.add_argument('--weights', default='yolov8n.pt')
    ap.add_argument('--device', default=None, help='cuda|cpu (default: try cuda)')
    ap.add_argument('--skip-detect', action='store_true',
                    help='Reuse existing tracks.parquet; recompute insights + report')
    ap.add_argument('--fuse-only', action='store_true',
                    help='Only rewrite TRACKING_REPORT.md from existing artifacts')
    args = ap.parse_args(argv)

    if args.fuse_only:
        from tracking.insights.match_metrics import team_metrics
        from tracking.insights.phases import seed_set_pieces_from_events, segment_phases
        from tracking.insights.player_metrics import player_metrics
        from tracking.store.parquet_io import read_tracks

        tracks = read_tracks(args.match_id)
        insights = {
            'players': player_metrics(tracks),
            'teams': team_metrics(tracks),
            'phases': segment_phases(tracks) + seed_set_pieces_from_events(
                args.match_id, offset_s=args.offset_s),
            'db_ok': None,
            'tracks_path': str(tracks),
        }
        path = write_tracking_report(args.match_id, insights)
        print(f'wrote {path}')
        return 0

    start_s = args.start_s
    if start_s is None and not args.full:
        start_s = args.offset_s

    result = run_pipeline(
        match_id=args.match_id,
        video=Path(args.video),
        kickoff_offset_s=args.offset_s,
        start_s=start_s,
        duration_s=None if args.full else args.duration_s,
        full=args.full,
        weights=args.weights,
        device=args.device,
        skip_detect=args.skip_detect,
    )
    print(f'tracks: {result.get("tracks_path")}')
    print(f'players: {len(result.get("players") or [])}')
    print(f'teams: {len(result.get("teams") or [])}')
    print(f'phases: {len(result.get("phases") or [])}')
    print(f'report: {result.get("report_path")}')
    print(f'db_ok: {result.get("db_ok")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
