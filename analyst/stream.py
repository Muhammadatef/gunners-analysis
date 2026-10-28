"""SSE framing for chat stages.

Named events map onto what FootballAnalyst / MatchReportGenerator already
compute — do not invent stages the agent does not have.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterator


def sse(event: str, data: dict[str, Any]) -> dict[str, str]:
    """Payload for sse-starlette EventSourceResponse."""
    return {'event': event, 'data': json.dumps(data, default=str)}


def chunk_tokens(text: str, size: int = 48) -> Iterator[str]:
    """Fake token chunks so the UI can stream a finished MD report."""
    if not text:
        return
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def aiter_chunks(text: str, size: int = 48) -> AsyncIterator[str]:
    for chunk in chunk_tokens(text, size):
        yield chunk
