#!/usr/bin/env python3
"""Generate airflow/fixtures/arsenal_2026_27.json from the published fixture list.

Source: premierleague.com + skysports.com Arsenal 2026/27 fixture lists
(cross-checked; both agree on all 38 matches).

Kick-off times are UK local. Where the broadcaster slot was not yet published
the time is left null and the DAG falls back to DEFAULT_KICKOFF.
"""

import json
import os

SEASON = '2026/2027'
SEASON_SLUG = 'england-premier-league-2026-2027'
ARSENAL = 'Arsenal'

# WhoScored URL slugs for every 2026/27 Premier League club.
SLUGS = {
    'Arsenal': 'arsenal',
    'Aston Villa': 'aston-villa',
    'AFC Bournemouth': 'bournemouth',
    'Brentford': 'brentford',
    'Brighton & Hove Albion': 'brighton',
    'Chelsea': 'chelsea',
    'Coventry City': 'coventry',
    'Crystal Palace': 'crystal-palace',
    'Everton': 'everton',
    'Fulham': 'fulham',
    'Hull City': 'hull',
    'Ipswich Town': 'ipswich',
    'Leeds United': 'leeds',
    'Liverpool': 'liverpool',
    'Manchester City': 'manchester-city',
    'Manchester United': 'manchester-united',
    # WhoScored drops the "United" here (verified against the live fixtures page).
    'Newcastle United': 'newcastle',
    'Nottingham Forest': 'nottingham-forest',
    'Sunderland': 'sunderland',
    'Tottenham Hotspur': 'tottenham',
}

# (matchweek, date, kickoff_uk or None, opponent, 'H' or 'A')
RAW = [
    (1,  '2026-08-21', '20:00', 'Coventry City', 'H'),
    (2,  '2026-08-29', None,    'Aston Villa', 'A'),
    (3,  '2026-09-05', None,    'Chelsea', 'H'),
    (4,  '2026-09-12', '20:00', 'Sunderland', 'A'),
    (5,  '2026-09-19', None,    'Brighton & Hove Albion', 'A'),
    (6,  '2026-10-10', None,    'Leeds United', 'H'),
    (7,  '2026-10-17', None,    'Nottingham Forest', 'A'),
    (8,  '2026-10-24', None,    'Everton', 'H'),
    (9,  '2026-10-31', None,    'Liverpool', 'A'),
    (10, '2026-11-07', None,    'Hull City', 'H'),
    (11, '2026-11-21', None,    'Newcastle United', 'A'),
    (12, '2026-11-28', None,    'Manchester City', 'H'),
    (13, '2026-12-02', '20:00', 'Brentford', 'A'),
    (14, '2026-12-05', None,    'Tottenham Hotspur', 'A'),
    (15, '2026-12-12', None,    'AFC Bournemouth', 'H'),
    (16, '2026-12-19', None,    'Manchester United', 'H'),
    (17, '2026-12-26', None,    'Crystal Palace', 'A'),
    (18, '2026-12-30', '20:00', 'Fulham', 'A'),
    (19, '2027-01-02', None,    'Ipswich Town', 'H'),
    (20, '2027-01-06', '20:00', 'Brentford', 'H'),
    (21, '2027-01-16', None,    'Hull City', 'A'),
    (22, '2027-01-23', None,    'Newcastle United', 'H'),
    (23, '2027-01-30', None,    'Manchester City', 'A'),
    (24, '2027-02-06', None,    'Liverpool', 'H'),
    (25, '2027-02-10', '20:00', 'Ipswich Town', 'A'),
    (26, '2027-02-20', None,    'Fulham', 'H'),
    (27, '2027-02-27', None,    'Manchester United', 'A'),
    (28, '2027-03-03', '20:00', 'Crystal Palace', 'H'),
    (29, '2027-03-13', None,    'Chelsea', 'A'),
    (30, '2027-03-20', None,    'Sunderland', 'H'),
    (31, '2027-04-10', None,    'Coventry City', 'A'),
    (32, '2027-04-17', None,    'Aston Villa', 'H'),
    (33, '2027-04-24', None,    'AFC Bournemouth', 'A'),
    (34, '2027-05-01', None,    'Tottenham Hotspur', 'H'),
    (35, '2027-05-08', None,    'Leeds United', 'A'),
    (36, '2027-05-15', None,    'Nottingham Forest', 'H'),
    (37, '2027-05-23', None,    'Everton', 'A'),
    (38, '2027-05-30', None,    'Brighton & Hove Albion', 'H'),
]


def build():
    fixtures = []
    for mw, date, ko, opponent, venue in RAW:
        home = ARSENAL if venue == 'H' else opponent
        away = opponent if venue == 'H' else ARSENAL
        fixtures.append({
            'matchweek': mw,
            'date': date,
            'kickoff_uk': ko,
            'kickoff_confirmed': ko is not None,
            'opponent': opponent,
            'venue': venue,
            'home_team': home,
            'away_team': away,
            # WhoScored match ids are only minted a few weeks out, so the URL is
            # resolved at run time from the slug (see scraper/fixtures.py).
            'whoscored_slug': f'{SEASON_SLUG}-{SLUGS[home]}-{SLUGS[away]}',
            'fixture_key': f'gw{mw:02d}_{date}_{SLUGS[home]}_v_{SLUGS[away]}',
        })
    return {
        'season': SEASON,
        'competition': 'Premier League',
        'team': ARSENAL,
        'source': [
            'https://www.premierleague.com/en/news/4675132/all-of-arsenals-fixtures-for-202627-premier-league-season',
            'https://www.skysports.com/football/news/11095/13555674/arsenal-premier-league-2026-27-fixtures-and-schedule',
        ],
        'notes': 'Kick-off times are UK local. Null kickoff_uk = broadcast slot not yet '
                 'confirmed; the DAG uses DEFAULT_KICKOFF (15:00) until the fixture is '
                 'refreshed. Re-run this script when the PL confirms TV picks.',
        'fixtures': fixtures,
    }


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arsenal_2026_27.json')
    data = build()
    with open(out, 'w') as fh:
        json.dump(data, fh, indent=2)
    home = sum(1 for f in data['fixtures'] if f['venue'] == 'H')
    print(f"wrote {out}: {len(data['fixtures'])} fixtures ({home} home / {38 - home} away)")
