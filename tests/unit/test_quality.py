"""Unit tests for quality validation service."""

import pytest
import pandas as pd
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent.parent))

from quality.validate import FootballDataValidator


@pytest.fixture
def validator():
    """Create validator instance."""
    return FootballDataValidator()


@pytest.fixture
def valid_events_df():
    """Create valid events DataFrame."""
    return pd.DataFrame({
        'id': [1, 2, 3],
        'eventId': [1, 2, 3],
        'minute': [0, 45, 90],
        'second': [0, 30, 59],
        'teamId': [100, 200, 100],
        'playerId': [1001, 2001, 1002],
        'x': [52.5, 84, 21],
        'y': [34.0, 45.0, 12.0],
        'endX': [63, 94.5, 31.5],
        'endY': [40.8, 51, 17],
        'type': ['Pass', 'Shot', 'Pass'],
        'outcomeType': ['Successful', 'Successful', 'Unsuccessful'],
        'qualifiers': ['', 'KeyPass', ''],
        'isTouch': [1, 1, 1]
    })


@pytest.fixture
def valid_players_df():
    """Create valid players DataFrame."""
    return pd.DataFrame({
        'playerId': [1001, 1002, 2001],
        'name': ['Mohamed Salah', 'Virgil van Dijk', 'Kevin De Bruyne'],
        'shirtNo': [11, 4, 17],
        'position': ['FW', 'DEF', 'MID'],
        'isFirstEleven': [True, True, True],
        'teamId': [100, 100, 200]
    })


def test_schema_validation_valid_events(validator, valid_events_df):
    """Test schema validation passes for valid events."""
    is_valid, errors = validator.validate_schema(valid_events_df, "events")
    assert is_valid
    assert len(errors) == 0


def test_schema_validation_valid_players(validator, valid_players_df):
    """Test schema validation passes for valid players."""
    is_valid, errors = validator.validate_schema(valid_players_df, "players")
    assert is_valid
    assert len(errors) == 0


def test_coordinate_range_validation_valid(validator, valid_events_df):
    """Test coordinate range validation passes for valid data."""
    is_valid, errors = validator.validate_coordinate_ranges(valid_events_df)
    assert is_valid
    assert len(errors) == 0


def test_coordinate_range_validation_invalid_x(validator, valid_events_df):
    """Test coordinate validation fails for invalid x."""
    invalid_df = valid_events_df.copy()
    invalid_df.loc[0, 'x'] = 106  # Out of range

    is_valid, errors = validator.validate_coordinate_ranges(invalid_df)
    assert not is_valid
    assert len(errors) > 0
    assert 'invalid x coordinates' in errors[0].lower()


def test_coordinate_range_validation_invalid_y(validator, valid_events_df):
    """Test coordinate validation fails for invalid y."""
    invalid_df = valid_events_df.copy()
    invalid_df.loc[0, 'y'] = -1  # Out of range

    is_valid, errors = validator.validate_coordinate_ranges(invalid_df)
    assert not is_valid
    assert len(errors) > 0


def test_referential_integrity_valid(validator, valid_events_df, valid_players_df):
    """Test referential integrity passes for valid data."""
    is_valid, errors = validator.validate_referential_integrity(
        valid_events_df, valid_players_df
    )
    assert is_valid
    assert len(errors) == 0


def test_referential_integrity_missing_player(validator, valid_events_df, valid_players_df):
    """Test referential integrity fails when player missing."""
    invalid_events = valid_events_df.copy()
    invalid_events.loc[0, 'playerId'] = 9999  # Non-existent player

    is_valid, errors = validator.validate_referential_integrity(
        invalid_events, valid_players_df
    )
    assert not is_valid
    assert len(errors) > 0
    assert 'not in players' in errors[0].lower()


def test_logical_checks_passes_with_end_coords(validator):
    """Test logical checks pass when passes have end coordinates."""
    df = pd.DataFrame({
        'id': [1],
        'type': ['Pass'],
        'endX': [52.5],
        'endY': [34]
    })

    is_valid, errors = validator.validate_logical_checks(df)
    assert is_valid


def test_logical_checks_fails_passes_without_end_coords(validator):
    """Test logical checks fail when passes missing end coordinates."""
    df = pd.DataFrame({
        'id': [1],
        'type': ['Pass'],
        'endX': [None],
        'endY': [None]
    })

    is_valid, errors = validator.validate_logical_checks(df)
    assert not is_valid
    assert len(errors) > 0


def test_team_balance_valid(validator, valid_players_df):
    """Test team balance validation passes."""
    # Add more players to reach 11 per team
    players = []
    for i in range(11):
        players.append({
            'playerId': 1000 + i,
            'name': f'Player {i}',
            'shirtNo': i + 1,
            'position': 'MID',
            'isFirstEleven': i < 11,
            'teamId': 100
        })
    for i in range(11):
        players.append({
            'playerId': 2000 + i,
            'name': f'Player {i}',
            'shirtNo': i + 1,
            'position': 'MID',
            'isFirstEleven': i < 11,
            'teamId': 200
        })

    df = pd.DataFrame(players)
    is_valid, errors = validator.validate_team_balance(df)
    assert is_valid
    assert len(errors) == 0


def test_team_balance_too_few_players(validator):
    """Test team balance fails with too few players."""
    df = pd.DataFrame({
        'playerId': [1, 2],
        'name': ['Player 1', 'Player 2'],
        'shirtNo': [1, 2],
        'position': ['GK', 'DEF'],
        'isFirstEleven': [True, True],
        'teamId': [100, 100]
    })

    is_valid, errors = validator.validate_team_balance(df)
    assert not is_valid
    assert len(errors) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
