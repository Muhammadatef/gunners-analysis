"""Two teams in one match must never be the same colour.

Six Premier League clubs wear a red within a few OKLab units of Arsenal's
#EF0107 - Bournemouth and Manchester United (#DA291C), Sunderland (#EB172B),
Brentford (#E30613), Nottingham Forest (#DD0000), Liverpool (#C8102E). Arsenal
play each of them twice, so twelve of a season's thirty-eight reports drew both
passing networks and a shared shot map in one indivisible red.

Thresholds are the data-visualisation ones: OKLab distance x100, >= 15 for
normal vision and >= 8 under simulated colour-vision deficiency.
"""

import pytest

from visualizer.palette import (MIN_CVD_DE, MIN_NORMAL_DE, TEAM_ALT_COLORS,
                                TEAM_COLORS, clash, delta_e, resolve,
                                separation, team_alt_color, team_color)

ARSENAL = '#EF0107'


# --- the measure ------------------------------------------------------------

def test_a_colour_is_zero_from_itself():
    assert delta_e(ARSENAL, ARSENAL) == pytest.approx(0.0, abs=1e-9)


def test_red_and_blue_are_far_apart():
    assert delta_e('#EF0107', '#034694') > 30


def test_shorthand_hex_is_accepted():
    assert delta_e('#000', '#000000') == pytest.approx(0.0, abs=1e-9)


def test_a_malformed_colour_raises():
    with pytest.raises(ValueError):
        delta_e('nonsense', ARSENAL)


def test_separation_reports_the_worst_deficiency():
    normal, cvd = separation('#EF0107', '#DA291C')
    assert cvd <= normal
    assert normal < MIN_NORMAL_DE and cvd < MIN_CVD_DE


# --- the clash test ---------------------------------------------------------

@pytest.mark.parametrize('other', [
    '#DA291C',   # Bournemouth, Manchester United
    '#EB172B',   # Sunderland
    '#E30613',   # Brentford
    '#DD0000',   # Nottingham Forest
    '#C8102E',   # Liverpool
])
def test_the_other_reds_clash_with_arsenal(other):
    assert clash(ARSENAL, other) is True


@pytest.mark.parametrize('other', [
    '#034694',   # Chelsea
    '#132257',   # Tottenham
    '#6CABDD',   # Manchester City
    '#FFCD00',   # Leeds
    '#000000',   # Fulham
])
def test_clearly_different_colours_do_not_clash(other):
    assert clash(ARSENAL, other) is False


def test_a_missing_colour_is_not_a_clash():
    assert clash(ARSENAL, None) is False


# --- club lookup ------------------------------------------------------------

@pytest.mark.parametrize('whoscored,sofascore', [
    ('Man Utd', 'Manchester United'),
    ('Bournemouth', 'AFC Bournemouth'),
    ('Spurs', 'Tottenham Hotspur'),
    ('Wolves', 'Wolverhampton'),
    ("Nott'm Forest", 'Nottingham Forest'),
])
def test_either_spelling_finds_the_same_colour(whoscored, sofascore):
    """The old map was keyed on full names, so short names fell through to a
    fallback that was itself Arsenal red."""
    assert team_color(whoscored) == team_color(sofascore) is not None


def test_an_uncurated_club_has_no_colour():
    assert team_color('Real Madrid') is None


# --- resolution -------------------------------------------------------------

def _separable(pair):
    normal, cvd = separation(*pair)
    return normal >= MIN_NORMAL_DE and cvd >= MIN_CVD_DE


@pytest.mark.parametrize('home,away', [
    ('Arsenal', 'Bournemouth'),
    ('Arsenal', 'Man Utd'),
    ('Arsenal', 'Liverpool'),
    ('Arsenal', 'Brentford'),
    ("Arsenal", "Nott'm Forest"),
    ('Arsenal', 'Sunderland'),
    # and with the red club at home, so the *away* side has to change
    ('Bournemouth', 'Arsenal'),
    ('Man Utd', 'Arsenal'),
    ('Liverpool', 'Arsenal'),
    ('Brentford', 'Arsenal'),
    ("Nott'm Forest", 'Arsenal'),
    ('Sunderland', 'Arsenal'),
    # two red clubs that are not Arsenal
    ('Liverpool', 'Man Utd'),
])
def test_a_red_on_red_fixture_is_forced_apart(home, away):
    assert _separable(resolve(home, away))


@pytest.mark.parametrize('home,away', [
    ('Arsenal', 'Chelsea'),
    ('Arsenal', 'Leeds'),
    ('Man City', 'Arsenal'),
    ('Arsenal', 'Newcastle'),
])
def test_a_fixture_that_never_clashed_keeps_both_first_kits(home, away):
    assert resolve(home, away) == (team_color(home), team_color(away))


def test_the_away_side_changes_strip_first():
    """As it would on a Saturday: the home club keeps its kit."""
    home, away = resolve('Arsenal', 'Bournemouth')
    assert home == team_color('Arsenal')
    assert away == team_alt_color('Bournemouth')


def test_an_explicit_colour_is_honoured_when_it_works():
    home, away = resolve('Barcelona', 'Real Madrid', home='#A50044', away='#FEBE10')
    assert (home, away) == ('#A50044', '#FEBE10')


def test_an_explicit_colour_is_still_moved_when_it_would_be_unreadable():
    """An unreadable chart is not what the caller asked for either."""
    assert _separable(resolve('Arsenal', 'Bournemouth', home=ARSENAL, away='#DA291C'))


def test_two_uncurated_clubs_still_get_distinct_defaults():
    assert _separable(resolve('Al Ahly', 'Zamalek'))


# --- the curated data itself ------------------------------------------------

@pytest.mark.parametrize('key', sorted(TEAM_COLORS))
def test_every_curated_colour_is_valid_hex(key):
    delta_e(TEAM_COLORS[key], '#000000')


@pytest.mark.parametrize('key', sorted(TEAM_ALT_COLORS))
def test_every_change_strip_belongs_to_a_club_we_know(key):
    assert key in TEAM_COLORS


@pytest.mark.parametrize('key', sorted(TEAM_ALT_COLORS))
def test_every_change_strip_actually_differs_from_its_first_kit(key):
    assert clash(TEAM_COLORS[key], TEAM_ALT_COLORS[key]) is False
