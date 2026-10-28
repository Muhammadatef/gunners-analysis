"""Main transformation logic for football event data."""

import json
import os
import re
import logging
from pathlib import Path
from typing import Dict, Tuple
import pandas as pd
import numpy as np
import requests
import sys

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))

from contracts.events import EventDataList
from contracts.players import PlayerDataList
from contracts.metadata import MatchMetadata

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = os.environ.get('GUNNERS_ROOT', str(Path(__file__).parent.parent))

XT_GRID_URL = os.getenv(
    'XT_GRID_URL',
    'https://raw.githubusercontent.com/mckayjohns/youtube-videos/main/data/xT_Grid.csv')

# Where the downloaded grid is kept so it is fetched once, not once per match.
XT_GRID_CACHE = os.getenv('XT_GRID_CACHE', os.path.join(ROOT, 'data', 'xT_Grid.csv'))


class FootballDataTransformer:
    """Transform raw Whoscored HTML to structured event data."""

    def __init__(self, xt_grid_url: str = XT_GRID_URL):
        """Initialize transformer.

        Args:
            xt_grid_url: URL to xT grid CSV, or a local path
        """
        self.xt_grid_url = xt_grid_url
        self.xt_grid = None
        self._load_xt_grid()

    def _load_xt_grid(self):
        """Load the xT grid, preferring the local cache over the network.

        The grid is a fixed 12x8 table that has not changed since it was
        published, but every transform used to re-download it - 38 fetches to
        ingest a season, any one of which can fail. It did: a DNS blip on
        GitHub failed a backfilled match outright, and the same blip during a
        Saturday-night DAG run would fail that fixture with nothing watching.

        So the first successful download is cached under data/ and used from
        then on. The network is only consulted when the cache is missing.
        """
        if os.path.exists(XT_GRID_CACHE):
            self.xt_grid = np.array(pd.read_csv(XT_GRID_CACHE, header=None))
            logger.info(f"xT grid from cache {XT_GRID_CACHE}: shape {self.xt_grid.shape}")
            return

        try:
            logger.info(f"Loading xT grid from {self.xt_grid_url}")
            frame = pd.read_csv(self.xt_grid_url, header=None)
        except Exception as e:
            logger.error(f"Failed to load xT grid: {e}")
            raise

        self.xt_grid = np.array(frame)
        logger.info(f"xT grid loaded: shape {self.xt_grid.shape}")

        try:
            os.makedirs(os.path.dirname(XT_GRID_CACHE), exist_ok=True)
            frame.to_csv(XT_GRID_CACHE, header=False, index=False)
            logger.info(f"cached xT grid -> {XT_GRID_CACHE}")
        except OSError as e:
            # A read-only or root-owned data/ must not fail a transform that
            # has already got what it needed.
            logger.warning(f"could not cache the xT grid: {e}")

    def extract_json_from_html(self, html_path: str, save_output: bool = False) -> str:
        """Extract JSON data from Whoscored HTML.

        Args:
            html_path: Path to HTML file
            save_output: Whether to save extracted JSON to file

        Returns:
            JSON string containing match data
        """
        logger.info(f"Extracting JSON from {html_path}")

        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()

        # Extract using regex pattern from notebook
        regex_pattern = r'(?<=require\.config\.params\["args"\].=.)[\s\S]*?;'
        matches = re.findall(regex_pattern, html)

        if not matches:
            raise ValueError("No match data found in HTML")

        data_txt = matches[0]

        # Add quotations for JSON parser
        data_txt = data_txt.replace('matchId', '"matchId"')
        data_txt = data_txt.replace('matchCentreData', '"matchCentreData"')
        data_txt = data_txt.replace('matchCentreEventTypeJson', '"matchCentreEventTypeJson"')
        data_txt = data_txt.replace('formationIdNameMappings', '"formationIdNameMappings"')
        data_txt = data_txt.replace('};', '}')

        if save_output:
            output_path = Path(html_path).with_suffix('.json')
            with open(output_path, 'w') as f:
                f.write(data_txt)
            logger.info(f"Saved JSON to {output_path}")

        return data_txt

    def extract_data_from_dict(self, data: Dict) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[int, str]]:
        """Extract events, players, and teams from data dict.

        Args:
            data: Parsed JSON dictionary

        Returns:
            Tuple of (events_df, players_df, teams_dict)
        """
        logger.info("Extracting data from dictionary")

        # Extract events
        events_dict = data["matchCentreData"]["events"]

        # Extract teams
        teams_dict = {
            data["matchCentreData"]['home']['teamId']: data["matchCentreData"]['home']['name'],
            data["matchCentreData"]['away']['teamId']: data["matchCentreData"]['away']['name']
        }

        # Extract players
        players_home_df = pd.DataFrame(data["matchCentreData"]['home']['players'])
        players_home_df["teamId"] = data["matchCentreData"]['home']['teamId']

        players_away_df = pd.DataFrame(data["matchCentreData"]['away']['players'])
        players_away_df["teamId"] = data["matchCentreData"]['away']['teamId']

        players_df = pd.concat([players_home_df, players_away_df], ignore_index=True)

        # Create events DataFrame
        events_df = pd.DataFrame(events_dict)

        logger.info(f"Extracted {len(events_df)} events, {len(players_df)} players, {len(teams_dict)} teams")

        return events_df, players_df, teams_dict

    def transform_event_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Transform event type columns from dict to string.

        Args:
            df: Events DataFrame

        Returns:
            Transformed DataFrame
        """
        logger.info("Transforming event columns")

        # type/outcomeType/period arrive as {'value': n, 'displayName': s} dicts.
        # The .str accessor silently yields NaN on dicts, which used to null out
        # these three columns and, with them, every downstream filter.
        def display_name(v):
            if isinstance(v, dict):
                return v.get('displayName')
            if isinstance(v, str):
                match = re.search(r"'displayName': '([^']+)", v)
                return match.group(1) if match else v
            return None

        for col in ('type', 'outcomeType', 'period'):
            if col in df.columns:
                df[col] = df[col].apply(display_name)

        # The qualifier filters downstream are all substring tests, so store the
        # list-of-dicts in its repr form (same shape the notebook works with).
        for col in ('qualifiers', 'satisfiedEventsTypes'):
            if col in df.columns:
                df[col] = df[col].apply(lambda v: v if isinstance(v, str) else str(v))

        # Columns that only appear when the match had the relevant event.
        for col in ('endX', 'endY', 'goalMouthY', 'goalMouthZ', 'isShot', 'isGoal', 'cardType'):
            if col not in df.columns:
                df[col] = np.nan

        return df

    def calculate_xt(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Expected Threat (xT) values for passes.

        Args:
            df: Events DataFrame

        Returns:
            DataFrame with xT values
        """
        logger.info("Calculating xT values")

        # Create copy for xT calculation
        dfxT = df.copy()

        # Filter: successful passes, no corners or throw-ins
        dfxT = dfxT[~dfxT['qualifiers'].str.contains('Corner', na=False) &
                    ~dfxT['qualifiers'].str.contains('ThrowIn', na=False)]
        dfxT = dfxT[(dfxT['type'] == 'Pass') & (dfxT['outcomeType'] == 'Successful')]

        if len(dfxT) == 0:
            logger.warning("No successful passes found for xT calculation")
            return df

        xT_rows, xT_cols = self.xt_grid.shape

        # Bin coordinates into xT grid
        dfxT['x1_bin_xT'] = pd.cut(dfxT['x'], bins=xT_cols, labels=False)
        dfxT['y1_bin_xT'] = pd.cut(dfxT['y'], bins=xT_rows, labels=False)
        dfxT['x2_bin_xT'] = pd.cut(dfxT['endX'], bins=xT_cols, labels=False)
        dfxT['y2_bin_xT'] = pd.cut(dfxT['endY'], bins=xT_rows, labels=False)

        # Look the grid cells up vectorised - positional access inside a row-wise
        # apply (x[0]/x[1]) is not supported from pandas 3.
        bins = ['x1_bin_xT', 'y1_bin_xT', 'x2_bin_xT', 'y2_bin_xT']
        dfxT = dfxT[dfxT[bins].notna().all(axis=1)].copy()
        for col in bins:
            dfxT[col] = dfxT[col].astype(int)

        grid = np.asarray(self.xt_grid)
        dfxT['start_zone_value_xT'] = grid[dfxT['y1_bin_xT'].to_numpy(), dfxT['x1_bin_xT'].to_numpy()]
        dfxT['end_zone_value_xT'] = grid[dfxT['y2_bin_xT'].to_numpy(), dfxT['x2_bin_xT'].to_numpy()]

        # Calculate xT as difference
        dfxT['xT'] = dfxT['end_zone_value_xT'] - dfxT['start_zone_value_xT']

        # Select only xT columns for merging
        xt_columns = ['x1_bin_xT', 'y1_bin_xT', 'x2_bin_xT', 'y2_bin_xT',
                      'start_zone_value_xT', 'end_zone_value_xT', 'xT']

        # Create merge key (use index if available, otherwise event identifiers)
        if df.index.name:
            merge_on = df.index.name
        else:
            df = df.reset_index()
            merge_on = 'index'
            dfxT = dfxT.reset_index()

        # Merge xT values back to original DataFrame
        df = df.merge(dfxT[[merge_on] + xt_columns], on=merge_on, how='left')

        logger.info(f"Calculated xT for {dfxT['xT'].notna().sum()} passes")

        return df

    def transform_coordinates(self, df: pd.DataFrame) -> pd.DataFrame:
        """Transform coordinates from 100x100 to 105x68 pitch.

        Args:
            df: Events DataFrame

        Returns:
            DataFrame with transformed coordinates
        """
        logger.info("Transforming coordinates")

        # Transform x and y coordinates
        df['x'] = df['x'] * 1.05
        df['y'] = df['y'] * 0.68
        df['endX'] = df['endX'] * 1.05
        df['endY'] = df['endY'] * 0.68
        df['goalMouthY'] = df['goalMouthY'] * 0.68

        return df

    def calculate_progressive_passes(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate progressive passes (distance reduction >= 9.144m).

        Args:
            df: Events DataFrame

        Returns:
            DataFrame with progressive pass column
        """
        logger.info("Calculating progressive passes")

        # Center of goal coordinates
        goal_x, goal_y = 105, 34

        # Calculate distance reduction for successful passes in attacking half
        df['pro'] = np.where(
            (df['type'] == 'Pass') &
            (df['outcomeType'] == 'Successful') &
            (df['x'] > 42),
            np.sqrt((goal_x - df['x'])**2 + (goal_y - df['y'])**2) -
            np.sqrt((goal_x - df['endX'])**2 + (goal_y - df['endY'])**2),
            0
        )

        progressive_count = (df['pro'] >= 9.144).sum()
        logger.info(f"Found {progressive_count} progressive passes")

        return df

    def get_short_name(self, full_name: str) -> str:
        """Generate short name from full name.

        Args:
            full_name: Player's full name

        Returns:
            Short name (e.g., "M. Salah")
        """
        if pd.isna(full_name):
            return full_name

        parts = full_name.split()

        if len(parts) == 1:
            return full_name
        elif len(parts) == 2:
            return parts[0][0] + ". " + parts[1]
        else:
            return parts[0][0] + ". " + parts[1][0] + ". " + " ".join(parts[2:])

    def add_player_data(self, events_df: pd.DataFrame, players_df: pd.DataFrame, teams_dict: Dict) -> pd.DataFrame:
        """Add player and team information to events.

        Args:
            events_df: Events DataFrame
            players_df: Players DataFrame
            teams_dict: Team ID to name mapping

        Returns:
            Merged DataFrame
        """
        logger.info("Adding player data to events")

        # Drop unnecessary columns from players_df
        columns_to_drop = ['height', 'weight', 'age', 'isManOfTheMatch', 'field', 'stats',
                          'subbedInPlayerId', 'subbedOutPeriod', 'subbedOutExpandedMinute',
                          'subbedInPeriod', 'subbedInExpandedMinute', 'subbedOutPlayerId', 'teamId']

        # Only drop columns that exist (including teamId from players to avoid suffix)
        columns_to_drop = [col for col in columns_to_drop if col in players_df.columns]
        players_df = players_df.drop(columns=columns_to_drop, errors='ignore')

        # Merge with events
        events_df = events_df.merge(players_df, on='playerId', how='left')

        # Add team names (teamId is from events_df)
        events_df['teamName'] = events_df['teamId'].map(teams_dict)

        # Generate short names
        events_df['shortName'] = events_df['name'].apply(self.get_short_name)

        return events_df

    def transform(self, html_path: str, output_dir: str) -> Tuple[str, str, str]:
        """Full transformation pipeline.

        Args:
            html_path: Path to input HTML file
            output_dir: Directory for output files

        Returns:
            Tuple of (events_csv_path, players_csv_path, metadata_json_path)
        """
        logger.info(f"Starting transformation for {html_path}")

        # Extract JSON
        json_str = self.extract_json_from_html(html_path, save_output=True)
        data = json.loads(json_str)

        # Extract data structures
        events_df, players_df, teams_dict = self.extract_data_from_dict(data)

        # Transform event columns
        events_df = self.transform_event_columns(events_df)

        # Transform coordinates BEFORE calculating xT
        events_df = self.transform_coordinates(events_df)

        # Calculate xT
        events_df = self.calculate_xt(events_df)

        # Calculate progressive passes
        events_df = self.calculate_progressive_passes(events_df)

        # Add player data
        events_df = self.add_player_data(events_df, players_df, teams_dict)

        # Save outputs
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        events_csv = output_path / "EventData.csv"
        players_csv = output_path / "PlayerData.csv"
        metadata_json = output_path / "metadata.json"

        events_df.to_csv(events_csv, index=False)
        players_df.to_csv(players_csv, index=False)

        # Create metadata
        home_team_id = list(teams_dict.keys())[0]
        away_team_id = list(teams_dict.keys())[1]

        metadata = {
            "matchId": str(data.get("matchId", "unknown")),
            "teams": teams_dict,
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "date": "TBD",  # To be filled by orchestrator
            "league": "TBD",
            "gameweek": 0,
            "home_xg": 0.0,
            "away_xg": 0.0,
            "home_xgot": 0.0,
            "away_xgot": 0.0
        }

        with open(metadata_json, 'w') as f:
            json.dump(metadata, f, indent=2)

        logger.info(f"Transformation complete. Outputs saved to {output_path}")
        logger.info(f"  Events: {len(events_df)} rows -> {events_csv}")
        logger.info(f"  Players: {len(players_df)} rows -> {players_csv}")
        logger.info(f"  Metadata: {metadata_json}")

        return str(events_csv), str(players_csv), str(metadata_json)


def main():
    """Main entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="Transform Whoscored HTML to structured data")
    parser.add_argument("html_path", help="Path to input HTML file")
    parser.add_argument("--output-dir", default="/data/processed", help="Output directory")
    parser.add_argument("--xt-grid-url",
                       default="https://raw.githubusercontent.com/mckayjohns/youtube-videos/main/data/xT_Grid.csv",
                       help="URL to xT grid CSV")

    args = parser.parse_args()

    transformer = FootballDataTransformer(xt_grid_url=args.xt_grid_url)
    transformer.transform(args.html_path, args.output_dir)


if __name__ == "__main__":
    main()
