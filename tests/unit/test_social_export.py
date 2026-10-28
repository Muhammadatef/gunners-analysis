"""The social exports must satisfy the upload limits for every panel shape.

A rendered panel that violates one of these is not a visual regression - it is
a file LinkedIn or X refuses outright, which is only discovered by hand at the
moment someone tries to post it. `check_upload_safe` is the same predicate the
exporter is written against, so asserting it here closes that loop.
"""

import random
from pathlib import Path

import pytest
from PIL import Image

from visualizer import social_export as se

BG = '#E7EAE4'


def _png(tmp_path: Path, size, mode='RGBA', name='panel.png') -> Path:
    """An opaque figure of a given shape, written the way matplotlib writes."""
    im = Image.new(mode, size, (231, 234, 228) + ((255,) if mode == 'RGBA' else ()))
    path = tmp_path / name
    im.save(path, format='PNG')
    return path


# The shapes that matter: a real panel, the real dashboard, and the extremes
# either side of the accepted ratio window.
SHAPES = [
    pytest.param((2087, 1615), False, id='panel-native'),
    pytest.param((3929, 4973), True, id='dashboard-native-0.790'),
    pytest.param((800, 4000), False, id='absurdly-tall'),
    pytest.param((6000, 900), False, id='absurdly-wide'),
    pytest.param((1000, 1000), False, id='square'),
]


@pytest.mark.parametrize('size,unified', SHAPES)
def test_exported_jpeg_uploads(tmp_path, size, unified):
    out = se.export_social_jpg(_png(tmp_path, size), bg=BG, unified=unified)
    assert se.check_upload_safe(out) == []


@pytest.mark.parametrize('size,unified', SHAPES)
def test_png_is_made_uploadable_in_place(tmp_path, size, unified):
    src = _png(tmp_path, size)
    se.make_png_upload_safe(src, bg=BG)
    assert se.check_upload_safe(src) == []


def test_alpha_channel_is_dropped(tmp_path):
    src = _png(tmp_path, (1200, 1200))
    assert Image.open(src).mode == 'RGBA'
    se.make_png_upload_safe(src, bg=BG)
    assert Image.open(src).mode == 'RGB'


def test_out_of_range_ratio_is_padded_not_stretched(tmp_path):
    """The pitch must keep its proportions; the canvas grows around it."""
    src = _png(tmp_path, (800, 4000))          # 0.20, far below the 0.8 floor
    out = se.export_social_jpg(src, bg=BG)
    w, h = Image.open(out).size
    # Height is untouched and width grew to reach the floor. A stretch would
    # have kept the pixel count and changed the geometry instead.
    assert h == 4000
    assert w >= 4000 * se.MIN_WH_RATIO
    assert w > 800


def test_panels_keep_their_rendered_resolution(tmp_path):
    """A panel is already inside every limit, so nothing should be thrown away.

    The bug this pins: an earlier cap resized panels to 1200px wide and the
    dashboard to 1080x1350 - display recommendations, not upload rules - which
    made the annotations unreadable.
    """
    out = se.export_social_jpg(_png(tmp_path, (2087, 1615)), bg=BG)
    assert Image.open(out).size == (2087, 1615)


def test_dashboard_stays_legible(tmp_path):
    """The unified export may be downscaled, but not to a thumbnail."""
    out = se.export_social_jpg(_png(tmp_path, (3929, 4973)), bg=BG, unified=True)
    w, h = Image.open(out).size
    assert max(w, h) == se.UNIFIED_LONG_EDGE
    assert w >= 2000, f'{w}px wide is too small to read eighteen panels'


def test_check_upload_safe_names_each_violation(tmp_path):
    """The guard has to actually fail, or the assertions above prove nothing."""
    rgba_tall = _png(tmp_path, (400, 4000), name='bad.png')
    problems = se.check_upload_safe(rgba_tall)
    assert any('RGBA' in p for p in problems)
    assert any('ratio' in p for p in problems)


def test_oversized_edge_is_brought_under_the_cap(tmp_path):
    out = se.export_social_jpg(_png(tmp_path, (12000, 6000)), bg=BG)
    assert max(Image.open(out).size) <= se.MAX_EDGE
    assert se.check_upload_safe(out) == []


# --- LinkedIn's caps, which are tighter than X's and fail silently ----------

# The unified dashboard renders at this size. 4973 is over LinkedIn's 4320
# height cap, so the post uploaded and displayed nothing - while alpha, ratio
# and byte count were all fine, which is why nothing flagged it.
DASHBOARD_NATIVE = (3984, 4973)


def test_dashboard_png_is_brought_under_linkedins_height_cap(tmp_path):
    src = _png(tmp_path, DASHBOARD_NATIVE, name='dash.png')
    se.make_png_upload_safe(src, BG)
    with Image.open(src) as im:
        assert im.height <= se.MAX_HEIGHT
        assert im.width <= se.MAX_WIDTH
    assert se.check_upload_safe(src) == []


def test_check_upload_safe_rejects_an_over_tall_image(tmp_path):
    """This used to pass: under the 8192 edge, but over LinkedIn's 4320."""
    src = _png(tmp_path, DASHBOARD_NATIVE, mode='RGB', name='tall.png')
    assert any('tall' in reason for reason in se.check_upload_safe(src))


def test_export_never_lands_exactly_on_the_ratio_floor(tmp_path):
    """2560x3200 is exactly 0.800 - accepted by LinkedIn, then rendered blank."""
    src = _png(tmp_path, DASHBOARD_NATIVE, name='dash.png')
    out = se.export_social_jpg(src, bg=BG, unified=True)
    with Image.open(out) as im:
        ratio = im.width / im.height
    assert ratio > se.MIN_WH_RATIO, f'ratio {ratio:.4f} sits on the 4:5 floor'
    assert se.check_upload_safe(out) == []


def test_the_byte_ceiling_is_linkedins_three_megabytes(tmp_path):
    src = _png(tmp_path, DASHBOARD_NATIVE, name='dash.png')
    out = se.export_social_jpg(src, bg=BG, unified=True)
    assert se.MAX_UPLOAD_BYTES == 3_000_000
    assert out.stat().st_size <= se.MAX_UPLOAD_BYTES


def test_a_png_over_the_byte_ceiling_loses_pixels_until_it_fits(tmp_path):
    """PNG is lossless: the only currency for bytes is resolution."""
    rng = random.Random(7)
    noisy = Image.new('RGB', (4000, 4800))
    noisy.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                   for _ in range(4000 * 4800)])
    src = tmp_path / 'noise.png'
    noisy.save(src, format='PNG')
    assert src.stat().st_size > se.MAX_UPLOAD_BYTES, 'fixture must start too big'

    se.make_png_upload_safe(src, BG)
    assert src.stat().st_size <= se.MAX_UPLOAD_BYTES
    assert se.check_upload_safe(src) == []
