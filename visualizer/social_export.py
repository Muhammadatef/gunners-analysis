"""Platform-safe image export for LinkedIn and X/Twitter uploads.

Uploads fail on hard limits, not on how the picture looks. Four of them bite
here, and every export below is proved against all four before it is written:

  * An alpha channel. matplotlib writes RGBA PNGs even when the figure is
    completely opaque, and both uploaders reject or mangle them. The same
    finding is already recorded in visualizer/player_performance.py, which
    flattens for exactly this reason. The match report never did: measured on
    the 2026/27 MW3 output, every panel and the dashboard were RGBA with alpha
    pinned at 255 - a dead channel that costs bytes and breaks uploads.
  * A width/height ratio outside 4:5 .. 3:1 (LinkedIn). The unified dashboard
    is natively 0.790, just under the floor.
  * A byte ceiling - 5 MB on X, 3 MB on LinkedIn.
  * A pixel box - LinkedIn will not display a feed image beyond 7680x4320.
    Height is what binds: the unified dashboard renders 4973 tall. LinkedIn
    accepts that upload and then shows nothing at all - no error, no
    placeholder - which is the most expensive way to find out.

Resolution is a limit, but a generous one, and the distinction matters. An
earlier version capped the unified dashboard at 1080x1350, which is a LinkedIn
*display* recommendation rather than an upload rule: it squeezed eighteen
panels of annotation into something unreadable, so the legible PNG got uploaded
instead - and that one fails. 7680x4320 is the actual rule, and it leaves the
dashboard comfortably legible.

Two lessons are encoded below rather than written down anywhere else. Limits
that fail *silently* have to be checked, not assumed: check_upload_safe only
ever tested X's 8192 edge and 5 MB, so a file LinkedIn refuses passed it
cleanly. And exports land just inside the ratio window rather than exactly on
it - block-rounding once produced precisely 0.800, the 4:5 floor to three
decimals, which LinkedIn also swallowed and never rendered.
"""

from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path

from PIL import Image

# --- hard limits the uploaders enforce ---------------------------------------

# LinkedIn accepts width/height between 4:5 (0.8) and 3:1 (3.0).
MIN_WH_RATIO = 4 / 5
MAX_WH_RATIO = 3.0

# Land just inside the window rather than exactly on it. The unified dashboard
# used to come out at precisely 0.800 - block-rounding the height to 3200 put
# it on the floor to three decimals - and LinkedIn accepted the upload, then
# showed nothing. Whether that is a strict `>` on their side or a rounding
# difference does not matter; a few pixels of background is a cheap way never
# to find out. These are the padding targets; the bare limits above stay the
# pass/fail test.
RATIO_SAFETY = 0.01
SAFE_MIN_RATIO = MIN_WH_RATIO * (1 + RATIO_SAFETY)
SAFE_MAX_RATIO = MAX_WH_RATIO * (1 - RATIO_SAFETY)

# X rejects images over 5 MB, or with either edge over 8192 px.
X_MAX_UPLOAD_BYTES = 5_000_000
MAX_EDGE = 8192

# LinkedIn is the tighter of the two and the one that fails silently: it caps
# a feed image at 7680x4320 and 3 MB. Height is the binding constraint here -
# the unified dashboard renders 3984x4973, and 4973 > 4320, which is exactly
# why it uploaded and then rendered blank. Only the JPEG ceiling was ever
# encoded, so check_upload_safe passed files LinkedIn would not display.
MAX_WIDTH = 7680
MAX_HEIGHT = 4320
LINKEDIN_MAX_BYTES = 3_000_000

# The binding ceiling across both platforms.
MAX_UPLOAD_BYTES = min(X_MAX_UPLOAD_BYTES, LINKEDIN_MAX_BYTES)

# --- legibility targets, chosen well inside those limits ---------------------

# Kept identical to SOCIAL_LONG_EDGE_* in visualizer/player_performance.py so
# the two renderers cannot drift apart.
PANEL_LONG_EDGE = 4096
UNIFIED_LONG_EDGE = 3200

JPEG_QUALITY = 92
MIN_JPEG_QUALITY = 70
QUALITY_STEP = 6
# 4:4:4. These are charts: chroma subsampling smears thin coloured lines and
# the small type in the annotations.
JPEG_SUBSAMPLING = 0
BLOCK = 8


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or '#FFFFFF').lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _round8(n: float) -> int:
    return max(BLOCK, int(round(n / BLOCK)) * BLOCK)


