#!/usr/bin/env python3
"""CLI wrapper around the data quality gate.

    python quality/validate.py --job <job_id> [--root .]
    python quality/validate.py --events <csv> --players <csv> --output <json>

Exits non-zero if any error-severity check fails, so the pipeline stops rather
than producing an empty dashboard. (This file used to be a stub that always
reported PASS, which is why broken transforms sailed through.)

`FootballDataValidator` is re-exported from `validate_strict` for the pandera
schema tests; the runtime gate is `quality.checks`.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quality.checks import run_checks, validate_job          # noqa: E402,F401
from quality.validate_strict import FootballDataValidator    # noqa: E402,F401

__all__ = ['FootballDataValidator', 'run_checks', 'validate_job', 'main']


def main() -> int:
    ap = argparse.ArgumentParser(description='Validate a transformed match')
    ap.add_argument('--job', help='processed job id')
    ap.add_argument('--root', default=os.getenv('GUNNERS_ROOT', '.'))
    ap.add_argument('--events', help='EventData.csv (when not using --job)')
    ap.add_argument('--players', help='PlayerData.csv (when not using --job)')
    ap.add_argument('--output', help='where to write the report')
    args = ap.parse_args()

    if args.job:
        report = validate_job(args.job, args.root)
    elif args.events and args.players:
        import pandas as pd
        report = run_checks(pd.read_csv(args.events), pd.read_csv(args.players))
        if args.output:
            os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
            with open(args.output, 'w') as fh:
                json.dump(report, fh, indent=2)
    else:
        ap.error('pass --job, or both --events and --players')

    print(f'{report["status"]}: {report["summary"]}')
    for check in report['checks']:
        if not check['passed']:
            print(f'  [{check["severity"]}] {check["name"]}: {check["detail"]}')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
