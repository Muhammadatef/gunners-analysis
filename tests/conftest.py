"""Shared fixtures.

Also puts the repo root on sys.path once, so individual test modules do not
each have to do it themselves.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """Point the pipeline's data directories at a throwaway tree.

    pipeline.ROOT and friends are module constants resolved at import time, so
    they have to be patched rather than configured.
    """
    from orchestrator import pipeline

    raw = tmp_path / 'data' / 'raw'
    processed = tmp_path / 'data' / 'processed'
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)

    monkeypatch.setattr(pipeline, 'ROOT', str(tmp_path))
    monkeypatch.setattr(pipeline, 'RAW_DIR', str(raw))
    monkeypatch.setattr(pipeline, 'PROCESSED_DIR', str(processed))
    return tmp_path
