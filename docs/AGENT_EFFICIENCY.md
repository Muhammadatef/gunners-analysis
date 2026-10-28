# Chatbot, DSPy, and vectorizing — status and efficiency roadmap

**As of 2026-08-21.** Snapshot of where the analyst layer actually is (working tree, not only `STATUS.md`) and what to change for more efficient chat, DSPy, and retrieval.

`STATUS.md` is still dated **2026-08-08**. Treat this file as the later note for the agent / chatbot path. It does not replace `STATUS.md`, `docs/AGENT.md`, or `OPEN_ISSUES.md`.

---

## Where the project actually is

`main` is 23 commits ahead of `origin/master`, plus a large **uncommitted** set: analyst HTTP + web chat, match reports, tracking, 2026/27 output.

### Data and pipeline

- **2025/26** is complete: 38/38 matches, scores verified, SofaScore xG on every match.
- **Analyst warehouse** (from the 8 Aug snapshot): possessions, pressing, carries, match tactical features, **~4,349** semantic docs in pgvector (`BAAI/bge-small-en-v1.5`, 384-d, HNSW).
- **2026/27** has started in output: Community Shield **Arsenal 3–0 Man City (16 Aug)** is rendered. `STATUS.md` still said “nothing played / first fixture 21 Aug Coventry” — that paragraph is stale.
- **Video tracking** is a sibling path (Fulham GW35 pilot), parquet + fused markdown, **not** wired into the chatbot yet.

### Chatbot / DSPy

Phase 1 agent is real: **classify → SQL or retrieval → answer**, then a mechanical number check (`unsupported_numbers`). The model never writes SQL.

| Piece | State |
|---|---|
| `FootballAnalyst` | 2 signatures, 3 modules, allowlisted SQL + pgvector |
| Chat | FastAPI `:8100`, SSE (`start → route → evidence → tokens → check → done`) |
| Reports | Fast fixture-slot path + warehouse scoreboard + **one** analysis LM call (`k_docs` defaults to **0**, so reports skip vectors) |
| Conversations | Stored in Postgres; **not** passed back into the next turn |
| Default chat tier | `fallback` (`gpt-4o-mini`) because `gpt-oss` often emits DSPy JSON junk |
| Optimizers | BootstrapFewShot **hurt** slightly on dev; **MIPROv2 never finished** |
| Held-out eval | Cheap ~**0.52**; Sonnet ~**0.88** on the 16 questions that ran |
| Agent DAGs | Rewired to HTTP on the analyst service (local / uncommitted); still not the live freshness loop |

Architecture that is already right: SQL for numbers, vectors for prose, refuse when the warehouse cannot answer.

---

## Where efficiency is leaking

**1. QA still pays two LLM calls, and chat can pay three.**  
`/chat` classifies, then `FootballAnalyst.forward` classifies **again**, then answers. Classify uses `ChainOfThought` and dumps the **full SQL catalogue** every time.

**2. Routing quality is the cheap-tier bottleneck.**  
Held-out score collapsed because the small model picks the wrong `query_id` on unseen families. Sonnet’s tool accuracy was 1.0. Optimizing prompts on a model that cannot emit JSON is wasted spend.

**3. Vectors are under-used on the product path.**  
Reports default `k_docs=0`. Retrieval is cosine + metadata filter, `k=5`, no rerank, no hybrid BM25, no MMR. Documents are prose (good) but “longest move” still will not rank by magnitude.

**4. Chat has no memory.**  
History is persisted for the UI only. Follow-ups like “what about Saka in that match?” re-route from scratch.

**5. The calculator is not in the graph.**  
`python_tool` exists; `FootballAnalyst` never calls it. Ratios get invented or refused instead of computed from evidence.

**6. Compiled programs are not loaded at serve time.**  
`ask.py` / eval can `--program`; the analyst service always starts a raw `FootballAnalyst()`.

---

## Highest-leverage enhancements

### Chatbot (latency and tokens)

