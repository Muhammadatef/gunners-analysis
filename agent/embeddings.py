"""Local text embeddings for the document store.

OpenRouter is a chat gateway and serves no embeddings endpoint, so vectors are
computed here rather than bought. That is not a compromise: an embedding model
small enough to run on CPU is entirely adequate for a few thousand short
tactical documents, and it means the corpus can be rebuilt at any hour without
spending a cent or depending on a third party being up.

**fastembed** rather than sentence-transformers: it runs the same models
through ONNX Runtime and needs no torch, which keeps roughly 2.5 GB out of the
image for a component the spec deliberately made lightweight.
`BAAI/bge-small-en-v1.5` is 384-dimensional and quick on CPU.

The model is loaded once and lazily — importing this module must stay cheap,
because Airflow's scheduler parses every DAG file repeatedly and paying a model
load on each parse would be ruinous.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv('GUNNERS_EMBEDDING_MODEL', 'BAAI/bge-small-en-v1.5')
EMBEDDING_DIM = 384

_model = None
_model_name: str | None = None
_lock = threading.Lock()


def _load(model_name: str):
    """Import and construct on first use, never at module import."""
    global _model, _model_name
    with _lock:
        if _model is not None and _model_name == model_name:
            return _model
        from fastembed import TextEmbedding          # heavy; deferred
        logger.info('loading embedding model %s', model_name)
        _model = TextEmbedding(model_name=model_name)
        _model_name = model_name
        return _model


def embed(texts: Sequence[str], model_name: str = DEFAULT_MODEL,
          batch_size: int = 64) -> list[list[float]]:
    """Embed documents. Order in, order out."""
    if not texts:
        return []
    model = _load(model_name)
    return [vector.tolist() for vector in model.embed(list(texts),
                                                      batch_size=batch_size)]


def embed_query(query: str, model_name: str = DEFAULT_MODEL) -> list[float]:
    """Embed a search query.

    Separate from `embed` because retrieval models are often asymmetric — they
    are trained with a prefix on the query side and none on the document side,
    and mixing the two up quietly costs recall. fastembed's `query_embed`
    applies whatever the chosen model expects.
    """
    model = _load(model_name)
    return next(iter(model.query_embed([query]))).tolist()


def model_name() -> str:
    return _model_name or DEFAULT_MODEL


def dimension() -> int:
    return EMBEDDING_DIM


def batched(items: Iterable, size: int):
    """Yield lists of at most `size` — embedding a whole season at once would
    hold every document in memory for no gain."""
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
