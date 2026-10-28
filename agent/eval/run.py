#!/usr/bin/env python3
"""Run the evaluation set against the agent and record the result.

    python -m agent.eval.run --split dev --tier wiring
    python -m agent.eval.run --split test --tier quality --limit 20

Baseline first, always. The master prompt is explicit that Phase 2 optimization
only begins "once routing accuracy is measurably solid", and without a recorded
baseline an optimizer's gain is unfalsifiable — every compiled program looks
like an improvement if nothing was written down beforehand.

Each run logs its configuration, its six metrics and its exact spend to MLflow,
so two runs are comparable by construction rather than by memory.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import config                                    # noqa: E402
from agent.eval import metrics as M                         # noqa: E402
from agent.eval.dataset import load                         # noqa: E402


def evaluate(program, examples, budget: config.Budget, lm=None,
             tier: str = 'wiring', verbose: bool = False) -> tuple[list[dict], M.RunStats]:
    stats = M.RunStats()
    results: list[dict] = []

    for n, example in enumerate(examples, 1):
        started = time.time()
        try:
            prediction = program(question=example.question)
        except config.BudgetExceeded:
            print(f'\nBUDGET CEILING REACHED after {n - 1} examples — stopping',
                  file=sys.stderr)
            break
        except Exception as exc:                              # noqa: BLE001
            print(f'  [{n}] {type(exc).__name__}: {str(exc)[:120]}', file=sys.stderr)
            results.append({'id': example.id, 'family': example.family,
                            'answer_type': example.answer_type,
                            'factual_accuracy': 0.0,
                            'tool_selection_accuracy': 0.0,
                            'hallucination_rate': 1.0,
                            'retrieval_precision_at_k': None,
                            'score': 0.0, 'error': f'{type(exc).__name__}'})
            continue

        latency = time.time() - started
        prompt_tokens, completion_tokens = config.usage_from(lm) if lm else (0, 0)
        cost = 0.0
        if prompt_tokens or completion_tokens:
            try:
                cost = budget.charge(tier, prompt_tokens, completion_tokens)
            except config.BudgetExceeded as exc:
                print(f'\n{exc}', file=sys.stderr)
                break
        stats.record(latency, cost, prompt_tokens, completion_tokens)

        answer = getattr(prediction, 'answer', '')
        evidence_numbers = getattr(prediction, 'evidence_numbers', set())
        row = {
            'id': example.id,
            'family': example.family,
            'answer_type': example.answer_type,
            'question': example.question,
            'expected': example.answer,
            'got': answer,
            'factual_accuracy': M.factual_accuracy(example, prediction),
            'tool_selection_accuracy': M.tool_selection_accuracy(example, prediction),
            'hallucination_rate': M.hallucination_rate(answer, evidence_numbers),
            'retrieval_precision_at_k': (
                M.retrieval_precision_at_k(getattr(prediction, 'retrieved', []), example)
                if getattr(prediction, 'retrieved', None) else None),
            'unsupported': getattr(prediction, 'unsupported_numbers', []),
            'latency_s': round(latency, 3),
        }
        row['score'] = M.agent_metric(example, prediction)
        results.append(row)

        if verbose or n % 10 == 0:
            # The running mean, not this example's score — a single score here
            # reads like a running average and is not one.
            so_far = sum(r.get('score') or 0 for r in results) / len(results)
            print(f'  [{n}/{len(examples)}] mean {so_far:.3f}  '
                  f'last {row["score"]:.2f}  {example.family:22s} '
                  f'${budget.spent_usd:.4f}', flush=True)

    return results, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--split', default='dev', choices=['train', 'dev', 'test', 'all'])
    ap.add_argument('--tier', default='wiring', choices=sorted(config.TIERS))
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--budget', type=float, default=config.DEFAULT_BUDGET_USD)
    ap.add_argument('--program', default=None,
                    help='path to a compiled program to load')
    ap.add_argument('--run-name', default=None)
    ap.add_argument('--no-mlflow', action='store_true')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    examples = load(split=None if a.split == 'all' else a.split)
    if a.limit:
        examples = examples[:a.limit]

    budget = config.Budget(limit_usd=a.budget)
    lm = config.configure(a.tier, trace=not a.no_mlflow)

    from agent.modules import FootballAnalyst
    program = FootballAnalyst()
    if a.program and os.path.exists(a.program):
        program.load(a.program)
        print(f'loaded compiled program from {a.program}')

    model_id = config.TIERS[a.tier][0]
    print(f'evaluating {len(examples)} examples on {a.split} with {model_id}')
    print(f'budget ceiling ${a.budget:.2f}\n')

    started = time.time()
    results, stats = evaluate(program, examples, budget, lm=lm, tier=a.tier,
                              verbose=a.verbose)
    summary = M.summarise(results, stats)
    summary['tier'] = a.tier
    summary['model'] = model_id
    summary['split'] = a.split
    summary['wall_seconds'] = round(time.time() - started, 1)
    summary.update({f'budget_{k}': v for k, v in budget.summary().items()})

    print('\n' + '=' * 60)
    for key, value in summary.items():
        print(f'  {key:26s} {value}')

    if not a.no_mlflow:
        _log(summary, results, a)
    _save(results, summary, a)
    return 0


def _log(summary: dict, results: list[dict], args):
    try:
        import mlflow
        mlflow.set_tracking_uri(config.MLFLOW_URI)
        mlflow.set_experiment(config.EXPERIMENT)
        name = args.run_name or f'eval-{args.split}-{args.tier}'
        with mlflow.start_run(run_name=name):
            mlflow.log_params({'split': args.split, 'tier': args.tier,
                               'model': config.TIERS[args.tier][0],
                               'examples': len(results),
                               'compiled': bool(args.program)})
            mlflow.log_metrics({k: v for k, v in summary.items()
                                if isinstance(v, (int, float))})
        print(f'\nlogged to MLflow experiment {config.EXPERIMENT!r} as {name!r}')
    except Exception as exc:                                   # noqa: BLE001
        print(f'\ncould not log to MLflow: {type(exc).__name__}: {exc}',
              file=sys.stderr)


def _save(results: list[dict], summary: dict, args):
    import json
    out_dir = os.path.join(ROOT, 'data', 'eval')
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    path = os.path.join(out_dir, f'{args.split}-{args.tier}-{stamp}.json')
    with open(path, 'w') as fh:
        json.dump({'summary': summary, 'results': results}, fh, indent=2, default=str)
    print(f'results written to {path}')


if __name__ == '__main__':
    sys.exit(main())
