"""The metrics. Without these the optimizer has nothing to optimize.

Six, as the spec names them. Each is deliberately mechanical — a metric a human
has to adjudicate cannot be run a thousand times inside MIPROv2.

    factual_accuracy        did the number match the warehouse
    retrieval_precision_at_k were the retrieved documents relevant
    tool_selection_accuracy  did it reach for the right tool
    hallucination_rate       did it state a number nothing vouched for
    cost_per_query           dollars
    p95_latency              seconds

`hallucination_rate` is the one worth reading closely. It does not ask a model
whether the answer looks truthful. It extracts every number from the answer
text and checks each against the set of numbers the evidence actually returned.
A figure that appears in the prose but not in the evidence is a fabrication, and
that is decidable rather than debatable — which is exactly what makes "never
hallucinates" a property rather than a hope.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Numbers that are almost never a claim about football: years, the small
# integers that show up in ordinary prose ("the first half", "one of two"),
# and 90 — "per 90 minutes" is the unit every rate in the sport is quoted in,
# so treating it as an asserted statistic flags an idiom as a fabrication.
#
# Note this list is for *idioms*, not for convenience. A derived figure like
# "1.94 progressive passes per 90" is exactly what should be caught: the model
# calculated it, no tool returned it, and the first hard rule says arithmetic
# is not its job.
_IGNORE_IN_PROSE = {0.0, 1.0, 2.0, 90.0, 2025.0, 2026.0, 2027.0}

# The comma-grouped alternative requires at least one group, or `\d{1,3}` wins
# on a bare number and chops it: "2025" became "202" and "5", and both were
# then reported as fabricated figures in an answer that never contained them.
_NUMBER_RE = re.compile(r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?')


def numbers_in(text: str) -> set[float]:
    """Every number stated in a piece of prose, normalised."""
    # Expand season shorthand (2025/26 → 2025/2026) so the trailing two-digit
    # year is recognised as a year and ignored by the hallucination gate,
    # rather than counted as an unsupported statistic.
    prose = re.sub(
        r'\b(\d{4})\s*[/-]\s*(\d{2})\b',
        lambda m: f'{m.group(1)}/{m.group(1)[:2]}{m.group(2)}',
        text or '',
    )
    found = set()
    for token in _NUMBER_RE.findall(prose):
        try:
            found.add(round(float(token.replace(',', '')), 4))
        except ValueError:
            continue
    return found


def _close(a: float, b: float, tolerance: float = 0.02) -> bool:
    """Equal to within rounding. A model saying 10.8 for 10.76 is not lying."""
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) / scale <= tolerance


# ---------------------------------------------------------------------------
# 1. factual accuracy
# ---------------------------------------------------------------------------

def factual_accuracy(example, prediction) -> float:
    """1.0 if the answer states the right value, else 0.0.

    Graded against the value, not the wording — an answer is free to say
    "85 points" or "they finished on 85" so long as 85 is in there.
    """
    expected = getattr(example, 'answer_value', None)
    answer = _answer_text(prediction)

    if getattr(example, 'answer_type', None) == 'refusal':
        return 1.0 if refused(answer) else 0.0

    if expected is None:
        # Nothing numeric to check (an open tactical question); fall back to
        # whether it produced a substantive answer at all.
        return 1.0 if answer and len(answer.split()) >= 8 else 0.0

    stated = numbers_in(answer)

    if isinstance(expected, (list, tuple)):
        return 1.0 if all(
            any(_close(float(v), s) for s in stated) for v in expected
            if isinstance(v, (int, float))) else 0.0

    if isinstance(expected, (int, float)):
        return 1.0 if any(_close(float(expected), s) for s in stated) else 0.0

    return 1.0 if str(expected).lower() in answer.lower() else 0.0


REFUSAL_MARKERS = (
    'cannot be answered', 'not in this warehouse', 'no data', 'does not hold',
    'insufficient evidence', 'cannot answer', 'not available', 'do not have',
    "don't have", 'unable to answer', 'not populated', 'no evidence',
)


def refused(answer: str) -> bool:
    low = (answer or '').lower()
    return any(marker in low for marker in REFUSAL_MARKERS)


# ---------------------------------------------------------------------------
# 2. retrieval precision@k
# ---------------------------------------------------------------------------

def retrieval_precision_at_k(retrieved: Sequence[dict], example, k: int = 5) -> float:
    """Share of the top k documents that are relevant to the question.

    Relevance is judged structurally, not by a model: a document is relevant if
    it concerns the match, opponent, player or phase the question is about.
    Crude, but honest and repeatable — and a model grading its own retrieval is
    a well-known way to get a flattering number.
    """
    if not retrieved:
        return 0.0
    top = list(retrieved)[:k]

    wanted = {str(v).lower() for v in getattr(example, 'query_params', {}).values()
              if isinstance(v, (str, int)) and str(v)}
    question_words = set(re.findall(r'[a-z]{4,}', example.question.lower()))

    hits = 0
    for doc in top:
        text = ((doc.get('content') or '') + ' ' + str(doc.get('opponent') or '')
                + ' ' + str(doc.get('doc_type') or '')).lower()
        if wanted and any(w in text for w in wanted):
            hits += 1
        elif len(question_words & set(re.findall(r'[a-z]{4,}', text))) >= 3:
            hits += 1
    return hits / len(top)


# ---------------------------------------------------------------------------
# 3. tool selection
# ---------------------------------------------------------------------------

def tool_selection_accuracy(example, prediction) -> float:
    """Did the agent reach for the tool the question needed?"""
    expected = getattr(example, 'expected_tool', None)
    if not expected:
        return 1.0
    used = _tools_used(prediction)
    if expected == 'none':
        # A question that should be refused should touch no tool at all.
        return 1.0 if not used else 0.0
    return 1.0 if expected in used else 0.0


def _tools_used(prediction) -> set[str]:
    used = getattr(prediction, 'tools_used', None)
    if used is None:
        used = getattr(prediction, 'tool', None)
    if used is None:
        return set()
    if isinstance(used, str):
        return {used}
    return set(used)


# ---------------------------------------------------------------------------
# 4. hallucination rate
# ---------------------------------------------------------------------------

def hallucinated_numbers(answer: str, evidence_numbers: Iterable[float]) -> set[float]:
    """Numbers asserted in the answer that no evidence vouched for."""
    vouched = {round(float(n), 4) for n in evidence_numbers}
    stated = numbers_in(answer) - _IGNORE_IN_PROSE
    return {n for n in stated
            if not any(_close(n, v) for v in vouched)}


def hallucination_rate(answer: str, evidence_numbers: Iterable[float]) -> float:
    """Share of stated numbers that are unsupported. 0.0 is the target."""
    stated = numbers_in(answer) - _IGNORE_IN_PROSE
    if not stated:
        return 0.0
    return len(hallucinated_numbers(answer, evidence_numbers)) / len(stated)


# ---------------------------------------------------------------------------
# 5 & 6. cost and latency
# ---------------------------------------------------------------------------

@dataclass
class RunStats:
    """What one evaluation pass cost and how long it took."""

    latencies: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, latency_s: float, cost_usd: float = 0.0,
               prompt: int = 0, completion: int = 0):
        self.latencies.append(latency_s)
        self.costs.append(cost_usd)
        self.prompt_tokens += prompt
        self.completion_tokens += completion

    @property
    def cost_per_query(self) -> float:
        return sum(self.costs) / len(self.costs) if self.costs else 0.0

    @property
    def total_cost(self) -> float:
        return sum(self.costs)

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        # Nearest-rank: with 20 samples the p95 is the 19th, not an
        # interpolation between two of them.
        index = max(0, min(len(ordered) - 1,
                           int(round(0.95 * len(ordered))) - 1))
        return ordered[index]

    @property
    def mean_latency(self) -> float:
        return statistics.fmean(self.latencies) if self.latencies else 0.0


# ---------------------------------------------------------------------------
# the composite used by DSPy optimizers
# ---------------------------------------------------------------------------

def _answer_text(prediction) -> str:
    for attribute in ('answer', 'response', 'output', 'text'):
        value = getattr(prediction, attribute, None)
        if isinstance(value, str) and value:
            return value
    return str(prediction) if prediction is not None else ''


def agent_metric(example, prediction, trace=None) -> float:
    """The single score DSPy optimizes against.

    Weighted towards being right and towards not making things up, because
    those are the two properties the whole design exists to protect. Tool
    selection is included because a wrong tool is usually the cause of the
    other two failing, and rewarding it directly gives the optimizer a cleaner
    signal than waiting for the downstream error.
    """
    answer = _answer_text(prediction)
    evidence_numbers = getattr(prediction, 'evidence_numbers', None) or set()

    correct = factual_accuracy(example, prediction)
    tool = tool_selection_accuracy(example, prediction)
    honest = 1.0 - hallucination_rate(answer, evidence_numbers)

    return round(0.55 * correct + 0.20 * tool + 0.25 * honest, 4)


def summarise(results: list[dict], stats: RunStats | None = None) -> dict:
    """Aggregate one evaluation pass into the numbers that go to MLflow."""
    if not results:
        return {}

    def mean(key):
        values = [r[key] for r in results if r.get(key) is not None]
        return round(statistics.fmean(values), 4) if values else None

    out = {
        'examples': len(results),
        'factual_accuracy': mean('factual_accuracy'),
        'tool_selection_accuracy': mean('tool_selection_accuracy'),
        'hallucination_rate': mean('hallucination_rate'),
        'retrieval_precision_at_k': mean('retrieval_precision_at_k'),
        'score': mean('score'),
        'refusal_accuracy': _refusal_accuracy(results),
    }
    if stats:
        out.update({
            'cost_per_query': round(stats.cost_per_query, 6),
            'total_cost_usd': round(stats.total_cost, 4),
            'p95_latency_s': round(stats.p95_latency, 3),
            'mean_latency_s': round(stats.mean_latency, 3),
            'prompt_tokens': stats.prompt_tokens,
            'completion_tokens': stats.completion_tokens,
        })
    return out


def _refusal_accuracy(results: list[dict]) -> float | None:
    """Scored separately: an agent that answers everything is not trustworthy,
    and this number is invisible inside an overall average."""
    refusals = [r for r in results if r.get('answer_type') == 'refusal']
    if not refusals:
        return None
    return round(statistics.fmean(r['factual_accuracy'] for r in refusals), 4)
