"""Unit tests for transformer service."""

import pytest
import pandas as pd
import numpy as np
from pathlib import Path
import sys

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent.parent))

from transformer.transform import FootballDataTransformer


@pytest.fixture
def transformer():
    """Create transformer instance."""
    return FootballDataTransformer()


def test_coordinate_transformation(transformer):
    """Test coordinate transformation from 100x100 to 105x68."""
    df = pd.DataFrame({
        'x': [0, 50, 100],
        'y': [0, 50, 100],
        'endX': [10, 60, 90],
        'endY': [10, 60, 90],
        'goalMouthY': [50, 50, 50],
    })

    result = transformer.transform_coordinates(df)

    # Float scaling is not exact (0.68 * 10 != 6.8), so compare approximately.
    # Check x transformation (multiply by 1.05)
    assert result['x'].tolist() == pytest.approx([0, 52.5, 105])

    # Check y transformation (multiply by 0.68)
    assert result['y'].tolist() == pytest.approx([0, 34, 68])

    # Check endX
    assert result['endX'].tolist() == pytest.approx([10.5, 63, 94.5])

    # Check endY
    assert result['endY'].tolist() == pytest.approx([6.8, 40.8, 61.2])


def test_progressive_pass_calculation(transformer):
    """Test progressive pass distance calculation."""
    df = pd.DataFrame({
        'type': ['Pass', 'Pass', 'Pass'],
        'outcomeType': ['Successful', 'Successful', 'Unsuccessful'],
        'x': [50, 60, 70],
        'y': [34, 34, 34],
        'endX': [70, 80, 90],
        'endY': [34, 34, 34]
    })

    result = transformer.calculate_progressive_passes(df)

    # First two should have positive progressive values
    assert result['pro'][0] > 0
    assert result['pro'][1] > 0

    # Unsuccessful pass should have pro = 0
    assert result['pro'][2] == 0

    # Check first pass is progressive (>= 9.144m)
    assert result['pro'][0] >= 9.144


def test_short_name_generation(transformer):
    """Test short name generation."""
    assert transformer.get_short_name("Mohamed Salah") == "M. Salah"
    # Split is on whitespace, so a hyphenated surname stays one token.
    assert transformer.get_short_name("Trent Alexander-Arnold") == "T. Alexander-Arnold"
    # Three or more tokens abbreviate the first two.
    assert transformer.get_short_name("Luis Diaz Marulanda") == "L. D. Marulanda"
    assert transformer.get_short_name("Ronaldo") == "Ronaldo"
    assert pd.isna(transformer.get_short_name(np.nan))


def test_xt_grid_loading(transformer):
    """Test xT grid loads correctly."""
    assert transformer.xt_grid is not None
    assert transformer.xt_grid.shape == (8, 12)  # 8 rows, 12 columns
    assert np.all(transformer.xt_grid >= 0)  # All values should be non-negative


def test_xt_calculation_bins():
    """Test xT binning logic."""
    transformer = FootballDataTransformer()

    df = pd.DataFrame({
        'type': ['Pass'],
        'outcomeType': ['Successful'],
        'qualifiers': [''],
        'x': [52.5],  # Middle of pitch
        'y': [34],    # Middle of pitch
        'endX': [84],  # Final third
        'endY': [34]
    })

    result = transformer.calculate_xt(df)

    # Should have xT bins calculated
    assert 'x1_bin_xT' in result.columns
    assert 'y1_bin_xT' in result.columns
    assert 'xT' in result.columns

    # xT should be calculated (end value - start value)
    assert pd.notna(result['xT'].iloc[0])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
