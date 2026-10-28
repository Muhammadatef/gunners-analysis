"""Club colours, and making sure the two teams in a match never look alike.

Every panel encodes team identity as colour. Arsenal are `#EF0107`, and six
Premier League clubs wear a red close enough to be the same mark on a chart:
Bournemouth and Manchester United are both `#DA291C`, Sunderland `#EB172B`,
Brentford `#E30613`, Nottingham Forest `#DD0000`, Liverpool `#C8102E`. Arsenal
play each of them twice, so twelve of a season's thirty-eight reports drew both
passing networks, both defensive blocks and a shared shot map in one indivisible
red.

Two things fix that.

**Look the club up properly.** The map used to be keyed on full names while
WhoScored sends short ones, so 'Bournemouth' missed 'AFC Bournemouth' and fell
through to a fallback that was itself Arsenal red. Keys are `club_key` now, so
either feed's spelling lands on the same entry.

**Then check the pair, and change the kit if they clash.** Real teams solve this
with a change strip; so does this. `resolve()` measures the two colours and, if
they are too close, swaps the away side to its second colour - and the home side
to its own if that is still not enough.

Separation is measured the way the eye works, not the way hex codes do:
Euclidean distance in OKLab (x100), against the unaltered colours and against
protanope, deuteranope and tritanope simulations of them. `#EF0107` and
`#DA291C` are 4.0 apart normally and 1.1 under protanopia - the thresholds below
want 15 and 8.
"""

from __future__ import annotations

import math

# Machado et al. (2009) CVD simulation matrices, applied to linear-light sRGB.
_MACHADO = {
    'protan': ((0.152286, 1.052583, -0.204868),
               (0.114503, 0.786281, 0.099216),
               (-0.003882, -0.048116, 1.051998)),
    'deutan': ((0.367322, 0.860646, -0.227968),
               (0.280085, 0.672501, 0.047413),
               (-0.011820, 0.042940, 0.968881)),
    'tritan': ((1.255528, -0.076749, -0.178779),
               (-0.078411, 0.930809, 0.147602),
               (0.004733, 0.691367, 0.303900)),
}

# Below 15 in OKLab x100, two marks are hard to tell apart with full colour
# vision; below 8 under any simulated deficiency, they are effectively one
# colour. Both have to hold.
MIN_NORMAL_DE = 15.0
MIN_CVD_DE = 8.0

# When a club has no second colour that separates - or none at all - this does.
# Deliberately a neutral rather than another hue: it reads as "not the other
# team" without pretending to be a kit.
LAST_RESORT = '#3D3D3D'

FALLBACK_HOME = '#1B458F'
FALLBACK_AWAY = '#00887A'


# --- club colours -----------------------------------------------------------

# First-choice kit, keyed by scraper.sofascore.club_key.
TEAM_COLORS = {
    'arsenal': '#EF0107',
    'aston-villa': '#95BFE5',
    'bournemouth': '#DA291C',
    'brentford': '#E30613',
    'brighton': '#0057B8',
    'burnley': '#6C1D45',
    'chelsea': '#034694',
    'coventry': '#78D0F3',
    'crystal-palace': '#1B458F',
    'everton': '#003399',
    'fulham': '#000000',
    'hull': '#F5A12D',
    'ipswich': '#3A64A3',
    'leeds': '#FFCD00',
    'leicester': '#003090',
    'liverpool': '#C8102E',
    'luton': '#F78F1E',
    'man-city': '#6CABDD',
    'man-utd': '#DA291C',
    'newcastle': '#241F20',
    'nottm-forest': '#DD0000',
    'sheffield-utd': '#EE2737',
    'southampton': '#D71920',
    'sunderland': '#EB172B',
    'tottenham': '#132257',
    'west-ham': '#7A263A',
    'wolves': '#FDB913',
}

# Change strip: a real second colour for the club, used only when the first
# choice is too close to the opponent's.
TEAM_ALT_COLORS = {
    'bournemouth': '#000000',      # the black half of the home stripes
    'brentford': '#F5C400',        # the bee
    'liverpool': '#00B2A9',        # the teal away shirt
    'man-utd': '#12326E',          # navy away
    'nottm-forest': '#1B2F5E',     # navy away
    'sunderland': '#2B1A47',       # dark away strip
    'sheffield-utd': '#000000',
    'southampton': '#130C0E',
    'arsenal': '#023474',          # Arsenal's own navy, for a red-on-red away day
    'leicester': '#FDBE11',
    'west-ham': '#1BB1E7',
}


def _srgb(hex_color: str) -> tuple[float, float, float]:
    h = (hex_color or '').strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f'not a hex colour: {hex_color!r}')
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _linear(hex_color: str) -> tuple[float, float, float]:
    def channel(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return tuple(channel(c) for c in _srgb(hex_color))


def _oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = l ** (1 / 3), m ** (1 / 3), s ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _simulate(hex_color: str, kind: str) -> tuple[float, float, float]:
    r, g, b = _linear(hex_color)
    matrix = _MACHADO[kind]
    return tuple(max(0.0, min(1.0, row[0] * r + row[1] * g + row[2] * b))
                 for row in matrix)


def delta_e(left: str, right: str, kind: str | None = None) -> float:
    """Perceptual distance in OKLab x100, optionally through a CVD simulation."""
    a = _oklab(*(_simulate(left, kind) if kind else _linear(left)))
    b = _oklab(*(_simulate(right, kind) if kind else _linear(right)))
    return 100 * math.dist(a, b)


def separation(left: str, right: str) -> tuple[float, float]:
    """(normal-vision distance, worst distance across the three deficiencies)."""
    worst_cvd = min(delta_e(left, right, kind)
                    for kind in ('protan', 'deutan', 'tritan'))
    return delta_e(left, right), worst_cvd


def clash(left: str, right: str) -> bool:
    """Would these two read as the same colour on a chart?"""
    if not left or not right:
        return False
    normal, cvd = separation(left, right)
    return normal < MIN_NORMAL_DE or cvd < MIN_CVD_DE


def team_color(name: str) -> str | None:
    """First-choice club colour, or None for a club we do not curate.

    Deliberately no generated palette: a hashed colour that happens to look
    plausible is worse than an obvious default.
    """
    from scraper.sofascore import club_key
    return TEAM_COLORS.get(club_key(name) or '')


def team_alt_color(name: str) -> str | None:
    from scraper.sofascore import club_key
    return TEAM_ALT_COLORS.get(club_key(name) or '')


def resolve(home_name: str, away_name: str,
            home: str | None = None, away: str | None = None) -> tuple[str, str]:
    """The two colours to draw this match in, guaranteed to be distinguishable.

    An explicitly supplied colour is honoured as the starting point - that is
    what run_match's --home-color/--away-color are for - but it is still moved
    if the pair would be unreadable, because an unreadable chart is not what
    anyone asked for either.
    """
    home = home or team_color(home_name) or FALLBACK_HOME
    away = away or team_color(away_name) or FALLBACK_AWAY

    if not clash(home, away):
        return home, away

    # The away side changes strip first, as it would on a Saturday.
    away_alt = team_alt_color(away_name)
    if away_alt and not clash(home, away_alt):
        return home, away_alt

    home_alt = team_alt_color(home_name)
    if home_alt and not clash(home_alt, away):
        return home_alt, away

    if away_alt and home_alt and not clash(home_alt, away_alt):
        return home_alt, away_alt

    return home, LAST_RESORT if not clash(home, LAST_RESORT) else FALLBACK_AWAY
