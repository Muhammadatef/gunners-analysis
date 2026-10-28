"""Zero is a real xG.

`M.hxg` and friends default to 0.0 when the metadata carries no xG, so the
numbers alone cannot distinguish "the enrichment found nothing" from "this side
genuinely managed nothing". The warehouse used to write `value or None`, which
collapsed the two - and a side with no shots on target has exactly 0.0 xGOT.
Three of Arsenal's first ten 2025/26 matches did, and lost the value to NULL.

`xg_source` is the discriminator: enrich_xg sets it only when it wrote real
numbers.
"""

import pytest

from db.aggregate import _xg, _xg_written


class _M:
    def __init__(self, source):
        self.meta = {} if source is None else {'xg_source': source}


@pytest.mark.parametrize('source', [
    'sofascore',
    'sofascore (sides swapped)',
    'fotmob',
    'fotmob (sides swapped)',
    'manual',
])
def test_a_real_source_means_the_numbers_were_written(source):
    assert _xg_written(_M(source)) is True


@pytest.mark.parametrize('source', [
    None,
    '',
    'unavailable: no sofascore event matched',
    'unavailable: sofascore teams do not match (Fulham v Arsenal)',
])
def test_no_source_means_the_zeroes_are_defaults(source):
    assert _xg_written(_M(source)) is False


def test_zero_survives_when_the_source_is_real():
    assert _xg(0.0, True) == 0.0


def test_a_genuine_number_survives():
    assert _xg(2.91, True) == 2.91


def test_zero_becomes_null_when_nothing_was_written():
    """Without a source the 0.0 is a placeholder, and NULL is the honest answer."""
    assert _xg(0.0, False) is None


def test_a_number_is_still_discarded_without_a_source():
    assert _xg(2.91, False) is None
