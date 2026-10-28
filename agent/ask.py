#!/usr/bin/env python3
"""Ask the analyst a question.

    python -m agent.ask "Was Arsenal a high-pressing side in 2025/26?"
    python -m agent.ask --tier quality --show-evidence "How many points?"

The interactive counterpart to `agent.eval.run`. It prints the answer, and --
by default -- the provenance behind it: which query ran, with what parameters,
and whether any number in the answer went unvouched for.

That last line is the one to read. `unsupported` lists figures that appear in
the prose but in none of the evidence. It should always be empty. When it is
not, the answer is not to be trusted, and the design says so out loud rather
than letting it pass.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('question', nargs='+')
    ap.add_argument('--tier', default=config.DEFAULT_TIER, choices=sorted(config.TIERS))
    ap.add_argument('--club', default=None)
    ap.add_argument('--program', default=None, help='a compiled program to load')
    ap.add_argument('--show-evidence', action='store_true')
    ap.add_argument('--no-mlflow', action='store_true')
    a = ap.parse_args(argv)

    question = ' '.join(a.question)
    lm = config.configure(a.tier, trace=not a.no_mlflow)

    from agent.modules import FootballAnalyst
    from db.connection import club_schema

    schema = club_schema(a.club) if a.club else None
    program = FootballAnalyst(schema=schema)
    if a.program and os.path.exists(a.program):
        program.load(a.program)

    prediction = program(question=question)

    print(f'\n{prediction.answer}\n')
    print('-' * 60)
    print(f'route      {prediction.tool}'
          + (f' -> {prediction.query_id}{prediction.parameters}'
             if prediction.query_id else ''))
    print(f'tools      {", ".join(prediction.tools_used) or "none"}')
    print(f'latency    {prediction.latency_s}s')
    if prediction.errors:
        print(f'errors     {prediction.errors}')

    unsupported = prediction.unsupported_numbers
    if unsupported:
        print(f'unsupported  {unsupported}   <-- these figures are in the answer '
              f'but not in the evidence')
    else:
        print('unsupported  none — every figure traces to the evidence')

    if a.show_evidence:
        print('\nevidence:')
        for line in prediction.evidence.splitlines():
            print(f'  {line}')

    _, _ = config.usage_from(lm)
    return 0


if __name__ == '__main__':
    sys.exit(main())
