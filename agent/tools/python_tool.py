"""Deterministic arithmetic, run somewhere the model cannot reach.

The first hard rule is "never use an LLM where deterministic computation is
possible". Language models are unreliable at arithmetic in a way that is
especially dangerous here, because a wrong number arrives in a fluent sentence
and looks exactly like a right one. So percentages, ratios, per-90s and
differences are computed, not generated.

`evaluate` takes an expression and evaluates it by walking the AST — not
`eval`, not `exec`, not `eval` with a scrubbed `__builtins__`, all of which
have well-known escapes. Only the node types below exist as far as this
evaluator is concerned; anything else is a syntax error before any value is
produced. There are no names to resolve, no attributes to reach through, and no
calls except the handful whitelisted by name.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass

_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_FUNCTIONS = {
    'abs': abs, 'round': round, 'min': min, 'max': max, 'sum': sum,
    'len': len, 'sqrt': math.sqrt, 'floor': math.floor, 'ceil': math.ceil,
    'pow': pow,
}

# Big enough for any football arithmetic, small enough that 9**9**9 cannot
# occupy the machine.
MAX_EXPONENT = 32


class UnsafeExpression(ValueError):
    """The expression asked for something the evaluator does not offer."""


@dataclass
class Calculation:
    expression: str
    result: float
    explanation: str = ''

    def as_prompt(self) -> str:
        head = f'[python] {self.expression} = {self.result}'
        return f'{head}  ({self.explanation})' if self.explanation else head


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise UnsafeExpression(f'only numbers are allowed, got {node.value!r}')

    if isinstance(node, ast.BinOp):
        op = _BINARY.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f'operator {type(node.op).__name__} not allowed')
        left, right = _eval(node.left), _eval(node.right)
        if op is operator.pow and abs(right) > MAX_EXPONENT:
            raise UnsafeExpression(f'exponent {right} is too large')
        if op in (operator.truediv, operator.floordiv, operator.mod) and right == 0:
            raise UnsafeExpression('division by zero')
        return op(left, right)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f'unary {type(node.op).__name__} not allowed')
        return op(_eval(node.operand))

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]

    if isinstance(node, ast.Call):
        # Only a bare name may be called: no attributes, so no __class__ walk.
        if not isinstance(node.func, ast.Name):
            raise UnsafeExpression('only plain function calls are allowed')
        fn = _FUNCTIONS.get(node.func.id)
        if fn is None:
            raise UnsafeExpression(
                f'{node.func.id}() is not available; allowed: '
                f'{", ".join(sorted(_FUNCTIONS))}')
        if node.keywords:
            raise UnsafeExpression('keyword arguments are not allowed')
        return fn(*[_eval(a) for a in node.args])

    raise UnsafeExpression(f'{type(node).__name__} is not allowed in an expression')


def evaluate(expression: str, explanation: str = '') -> Calculation:
    """Evaluate a numeric expression safely.

        evaluate('(71 - 65.84)')        -> 5.16
        evaluate('round(100 * 26/38)')  -> 68
    """
    if not expression or not expression.strip():
        raise UnsafeExpression('empty expression')
    if len(expression) > 500:
        raise UnsafeExpression('expression is too long')

    try:
        tree = ast.parse(expression.strip(), mode='eval')
    except SyntaxError as exc:
        raise UnsafeExpression(f'could not parse: {exc.msg}') from exc

    result = _eval(tree)
    if isinstance(result, list):
        raise UnsafeExpression('expression must produce a single number')
    if isinstance(result, float):
        result = round(result, 6)
    return Calculation(expression=expression.strip(), result=result,
                       explanation=explanation)


# ---------------------------------------------------------------------------
# derived figures — the calculator, wired to evidence rather than to the model
# ---------------------------------------------------------------------------
#
# `docs/AGENT_EFFICIENCY.md`: the calculator existed but was never in the
# graph, so "goals per game" was either invented or refused. It is wired as a
# deterministic post-step instead of as a tool the model may call: the question
# names a ratio, the evidence already holds both sides of it, and the division
# happens here. The model never chooses the operands and never does the sum —
# it only sees the finished line in the evidence, which the hallucination check
# then vouches for like any other tool output.

_RATIO_INTENT = re.compile(
    r'\b(per\s*(?:game|match|90|ninety)|per-?90|on average|average|'
    r'ratio|percentage|accuracy|what share|how often)\b', re.I)

# question keyword -> the evidence columns that could be the numerator
_NUMERATORS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ('conceded', ('goals_against',), 'goals conceded'),
    ('goal', ('goals', 'goals_for'), 'goals'),
    ('assist', ('assists',), 'assists'),
    ('point', ('points',), 'points'),
    ('shot', ('shots',), 'shots'),
    ('key pass', ('key_passes',), 'key passes'),
    ('progressive', ('progressive_passes',), 'progressive passes'),
    ('tackle', ('tackles',), 'tackles'),
    ('interception', ('interceptions',), 'interceptions'),
    ('recover', ('ball_recoveries',), 'ball recoveries'),
    ('xt', ('total_xt',), 'expected threat'),
    ('xg', ('total_xg', 'xg'), 'xG'),
    ('pass', ('passes',), 'passes'),
)

_PER_MATCH = ('played', 'appearances', 'matches', 'games', 'starts')


def _first_number(rows, fields):
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field_name in fields:
            value = row.get(field_name)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return field_name, float(value)
            except (TypeError, ValueError):
                continue
    return None, None


def derive(question: str, rows) -> Calculation | None:
    """A ratio the question asked for and the evidence can already support.

    Returns None whenever anything is missing — a wrong operand is worse than
    no calculation, and the answer module handles absent evidence honestly.
    """
    if not rows or not _RATIO_INTENT.search(question or ''):
        return None
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return None

    lowered = (question or '').lower()

    if re.search(r'\b(pass(ing)? accuracy|accurate passes)\b', lowered):
        _, done = _first_number(rows, ('passes_accurate',))
        _, attempted = _first_number(rows, ('passes',))
        if done is not None and attempted:
            return evaluate(f'round(100 * {done} / {attempted}, 1)',
                            'pass accuracy, percent')

    numerator_label = None
    numerator = None
    for keyword, fields, label in _NUMERATORS:
        if keyword in lowered:
            _, numerator = _first_number(rows, fields)
            if numerator is not None:
                numerator_label = label
                break
    if numerator is None:
        return None

    if re.search(r'\bper-?\s*(?:90|ninety)\b', lowered):
        _, minutes = _first_number(rows, ('minutes',))
        if minutes:
            return evaluate(f'round({numerator} / ({minutes} / 90), 2)',
                            f'{numerator_label} per 90 minutes')
        return None

    _, matches = _first_number(rows, _PER_MATCH)
    if matches:
        return evaluate(f'round({numerator} / {matches}, 2)',
                        f'{numerator_label} per match')
    return None
