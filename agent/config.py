"""Model tiers, the spend guard, and MLflow wiring.

Everything goes through OpenRouter via DSPy's LiteLLM backend, so model choice
is configuration rather than code. Three tiers, and which one you are on
matters more than anything else in this file:

    wiring   openai/gpt-oss-20b:free      $0        plumbing and smoke tests
    cheap    openai/gpt-oss-20b           $0.03/$0.13 per 1M   optimizer runs
    quality  anthropic/claude-sonnet-4.5  $3/$15 per 1M        final answers

Two deliberate departures from the master prompt, both in its own spirit:

* It pins the optimizer to `gpt-4o-mini`. The paid `gpt-oss-20b` is five times
  cheaper at $0.03/$0.13 against $0.15/$0.60, for comparable bootstrapping.
  `gpt-4o-mini` stays as the documented fallback if structured output proves
  unreliable, which is the spec's own stated caveat. In practice gpt-oss often
  returns adapter junk (`query_id=SINGLE-RESULT-END`) under DSPy JSON mode;
  `IntentClassifier` auto-retries on `fallback` when that happens. Chat/ask
  default to `fallback` for the same reason.
* Plumbing runs on the free tier. Whether the retriever returns documents and
  whether routing picks the right tool are properties of the wiring, not of
  model quality, so debugging them costs nothing.

**The spend guard is a hard stop, not a warning.** MIPROv2 issues hundreds to
thousands of calls, this runs unattended, and the account holds $15. `Budget`
raises `BudgetExceeded` the moment a call would cross the ceiling, and the
exception is allowed to propagate — a half-finished optimization that stopped
is recoverable; a drained account at 4am is not.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Tier -> (model id, $ per 1M prompt tokens, $ per 1M completion tokens)
TIERS: dict[str, tuple[str, float, float]] = {
    'wiring': ('openrouter/openai/gpt-oss-20b:free', 0.0, 0.0),
    'cheap': ('openrouter/openai/gpt-oss-20b', 0.03, 0.13),
    'quality': ('openrouter/anthropic/claude-sonnet-4.5', 3.00, 15.00),
    # Documented fallback, per the spec's structured-output caveat.
    'fallback': ('openrouter/openai/gpt-4o-mini', 0.15, 0.60),
    # Fast report / chat drafts — Gemini Flash via OpenRouter.
    'fast': ('openrouter/google/gemini-2.5-flash', 0.30, 2.50),
}

DEFAULT_TIER = os.getenv('GUNNERS_LM_TIER', 'wiring')
DEFAULT_BUDGET_USD = float(os.getenv('GUNNERS_BUDGET_USD', '10'))

# Cap every completion. This is not a nicety — without it DSPy sends no
# max_tokens, LiteLLM asks for the model's maximum (64,000 for Sonnet 4.5), and
# OpenRouter pre-authorises the *worst case* against the balance rather than
# the actual cost. On a modest balance that rejects every call outright:
#
#   402: You requested up to 64000 tokens, but can only afford 2269
#
# which reads like a billing problem and is really a default. The answers here
# are two or three sentences, so a cap this size is generous and it makes the
# pre-authorisation a fraction of a cent.
DEFAULT_MAX_TOKENS = int(os.getenv('GUNNERS_MAX_TOKENS', '1024'))
MLFLOW_URI = os.getenv('MLFLOW_TRACKING_URI', 'http://localhost:5000')
EXPERIMENT = os.getenv('GUNNERS_MLFLOW_EXPERIMENT', 'football-analyst')


class BudgetExceeded(RuntimeError):
    """The run would have spent more than it was allowed to."""


@dataclass
class Budget:
    """Tracks spend and refuses to let it pass the ceiling.

    Thread-safe because DSPy's optimizers evaluate candidates in parallel, and
    a race here would be a race on money.
    """

    limit_usd: float = DEFAULT_BUDGET_USD
    spent_usd: float = 0.0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(self, tier: str, prompt: int, completion: int) -> float:
        _, in_rate, out_rate = TIERS[tier]
        cost = (prompt * in_rate + completion * out_rate) / 1_000_000
        with self._lock:
            if self.spent_usd + cost > self.limit_usd:
                raise BudgetExceeded(
                    f'this call would take spend to '
                    f'${self.spent_usd + cost:.4f}, over the ${self.limit_usd:.2f} '
                    f'ceiling after {self.calls} calls. Stopping rather than '
                    f'continuing unattended.')
            self.spent_usd += cost
            self.calls += 1
            self.prompt_tokens += prompt
            self.completion_tokens += completion
        return cost

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    def summary(self) -> dict:
        return {'calls': self.calls, 'spent_usd': round(self.spent_usd, 6),
                'limit_usd': self.limit_usd, 'remaining_usd': round(self.remaining, 6),
                'prompt_tokens': self.prompt_tokens,
                'completion_tokens': self.completion_tokens}


BUDGET = Budget()


def api_key() -> str:
    key = os.getenv('OPENROUTER_API_KEY')
    if not key:
        # Load .env lazily so importing this module never depends on it.
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(ROOT, '.env'))
            key = os.getenv('OPENROUTER_API_KEY')
        except ImportError:
            pass
    if not key:
        raise RuntimeError(
            'OPENROUTER_API_KEY is not set. Put it in .env; see .env.example.')
    return key


def make_lm(tier: str = DEFAULT_TIER, **kwargs):
    """A DSPy LM for one tier."""
    import dspy

    if tier not in TIERS:
        raise ValueError(f'unknown tier {tier!r}; expected one of {sorted(TIERS)}')
    model, _, _ = TIERS[tier]
    kwargs.setdefault('max_tokens', DEFAULT_MAX_TOKENS)
    return dspy.LM(model, api_key=api_key(),
                   api_base='https://openrouter.ai/api/v1',
                   # Free-tier models are throttled; a modest cap keeps a
                   # single hung request from stalling an overnight run.
                   num_retries=3, timeout=120, **kwargs)


def configure(tier: str = DEFAULT_TIER, trace: bool = True):
    """Point DSPy at a tier and start tracing. Returns the LM.

    Call this **once** at process startup (e.g. FastAPI lifespan). Per-request
    overrides must use `use_tier` — DSPy forbids `configure` from a second
    async task, which is exactly what every `/chat` SSE handler is.
    """
    import dspy

    lm = make_lm(tier)
    dspy.configure(lm=lm)
    if trace:
        enable_tracing()
    return lm


@contextmanager
def use_tier(tier: str = DEFAULT_TIER, **lm_kwargs):
    """Scoped LM for one request / worker thread. Safe under FastAPI async."""
    import dspy

    if tier not in TIERS:
        raise ValueError(f'unknown tier {tier!r}; expected one of {sorted(TIERS)}')
    lm = make_lm(tier, **lm_kwargs)
    with dspy.context(lm=lm):
        yield lm


def enable_tracing(experiment: str = EXPERIMENT) -> bool:
    """Turn on MLflow autologging for DSPy.

    Never fatal: an agent that refuses to answer because the tracing server is
    down is worse than one that answers untraced and says so.
    """
    try:
        import mlflow
        mlflow.set_tracking_uri(MLFLOW_URI)
        mlflow.set_experiment(experiment)
        mlflow.dspy.autolog()
        return True
    except Exception as exc:                                    # noqa: BLE001
        print(f'MLflow tracing unavailable ({type(exc).__name__}: {exc}); '
              f'continuing untraced', file=sys.stderr)
        return False


def usage_from(lm) -> tuple[int, int]:
    """(prompt, completion) tokens from the most recent LM call.

    DSPy keeps a rolling history on the LM object; the shape of the usage block
    varies by provider, so this reads defensively and returns zeros rather than
    raising inside a metric.
    """
    try:
        entry = lm.history[-1]
    except (AttributeError, IndexError):
        return 0, 0
    usage = entry.get('usage') or {}
    return (int(usage.get('prompt_tokens') or 0),
            int(usage.get('completion_tokens') or 0))