def _ceil8(n: float) -> int:
    return max(BLOCK, int(math.ceil(n / BLOCK)) * BLOCK)


def _flatten(im: Image.Image, bg: str) -> Image.Image:
    """Composite onto an opaque background and drop the alpha channel."""
    if im.mode == 'RGB':
        # copy() forces the load, so the result outlives the caller's file
        # handle - `convert` on the other branch already does.
        return im.copy()
    rgba = im.convert('RGBA')
    rgb = Image.new('RGB', rgba.size, _hex_to_rgb(bg))
    rgb.paste(rgba, mask=rgba.split()[3])
    return rgb


def _fit_long_edge(rgb: Image.Image, long_edge: int) -> Image.Image:
    """Scale down uniformly so the longest side is at most `long_edge`.

    Uniform is the point: the previous version rounded width and height to the
    block grid independently and then resized to whatever ratio the clamp
    wanted, which stretched the pitch out of square.
    """
    w, h = rgb.size
    limit = min(long_edge, MAX_EDGE)
    # Per-axis caps as well as the long edge: a tall image can sit under an
    # 8192 long edge and still break LinkedIn's 4320 height limit.
    scale = min(limit / max(w, h), MAX_WIDTH / w, MAX_HEIGHT / h, 1.0)
    if scale >= 1.0:
        return rgb
    return rgb.resize((_round8(w * scale), _round8(h * scale)),
                      Image.Resampling.LANCZOS)


