"""The Phase 1 agent: two DSPy modules, three tools.

The master prompt cut the original seven-module, eighteen-tool design down to
this, and the reasoning is right: DSPy's tool routing degrades as the tool list
grows, and a wide surface means debugging selection failures instead of
building capability. Modules get added when the evaluation set shows the agent
failing at a class of question, not before.

    IntentClassifier   what kind of question is this, and what should answer it
    MatchAnalyst       answer it, citing the evidence

The division of labour is the important part. The model chooses *which*
allowlisted query to run and *how to phrase* the result. It never writes SQL,
never does arithmetic, and never supplies a number that did not come back from
a tool. `FootballAnalyst.forward` enforces that last point mechanically:
before an answer is returned, every number in it is checked against the numbers
the evidence actually vouched for, and unsupported figures are reported rather
than shipped.

Three things here are efficiency measures rather than design, all from
`docs/AGENT_EFFICIENCY.md`:

* **Not every question needs the model.** `agent.routing.fast_route` decides
  the plain ones from the text, and `IntentClassifier` only reaches for an LM
  when it returns `None`.
* **Routing is a choice, not an essay.** Classification uses `Predict` over
  enum-constrained fields rather than `ChainOfThought` over free text, and is
  shown a shortlist of the catalogue instead of all of it.
* **Classification happens once per request.** Callers that have already routed
  (the chat service) pass the route in, instead of paying for it twice.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Literal

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dspy  # noqa: E402

from agent import routing                            # noqa: E402
from agent.eval.metrics import hallucinated_numbers  # noqa: E402
from agent.routing import Route                      # noqa: E402
from agent.tools import python_tool, sql_registry    # noqa: E402
from agent.tools.retriever import PgVectorRetriever  # noqa: E402


# The allowlist, as a type. An id outside it is not a wrong answer the adapter
# can return — it is not a representable one.
QueryId = Literal[tuple(sorted(sql_registry.REGISTRY)) + ('',)]  # type: ignore[valid-type]
Tool = Literal['sql', 'retrieval', 'report', 'none']


# ---------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------

class ClassifyIntent(dspy.Signature):
    """Decide how a football question should be answered.

    Pick `sql` when the answer is a number or a record the warehouse holds:
    scores, totals, per-90s, PPDA, possession, minutes played.
    Pick `retrieval` when the answer is a description of how a team or a
    passage of play unfolded, where no single figure answers it.
    Pick `report` when the user wants a full technical / post-match report
    for one fixture (not a two-sentence Q&A) — e.g. "write a report for
    Arsenal vs Leeds", "analyse the match", "GW2 technical briefing".
    Pick `none` when the warehouse cannot answer at all — injuries, transfer
    fees, other clubs' players, anything in the future, or per-shot xG, which
    is not populated. Refusing is a correct answer, not a failure.
    """

    question: str = dspy.InputField()
    available_queries: str = dspy.InputField(
        desc='the allowlisted SQL queries and their parameters')
    tool: Tool = dspy.OutputField(desc='one of: sql, retrieval, report, none')
    query_id: QueryId = dspy.OutputField(
        desc='the allowlisted query id when tool is sql, otherwise the empty string')
    parameters: str = dspy.OutputField(
        desc='JSON object of parameters for the query, e.g. '
             '{"team": "Arsenal", "season": "2025/2026"}. '
             'Season must be YYYY/YYYY (2025/2026), never YYYY/YY (2025/26).')
    reasoning: str = dspy.OutputField(desc='one sentence on why')


class AnswerFromEvidence(dspy.Signature):
    """Answer a football question using only the evidence supplied.

    Every number in the answer must appear in the evidence. Do not calculate,
    estimate, round beyond what is given, or add figures from your own
    knowledge of football — the evidence is the only thing that is true here.

    If the evidence does not answer the question, say so plainly and say what
    is missing. A clear refusal is worth more than a confident guess.

    Write as an analyst talking to a colleague: direct, specific, no padding.
    """

    question: str = dspy.InputField()
    evidence: str = dspy.InputField(desc='query results and retrieved documents')
    answer: str = dspy.OutputField(desc='two or three sentences, grounded in the evidence')


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------

# gpt-oss (and some free OpenRouter routes) often emit adapter sentinel
# strings instead of real fields under DSPy JSON mode. Treat those as
# structured-output failure and retry on the documented fallback model. The
# enum-typed fields above make this rarer; they do not make it impossible,
# because a model that ignores the schema ignores the enum too.
_ADAPTER_JUNK_QUERY_IDS = frozenset({
    'SINGLE-RESULT-END',
    'MULTI-RESULT-END',
    'COMPLETED',
})


def route_looks_broken(route) -> bool:
    """True when classifier output is unusable (wrong tool / adapter junk)."""
    tool = (getattr(route, 'tool', '') or '').strip().lower()
    qid = (getattr(route, 'query_id', '') or '').strip()
    qid_key = qid.upper().replace(' ', '-').replace('_', '-')
    if tool not in {'sql', 'retrieval', 'report', 'none'}:
        return True
    if qid_key in _ADAPTER_JUNK_QUERY_IDS or 'RESULT-END' in qid_key:
        return True
    if tool == 'sql' and not qid:
        return True
    return False


def as_route(prediction, source: str = 'lm') -> Route:
    """Normalise a raw DSPy prediction into a `Route`."""
    tool = (getattr(prediction, 'tool', '') or '').strip().lower()
    raw_id = (getattr(prediction, 'query_id', '') or '').strip()
    query_id = sql_registry.resolve_query_id(raw_id) or raw_id
    return Route(
        tool=tool,
        query_id=query_id,
        parameters=_parse_params(getattr(prediction, 'parameters', '') or ''),
        reasoning=getattr(prediction, 'reasoning', '') or '',
        source=source,
    )


class IntentClassifier(dspy.Module):
    """Route a question — from the text when that is enough, else from the LM."""

    def __init__(self, fast_path: bool = True):
        super().__init__()
        self.classify = dspy.Predict(ClassifyIntent)
        self.fast_path = fast_path

    def forward(self, question: str, slots: dict | None = None,
                schema: str | None = None) -> Route:
        if self.fast_path:
            fast = routing.fast_route(question, slots=slots, schema=schema)
            if fast is not None:
                fast.parameters = routing.merge_slots(
                    fast.parameters, slots, question)
                return fast

        catalogue = routing.catalogue_for(question)
        prediction = self.classify(question=question,
                                   available_queries=catalogue)
        if not route_looks_broken(prediction):
            route = as_route(prediction)
        else:
            route = as_route(self._retry(question, catalogue), 'lm-fallback')
        route.parameters = routing.merge_slots(route.parameters, slots, question)
        return route

    def _retry(self, question: str, catalogue: str):
        """Structured output failed on the active LM (typically gpt-oss-20b).

        Retry once on gpt-4o-mini — config documents this exact caveat.
        """
        from agent import config

        current = getattr(getattr(dspy.settings, 'lm', None), 'model', '') or ''
        fallback_model = config.TIERS['fallback'][0]
        if fallback_model in current:
            return self.classify(question=question, available_queries=catalogue)
        with config.use_tier('fallback'):
            return self.classify(question=question, available_queries=catalogue)


class MatchAnalyst(dspy.Module):
    def __init__(self):
        super().__init__()
        self.answer = dspy.ChainOfThought(AnswerFromEvidence)

    def forward(self, question: str, evidence: str):
        return self.answer(question=question, evidence=evidence)


class FootballAnalyst(dspy.Module):
    """Route, gather evidence, answer, then check the answer against it."""

    def __init__(self, retriever=None, schema: str | None = None, k: int = 5,
                 fast_path: bool = True):
        super().__init__()
        self.classifier = IntentClassifier(fast_path=fast_path)
        self.analyst = MatchAnalyst()
        self.retriever = retriever or PgVectorRetriever(schema=schema)
        self.schema = schema
        self.k = k

    # -- evidence gathering, none of which involves the model ---------------

    def _run_sql(self, query_id: str, params: dict):
        try:
            return sql_registry.run(query_id, params, self.schema), None
        except sql_registry.QueryNotAllowed as exc:
            return None, str(exc)
        except Exception as exc:                                # noqa: BLE001
            return None, f'{type(exc).__name__}: {exc}'

    def forward(self, question: str, route: Route | None = None,
                slots: dict | None = None, **_):
        started = time.time()
        if route is None:
            route = self.classifier(question=question, slots=slots,
                                    schema=self.schema)
        elif slots:
            route.parameters = routing.merge_slots(
                route.parameters, slots, question)

        tool = (route.tool or '').strip().lower()
        query_id = route.query_id or ''
        params = dict(route.parameters or {})

        evidence_parts: list[str] = []
        evidence_numbers: set[float] = set()
        evidence_rows: list[dict] = []
        retrieved: list[dict] = []
        tools_used: set[str] = set()
        errors: list[str] = []

        if tool == 'report':
            # Chat owns the report skill; short Q&A path points there rather
            # than writing a multi-section MD document inline.
            return dspy.Prediction(
                answer=('That looks like a match-report request. Ask for a '
                        'technical report in chat (or open the match page and '
                        'use Ask about this match) so the report skill can run.'),
                tool='report',
                tools_used=[],
                query_id='',
                parameters=params,
                evidence='',
                evidence_numbers=set(),
                retrieved=[],
                unsupported_numbers=[],
                errors=[],
                reasoning=route.reasoning,
                route_source=route.source,
                latency_s=round(time.time() - started, 3),
            )

        if tool == 'sql' and query_id:
            result, error = self._run_sql(query_id, params)
            if result is not None:
                tools_used.add('sql')
                query_id = result.query_id  # canonical id after resolve
                evidence_parts.append(result.as_prompt())
                evidence_numbers |= result.numbers()
                evidence_rows.extend(result.rows)
            else:
                errors.append(error)
                # A bad query id or missing parameter is a routing failure, not
                # a dead end: fall back to retrieval so the user still gets
                # something grounded rather than an apology.
                tool = 'retrieval'

        if tool == 'retrieval':
            documents = self.retriever.search(question, k=self.k,
                                              filters=_retrieval_filters(params))
            if documents:
                tools_used.add('retrieval')
                retrieved = [d.as_dict() for d in documents]
                evidence_parts.extend(d.as_prompt() for d in documents)
                for d in documents:
                    evidence_numbers |= _numbers(d.content)

        # The calculator, as a post-step: if the question asked for a ratio and
        # the rows already hold both sides of it, compute it rather than
        # leaving the model to invent or refuse it.
        calculation = python_tool.derive(question, evidence_rows)
        if calculation is not None:
            tools_used.add('python')
            evidence_parts.append(calculation.as_prompt())
            evidence_numbers.add(round(float(calculation.result), 4))

        if not evidence_parts:
            evidence_parts.append(
                'No evidence was found in the warehouse for this question.')

        prediction = self.analyst(question=question,
                                  evidence='\n'.join(evidence_parts))
        answer = getattr(prediction, 'answer', '') or ''

        unsupported = hallucinated_numbers(answer, evidence_numbers)

        return dspy.Prediction(
            answer=answer,
            tool=tool,
            tools_used=sorted(tools_used),
            query_id=query_id,
            parameters=params,
            evidence='\n'.join(evidence_parts),
            evidence_numbers=evidence_numbers,
            retrieved=retrieved,
            unsupported_numbers=sorted(unsupported),
            errors=errors,
            reasoning=route.reasoning,
            route_source=route.source,
            latency_s=round(time.time() - started, 3),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_params(raw) -> dict:
    """Model output is text; parameters have to survive it being imperfect."""
    import json
    import re

    if isinstance(raw, dict):
        return dict(raw)
    raw = (raw or '').strip()
    if not raw:
        return {}
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-z]*\n?|\n?```$', '', raw).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    # Single quotes are the usual failure; try it as a Python literal.
    try:
        import ast
        parsed = ast.literal_eval(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, SyntaxError):
        return {}


def _retrieval_filters(params: dict) -> dict:
    from agent.tools.retriever import FILTERABLE
    return {k: v for k, v in (params or {}).items() if k in FILTERABLE}


def _numbers(text: str) -> set[float]:
    from agent.eval.metrics import numbers_in
    return numbers_in(text)


def calculate(expression: str) -> python_tool.Calculation:
    """The third tool. Exposed for the analyst to use via the runner, and kept
    out of the module graph so no model ever gets to pass it a string it wrote
    without the expression being parsed first."""
    return python_tool.evaluate(expression)