1. **One classify per request.** Pass the already-chosen `tool` / `query_id` / params into `FootballAnalyst`; skip the second `IntentClassifier`.
2. **Expand the fast path** (obvious reports already skip classify): regex / keyword / embedding-of-query-ids for “how many goals did X score”, season totals, PPDA, etc. Only call the LM when the fast path is ambiguous.
3. **Classify with `Predict`, not `ChainOfThought`.** Routing does not need a reasoning essay. Keep CoT only for the answer module if eval still needs it.
4. **Shrink the catalogue.** Send 4–6 query ids after a cheap first pass (embedding over `query_id + description`, or a tiny router LM) instead of `describe_all()`.
5. **Split tiers:** cheap/fallback for route, `fast` (Gemini Flash) or `quality` only for the answer/report. The `fast` tier already exists in `agent/config.py`.
6. **Feed last N turns** (or last `match_id` / opponent / season) as **slots**, not as a long chat transcript — keep the “no invented stats” rule.
7. **Warm fastembed in analyst lifespan** so the first retrieval is not a model load.
8. **Stream real tokens** from the LM if the provider allows it; `chunk_tokens` today is UI-only.

### DSPy (quality per dollar)

1. **Do not re-run BootstrapFewShot on `gpt-oss`.** It already failed. Compile **IntentClassifier only** on `gpt-4o-mini` (or a model with reliable JSON), with a metric that is **tool-selection + query_id**, not the composite answer score.
2. **Finish MIPROv2 / GEPA with a hard budget** once credit is there; promote only if **held-out test** improves (already refuse to save on a worse dev score — keep that).
3. **Load the artefact in `/chat`.** Without that, every compile is research, not product.
4. **Add labelled families that currently fail** (`player_assists`, `player_prog_passes`, follow-ups, report vs QA). Family-split is already correct; the train set is thin on the hard families.
5. **Use constrained enums** for `tool` and `query_id` so adapter junk (`SINGLE-RESULT-END`) cannot be a valid output.
6. **Wire `python_tool` as a deterministic post-step** when the question is a ratio and two numbers are already in evidence — not as a free-form LM tool.
7. **Keep Phase 3 unbuilt** until eval shows a class of failure. More tools will make routing worse.

### Vectorizing (recall without extra LLM cost)

1. **Turn retrieval on for reports with `k_docs=3–5` + `match_id` filter** (the retriever already supports this). Cheapest quality win.
2. **Hybrid search:** `tsvector` / BM25 on `content` plus cosine, then RRF. Fixes names, query ids, and “PPDA”-style tokens that bge-small undershoots.
3. **Query rewrite only when needed:** one cheap “search query” string for tactical questions; skip it for SQL-routed ones (those should not retrieve at all).
4. **Rerank top 20 → 5** with a small cross-encoder, or diversity (MMR) so five near-duplicate sequence docs do not fill the window.
5. **Richer metadata filters from slots:** `doc_type` (`tactical` vs `player` vs `shot`), `phase`, `opponent`. The allowlist already exists; the classifier rarely fills it.
6. **Do not jump to ColBERT.** Corpus is still thousands of docs. Revisit only if hybrid + rerank plateaus.
7. **Document generators:** shorter, one claim per doc, include canonical numbers as text (“PPDA 10.76”) so retrieval and the hallucination check share vocabulary. Rebuild is idempotent.
8. **Cache query embeddings** (LRU on the question string) and keep using `query_embed` vs `embed` — do not collapse them.
9. **Nightly watermark refresh via `/maintenance/documents`** so 2026/27 matches get vectors without a full 4k rebuild.

---

## Suggested order (efficiency first)

| Priority | Change | Why |
|---|---|---|
| 1 | Deduplicate classify in `/chat` | Immediate latency/cost cut |
| 2 | Fast-path SQL + enum-constrained router | Cheap-tier tool accuracy |
| 3 | Hybrid retrieve + `k_docs>0` on reports | Better context, still one SQL + one ANN |
| 4 | Compile **classifier only** on `gpt-4o-mini`, load at serve | Measured DSPy gain |
| 5 | Conversation slots + calculator | Follow-ups and ratios without extra tools |
| 6 | MIPROv2 later, Phase 3 never until eval says so | Avoid routing collapse |

The design that should stay: **numbers from SQL, story from vectors, refuse when empty.** Efficiency here is fewer LM calls and better routing, not a bigger agent.

---

## Related files

| Path | Role |
|---|---|
| `STATUS.md` | System snapshot as of 2026-08-08 |
| `docs/AGENT.md` | How the analyst is supposed to work |
| `Football_Analyst_AI_Agent_Master_Prompt.md` | Original spec |
| `agent/modules.py` | DSPy modules |
| `analyst/api.py` | Chat / SSE / maintenance HTTP |
| `agent/tools/retriever.py` | pgvector retriever |
| `agent/embeddings.py` | fastembed |
| `agent/optimize.py` | Phase 2 compile |
