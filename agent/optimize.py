#!/usr/bin/env python3
"""Phase 2: compile the program, under a ceiling, and keep it only if better.

    python -m agent.optimize --optimizer bootstrap --budget 2
    python -m agent.optimize --optimizer mipro --budget 5

Three optimizers, in the order the spec sets out — `BootstrapFewShot` for a
baseline lift, `BootstrapFewShotWithRandomSearch` for a broader search, then
`MIPROv2` for instruction and demonstration optimization together.

Everything here runs on the **cheap** tier, never the inference model. That is
the spec's rule and it is also just arithmetic: MIPROv2 issues hundreds to
thousands of calls, and at Sonnet's $3/$15 per million that is the difference
between cents and the whole account.

Two properties matter more than the score:

**The budget is a hard stop.** `Budget.charge` raises before a call that would
cross the ceiling, and nothing catches it. Stopping mid-compile is recoverable;
an empty account is not.

**A new artefact is only kept if it beats the baseline on a split the
optimizer never saw.** An optimizer will always improve its training score —
that is what it is for — and promoting on that number would be measuring the
search, not the program.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import config                                  # noqa: E402
from agent.eval.dataset import load                       # noqa: E402
from agent.eval.metrics import agent_metric, summarise    # noqa: E402
from agent.eval.run import evaluate                       # noqa: E402

ARTEFACTS = os.path.join(ROOT, 'data', 'compiled')


def _as_dspy_examples(examples):
    import dspy
    return [dspy.Example(question=e.question, answer=e.answer,
                         answer_value=e.answer_value,
                         answer_type=e.answer_type,
                         expected_tool=e.expected_tool,
                         family=e.family,
                         query_params=e.query_params).with_inputs('question')
            for e in examples]


def check_requirements(name: str) -> None:
    """Fail before the work, not after it.

    MIPROv2 needs optuna, but it only discovers that at the search step — after
    bootstrapping demonstrations and proposing instructions. That cost sixteen
    minutes of an unattended run to learn one import was missing, so the check
    happens here instead.
    """
    if name == 'mipro':
        try:
            import optuna  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                'MIPROv2 needs optuna, and it will not tell you so until it has '
                'already spent ~15 minutes bootstrapping. Install it first:\n'
                '    pip install "dspy[optuna]"') from exc


def build_optimizer(name: str, metric):
    import dspy
    from dspy.teleprompt import (BootstrapFewShot,
                                 BootstrapFewShotWithRandomSearch, MIPROv2)

    if name == 'bootstrap':
        return BootstrapFewShot(metric=metric, max_bootstrapped_demos=4,
                                max_labeled_demos=8, max_rounds=1)
    if name == 'random_search':
        return BootstrapFewShotWithRandomSearch(
            metric=metric, max_bootstrapped_demos=4, max_labeled_demos=8,
            num_candidate_programs=4)
    if name == 'mipro':
        return MIPROv2(metric=metric, auto='light', num_threads=1)
    raise ValueError(f'unknown optimizer {name!r}; expected bootstrap, '
                     f'random_search or mipro')


def run_optimization(optimizer: str = 'bootstrap', budget_usd: float = 3.0,
                     tier: str = 'cheap', train_limit: int | None = None) -> dict:
    """Compile, evaluate against the baseline, and report whether to promote."""
    import mlflow

    from agent.modules import FootballAnalyst

    check_requirements(optimizer)

    budget = config.Budget(limit_usd=budget_usd)
    lm = config.configure(tier)
    model_id = config.TIERS[tier][0]

    train = load(split='train')
    dev = load(split='dev')
    if train_limit:
        train = train[:train_limit]

    program = FootballAnalyst()

    print(f'optimizer {optimizer} on {model_id}')
    print(f'train {len(train)}  dev {len(dev)}  ceiling ${budget_usd:.2f}\n')

    # Baseline on dev, before anything is compiled.
    print('measuring the baseline ...')
    base_results, base_stats = evaluate(program, dev, budget, lm=lm, tier=tier)
    baseline = summarise(base_results, base_stats)
    print(f'  baseline score {baseline.get("score")}')

    started = time.time()
    compiled = None
    error = None
    try:
        teleprompter = build_optimizer(optimizer, agent_metric)
        compiled = teleprompter.compile(program, trainset=_as_dspy_examples(train))
    except config.BudgetExceeded as exc:
        error = f'budget ceiling reached during compile: {exc}'
        print(f'\n{error}', file=sys.stderr)
    except Exception as exc:                                    # noqa: BLE001
        error = f'{type(exc).__name__}: {exc}'
        print(f'\ncompile failed: {error}', file=sys.stderr)

    optimized = {}
    improved = False
    path = None

    if compiled is not None:
        print('\nmeasuring the compiled program ...')
        opt_results, opt_stats = evaluate(compiled, dev, budget, lm=lm, tier=tier)
        optimized = summarise(opt_results, opt_stats)
        improved = (optimized.get('score') or 0) > (baseline.get('score') or 0)

        if improved:
            os.makedirs(ARTEFACTS, exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S')
            path = os.path.join(ARTEFACTS, f'{optimizer}-{stamp}.json')
            compiled.save(path)
            print(f'saved {path}')
        else:
            print('no improvement on the held-out dev split; not saving')

    result = {
        'optimizer': optimizer, 'model': model_id,
        'baseline': baseline.get('score'), 'optimized': optimized.get('score'),
        'improved': improved, 'path': path, 'error': error,
        'compile_seconds': round(time.time() - started, 1),
        **{f'budget_{k}': v for k, v in budget.summary().items()},
    }

    try:
        mlflow.set_tracking_uri(config.MLFLOW_URI)
        mlflow.set_experiment(config.EXPERIMENT)
        with mlflow.start_run(run_name=f'optimize-{optimizer}'):
            mlflow.log_params({'optimizer': optimizer, 'model': model_id,
                               'train_examples': len(train),
                               'budget_usd': budget_usd})
            mlflow.log_metrics({k: v for k, v in result.items()
                                if isinstance(v, (int, float))})
            for key, value in (optimized or {}).items():
                if isinstance(value, (int, float)):
                    mlflow.log_metric(f'optimized_{key}', value)
    except Exception as exc:                                    # noqa: BLE001
        print(f'MLflow logging failed: {exc}', file=sys.stderr)

    print('\n' + '=' * 60)
    for key, value in result.items():
        print(f'  {key:24s} {value}')
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--optimizer', default='bootstrap',
                    choices=['bootstrap', 'random_search', 'mipro'])
    ap.add_argument('--budget', type=float, default=3.0)
    ap.add_argument('--tier', default='cheap', choices=sorted(config.TIERS))
    ap.add_argument('--train-limit', type=int, default=None)
    a = ap.parse_args(argv)

    result = run_optimization(a.optimizer, a.budget, a.tier, a.train_limit)
    return 0 if not result.get('error') else 1


if __name__ == '__main__':
    sys.exit(main())
