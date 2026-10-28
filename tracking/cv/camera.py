"""Optional camera motion compensation (stub for v1)."""

from __future__ import annotations


def identity_shift() -> tuple[float, float]:
    """v1: no optical-flow compensation — return zero shift."""
    return 0.0, 0.0