def _pad_to_ratio(rgb: Image.Image, bg: str) -> Image.Image:
    """Letterbox until the ratio is inside the accepted window.

    Padding only, never resizing, and always the last step before encoding:
    growing the short side with `_ceil8` can only push the ratio further inside
    the bounds, so the result is in range by construction rather than by luck.
    """
    w, h = rgb.size
    ratio = w / h
    if SAFE_MIN_RATIO <= ratio <= SAFE_MAX_RATIO:
        return rgb
    if ratio < SAFE_MIN_RATIO:
        new_w, new_h = _ceil8(h * SAFE_MIN_RATIO), h
    else:
        new_w, new_h = w, _ceil8(w / SAFE_MAX_RATIO)
    canvas = Image.new('RGB', (new_w, new_h), _hex_to_rgb(bg))
    canvas.paste(rgb, ((new_w - w) // 2, (new_h - h) // 2))
    # Padding grows one side, which can push it past an axis cap.
    return _fit_long_edge(canvas, max(canvas.size))


def _encode(im: Image.Image, quality: int) -> bytes:
    buf = BytesIO()
    im.save(
        buf,
        format='JPEG',
        quality=quality,
        subsampling=JPEG_SUBSAMPLING,
        optimize=True,
        progressive=False,   # baseline: the most widely accepted variant
        dpi=(72, 72),        # JFIF density in real units rather than "aspect ratio"
    )
    return buf.getvalue()


def _encode_png(im: Image.Image) -> bytes:
    buf = BytesIO()
    im.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def _write_jpeg(im: Image.Image, path: Path, bg: str) -> Path:
    """Encode under the byte ceiling, giving up quality before resolution."""
    path.parent.mkdir(parents=True, exist_ok=True)
    im = _pad_to_ratio(im, bg)

    quality = JPEG_QUALITY
    data = _encode(im, quality)
    while len(data) > MAX_UPLOAD_BYTES and quality > MIN_JPEG_QUALITY:
        quality = max(MIN_JPEG_QUALITY, quality - QUALITY_STEP)
        data = _encode(im, quality)

    # Only once quality is spent does resolution go. Charts this dense have
    # never reached here, but a silent 5 MB overrun is an upload failure.
    while len(data) > MAX_UPLOAD_BYTES and max(im.size) > 1024:
        im = _pad_to_ratio(_fit_long_edge(im, int(max(im.size) * 0.85)), bg)
        data = _encode(im, quality)

    path.write_bytes(data)
    return path


def check_upload_safe(path: Path | str) -> list[str]:
    """Return the reasons `path` would be rejected. Empty means it uploads."""
    path = Path(path)
    im = Image.open(path)
    w, h = im.size
    problems = []
    if im.mode not in ('RGB', 'L'):
        problems.append(f'{im.mode} (alpha or palette)')
    if not (MIN_WH_RATIO <= w / h <= MAX_WH_RATIO):
        problems.append(f'ratio {w / h:.3f} outside {MIN_WH_RATIO:.2f}..{MAX_WH_RATIO:.1f}')
    if max(w, h) > MAX_EDGE:
        problems.append(f'{max(w, h)}px edge over {MAX_EDGE}')
    if w > MAX_WIDTH:
        problems.append(f'{w}px wide over LinkedIn\'s {MAX_WIDTH}')
    if h > MAX_HEIGHT:
        problems.append(f'{h}px tall over LinkedIn\'s {MAX_HEIGHT}')
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        problems.append(f'{size / 1e6:.1f}MB over {MAX_UPLOAD_BYTES / 1e6:.0f}MB')
    return problems


def make_png_upload_safe(path: Path | str, bg: str = '#E7EAE4') -> Path:
    """Rewrite a PNG in place so the archive copy uploads as-is.

    Three changes, none of which touches a rendered pixel beyond scaling. The
    alpha channel goes: matplotlib figures drawn with an opaque `facecolor`
    still carry a fully opaque alpha band, so dropping it is lossless and
    removes the thing both uploaders choke on. The canvas is letterboxed into
    the accepted ratio window - the unified dashboard is natively 0.790, which
    LinkedIn refuses. And the result is scaled to fit inside LinkedIn's
    7680x4320 box.

    That last one was missing, and it is what made this function's promise
    false. The unified dashboard renders 3984x4973; 4973 is over the 4320
    height cap, so LinkedIn took the upload and displayed nothing. Ratio and
    alpha were both fine, which is why nothing here or in check_upload_safe
    reported a problem.

    The JPEG sibling is still the better thing to post - a PNG this dense can
    exceed the 3 MB ceiling even at legal dimensions, and only a lossy format
    can trade quality for bytes. This means reaching for the full-resolution
    PNG is no longer a dead end.
    """
    path = Path(path)
    with Image.open(path) as im:
        before = (im.mode, im.size)
        rgb = _flatten(im, bg)
    fitted = _fit_long_edge(rgb, max(rgb.size))
    padded = _pad_to_ratio(fitted, bg)
    if before == ('RGB', padded.size) and path.stat().st_size <= MAX_UPLOAD_BYTES:
        return path

    data = _encode_png(padded)
    # PNG is lossless, so bytes can only be bought with pixels. The dashboard
    # lands at 2.9 MB against a 3 MB ceiling - close enough that a denser match
    # would cross it, and crossing it is another silent LinkedIn failure.
    while len(data) > MAX_UPLOAD_BYTES and max(padded.size) > 1024:
        padded = _pad_to_ratio(
            _fit_long_edge(padded, int(max(padded.size) * 0.9)), bg)
        data = _encode_png(padded)
    path.write_bytes(data)
    return path


def export_social_jpg(
    src: Path | str,
    *,
    bg: str = '#E7EAE4',
    unified: bool = False,
    suffix: str = '_x',
    out_dir: Path | str | None = None,
) -> Path:
    """Write a flattened, aspect-clamped JPEG for social uploads.

    Lands next to `src` by default; `out_dir` collects the uploads elsewhere.
    """
    src = Path(src)
    out = (Path(out_dir) if out_dir else src.parent) / f'{src.stem}{suffix}.jpg'
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        rgb = _flatten(im, bg)
    long_edge = UNIFIED_LONG_EDGE if unified else PANEL_LONG_EDGE
    return _write_jpeg(_fit_long_edge(rgb, long_edge), out, bg)


def export_figure_jpg(
    fig,
    path: Path | str,
    *,
    bg: str = '#E7EAE4',
    dpi: int = 150,
    unified: bool = False,
    suffix: str = '_x',
) -> Path:
    """Render a matplotlib figure to a PNG buffer, then write the social JPEG."""
    path = Path(path)
    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor=bg, dpi=dpi)
    buf.seek(0)
    with Image.open(buf) as im:
        rgb = _flatten(im, bg)
    long_edge = UNIFIED_LONG_EDGE if unified else PANEL_LONG_EDGE
    out = path.with_name(f'{path.stem}{suffix}.jpg')
    return _write_jpeg(_fit_long_edge(rgb, long_edge), out, bg)
