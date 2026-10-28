#!/usr/bin/env python3
"""Run the full pipeline for one match, from two links.

    python orchestrator/run_match.py \
      --club barcelona \
      --whoscored 'https://www.whoscored.com/matches/1234567/live/...' \
      --sofascore 'https://www.sofascore.com/football/match/.../xyz' \
      --season '2025/2026' --competition 'Champions League'

WhoScored carries the event stream that the 18 panels are built from but no xG
at all; SofaScore carries xG and xGOT but no pass-by-pass feed. Passing both
links is what produces a complete report.

Each club gets its own warehouse schema and its own output directory, so
`gunners` stays Arsenal's alone:

    barcelona.matches, barcelona.events, ...
    output/barcelona/2025-2026/mw06_2025-11-26_barcelona_v_chelsea/

Runs synchronously and exits non-zero on failure - see the API's
POST /pipeline/run for the fire-and-forget version.
"""

import argparse
import logging
import os
import sys
import uuid

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.connection import DEFAULT_SCHEMA, club_schema, ensure_schema, use_schema  # noqa: E402
from orchestrator import pipeline                                                 # noqa: E402
from scraper.scraper import BROWSER_MODES                                         # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description='Scrape, align with SofaScore, render and load one match',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # a Champions League night, into the barcelona schema
  python orchestrator/run_match.py --club barcelona \\
    --whoscored 'https://www.whoscored.com/matches/.../live/...' \\
    --sofascore 'https://www.sofascore.com/football/match/.../xyz' \\
    --season '2025/2026' --competition 'Champions League' --matchweek 6

  # an Egyptian Premier League match, sparser feed, custom colours
  python orchestrator/run_match.py --club al_ahly \\
    --whoscored '...' --sofascore '...' --season '2025/2026' \\
    --competition 'Egyptian Premier League' --min-events 500 \\
    --home-color '#D50000' --away-color '#004C99'
""")

    ap.add_argument('--club', required=True,
                    help="club whose schema and output dir this match belongs to "
                         "('barcelona', 'AC Milan' -> ac_milan)")
    ap.add_argument('--whoscored', required=True, help='WhoScored match URL')
    ap.add_argument('--sofascore', default=None,
                    help='SofaScore match URL - the xG source, required unless --no-xg')
    ap.add_argument('--no-xg', action='store_true',
                    help='run without SofaScore; xG panels render 0.0 placeholders')

    ap.add_argument('--season', default=os.getenv('GUNNERS_SEASON', '2026/2027'))
    ap.add_argument('--competition', default=None,
                    help="competition name, e.g. 'Champions League'. WhoScored's "
                         f"feed does not carry it; defaults to {pipeline.DEFAULT_COMPETITION!r}")
    ap.add_argument('--matchweek', type=int, default=None)
    ap.add_argument('--fixture-key', default=None,
                    help='link this match to a seeded calendar row (Arsenal only)')

    ap.add_argument('--job', default=None, help='job id (default: a fresh uuid4)')
    ap.add_argument('--no-load', action='store_true', help='render only, skip the warehouse')
    ap.add_argument('--output-root', default=None, help='root of the season layout')
    ap.add_argument('--dpi', type=int, default=150)
    ap.add_argument('--theme', choices=['classic', 'ucl'], default='classic',
                    help="panel palette; 'ucl' is the Champions League night look")

    ap.add_argument('--mode', choices=BROWSER_MODES, default=None,
                    help='browser mode (default: headed if a display exists, else xvfb)')
    ap.add_argument('--wait', type=int, default=15,
                    help='seconds to poll for the WhoScored match payload')
    ap.add_argument('--min-events', type=int, default=None,
                    help='relax the quality gate for a sparser feed (default 800)')

    ap.add_argument('--home-color', default=None, help="home club hex, e.g. '#A50044'")
    ap.add_argument('--away-color', default=None, help='away club hex')
    ap.add_argument('--home-xg', type=float, default=None,
                    help='manual xG override; SofaScore fills these in otherwise')
    ap.add_argument('--away-xg', type=float, default=None)
    ap.add_argument('--home-xgot', type=float, default=None)
    ap.add_argument('--away-xgot', type=float, default=None)

    ap.add_argument('-v', '--verbose', action='store_true')
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s  %(levelname)-7s %(message)s',
                        datefmt='%H:%M:%S')

    if not args.sofascore and not args.no_xg:
        print('--sofascore is required (or pass --no-xg to accept 0.0 xG panels).\n'
              'WhoScored carries no xG at all, so without it the shot map and '
              'goalkeeper panels are placeholders.', file=sys.stderr)
        return 2

    if args.mode:
        os.environ['GUNNERS_BROWSER_MODE'] = args.mode

    # Fail on a bad club name before spending a minute in the browser.
    try:
        schema = club_schema(args.club)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    job_id = args.job or str(uuid.uuid4())
    xg = {k: v for k, v in {
        'home_xg': args.home_xg, 'away_xg': args.away_xg,
        'home_xgot': args.home_xgot, 'away_xgot': args.away_xgot,
    }.items() if v is not None}
    colors = {k: v for k, v in {
        'home_color': args.home_color, 'away_color': args.away_color,
    }.items() if v}

    print(f'job    {job_id}\nclub   {args.club} -> schema {schema}\n'
          f'season {args.season}'
          + (f'  ({args.competition})' if args.competition else ''))

    if not args.no_load:
        ensure_schema(schema)

    try:
        with use_schema(schema):
            result = pipeline.run_match(
                job_id, args.whoscored, args.season,
                fixture_key=args.fixture_key,
                sofascore_url=args.sofascore,
                club=schema,
                competition=args.competition,
                matchweek=args.matchweek,
                xg=xg or None,
                colors=colors or None,
                load_to_warehouse=not args.no_load,
                output_root=args.output_root,
                min_events=args.min_events,
                dpi=args.dpi,
                wait_time=args.wait,
                theme=args.theme,
            )
    except Exception as exc:                                         # noqa: BLE001
        print(f'\nFAILED: {exc}', file=sys.stderr)
        return 1

    print('\n' + '-' * 60)
    print(f'source     {result["source"]}'
          + (f'  ({result["whoscored_error"].splitlines()[0]})'
             if result.get('whoscored_error') else ''))
    print(f'schema     {result.get("schema", schema)}')
    print(f'output     {result.get("match_dir") or result.get("out_dir")}')
    if result.get('xg_source'):
        home_xg, away_xg = result.get('xg') or (None, None)
        print(f'xG         {home_xg} - {away_xg}   [{result["xg_source"]}]')
    if result.get('match_id'):
        print(f'match_id   {result["match_id"]}')
    if result.get('quality'):
        print(f'quality    {result["quality"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
