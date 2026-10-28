"""Data quality validation for football event data."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple
import pandas as pd
import pandera as pa
from pandera import Column, DataFrameSchema, Check

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FootballDataValidator:
    """Validate transformed football event data."""

    def __init__(self):
        """Initialize validator with schemas."""
        self.event_schema = self._create_event_schema()
        self.player_schema = self._create_player_schema()

    def _create_event_schema(self) -> DataFrameSchema:
        """Create validation schema for event data.

        Returns:
            Pandera DataFrameSchema
        """
        return DataFrameSchema({
            "id": Column(int, Check.greater_than_or_equal_to(0)),
            "eventId": Column(int, Check.greater_than_or_equal_to(0)),
            "minute": Column(int, Check.in_range(0, 120)),
            "second": Column(int, Check.in_range(0, 59)),
            "teamId": Column(int, Check.greater_than(0)),
            "playerId": Column(int, Check.greater_than(0)),
            # coerce: an all-integer coordinate column reads back from CSV as
            # int64, which is a perfectly valid coordinate.
            "x": Column(float, Check.in_range(0, 105), coerce=True),
            "y": Column(float, Check.in_range(0, 68), coerce=True),
            "type": Column(str, nullable=True),
            "outcomeType": Column(str, nullable=True),
            "qualifiers": Column(str, nullable=True),
            "isTouch": Column(int, Check.isin([0, 1])),
        }, strict=False)  # Allow extra columns

    def _create_player_schema(self) -> DataFrameSchema:
        """Create validation schema for player data.

        Returns:
            Pandera DataFrameSchema
        """
        return DataFrameSchema({
            "playerId": Column(int, Check.greater_than(0)),
            "name": Column(str, nullable=True),
            "shirtNo": Column(int, Check.in_range(0, 99)),
            "position": Column(str, Check.isin(["GK", "DEF", "MID", "FW"])),
            "isFirstEleven": Column(bool),
            "teamId": Column(int, Check.greater_than(0)),
        }, strict=False)

    def validate_schema(self, df: pd.DataFrame, schema_name: str) -> Tuple[bool, List[str]]:
        """Validate DataFrame against schema.

        Args:
            df: DataFrame to validate
            schema_name: Name of schema ('events' or 'players')

        Returns:
            Tuple of (is_valid, error_messages)
        """
        logger.info(f"Validating {schema_name} schema")

        try:
            if schema_name == "events":
                self.event_schema.validate(df, lazy=True)
            elif schema_name == "players":
                self.player_schema.validate(df, lazy=True)
            else:
                return False, [f"Unknown schema: {schema_name}"]

            logger.info(f"{schema_name} schema validation passed")
            return True, []

        except pa.errors.SchemaErrors as e:
            errors = []
            for err in e.failure_cases.itertuples():
                errors.append(f"{err.column}: {err.check}")
            logger.error(f"{schema_name} schema validation failed: {len(errors)} errors")
            return False, errors

    def validate_coordinate_ranges(self, events_df: pd.DataFrame) -> Tuple[bool, List[str]]:
        """Validate coordinate ranges.

        Args:
            events_df: Events DataFrame

        Returns:
            Tuple of (is_valid, error_messages)
        """
        logger.info("Validating coordinate ranges")
        errors = []

        # Check x coordinates
        invalid_x = events_df[(events_df['x'] < 0) | (events_df['x'] > 105)]
        if len(invalid_x) > 0:
            errors.append(f"Found {len(invalid_x)} events with invalid x coordinates")

        # Check y coordinates
        invalid_y = events_df[(events_df['y'] < 0) | (events_df['y'] > 68)]
        if len(invalid_y) > 0:
            errors.append(f"Found {len(invalid_y)} events with invalid y coordinates")

        # Check endX (where applicable)
        has_endx = events_df['endX'].notna()
        invalid_endx = events_df[has_endx & ((events_df['endX'] < 0) | (events_df['endX'] > 105))]
        if len(invalid_endx) > 0:
            errors.append(f"Found {len(invalid_endx)} events with invalid endX coordinates")

        # Check endY (where applicable)
        has_endy = events_df['endY'].notna()
        invalid_endy = events_df[has_endy & ((events_df['endY'] < 0) | (events_df['endY'] > 68))]
        if len(invalid_endy) > 0:
            errors.append(f"Found {len(invalid_endy)} events with invalid endY coordinates")

        is_valid = len(errors) == 0
        if is_valid:
            logger.info("Coordinate range validation passed")
        else:
            logger.error(f"Coordinate range validation failed: {len(errors)} errors")

        return is_valid, errors

    def validate_referential_integrity(self, events_df: pd.DataFrame, players_df: pd.DataFrame) -> Tuple[bool, List[str]]:
        """Validate referential integrity between events and players.

        Args:
            events_df: Events DataFrame
            players_df: Players DataFrame

        Returns:
            Tuple of (is_valid, error_messages)
        """
        logger.info("Validating referential integrity")
        errors = []

        # Check all playerIds in events exist in players
        event_players = set(events_df['playerId'].unique())
        player_ids = set(players_df['playerId'].unique())

        missing_players = event_players - player_ids
        if missing_players:
            errors.append(f"Found {len(missing_players)} playerIds in events not in players: {list(missing_players)[:5]}")

        is_valid = len(errors) == 0
        if is_valid:
            logger.info("Referential integrity validation passed")
        else:
            logger.error(f"Referential integrity validation failed")

        return is_valid, errors

    def validate_logical_checks(self, events_df: pd.DataFrame) -> Tuple[bool, List[str]]:
        """Validate logical consistency of events.

        Args:
            events_df: Events DataFrame

        Returns:
            Tuple of (is_valid, error_messages)
        """
        logger.info("Validating logical checks")
        errors = []
        warnings = []

        # Shots should have goalMouthY/Z
        shots = events_df[events_df['type'].isin(['Goal', 'SavedShot', 'ShotOnPost', 'MissedShots'])]
        if len(shots) > 0:
            missing_goalmouth = shots[shots['goalMouthY'].isna() & shots['goalMouthZ'].isna()]
            if len(missing_goalmouth) > 0:
                warnings.append(f"Found {len(missing_goalmouth)} shots without goalMouth data")

        # Passes should have endX/endY
        passes = events_df[events_df['type'] == 'Pass']
        if len(passes) > 0:
            missing_end = passes[passes['endX'].isna() | passes['endY'].isna()]
            if len(missing_end) > 0:
                errors.append(f"Found {len(missing_end)} passes without end coordinates")

        # Check for duplicate event IDs
        duplicate_ids = events_df[events_df.duplicated(['id'], keep=False)]
        if len(duplicate_ids) > 0:
            errors.append(f"Found {len(duplicate_ids)} events with duplicate IDs")

        is_valid = len(errors) == 0

        if is_valid:
            logger.info("Logical checks validation passed")
            if warnings:
                logger.warning(f"Validation warnings: {warnings}")
        else:
            logger.error(f"Logical checks validation failed: {len(errors)} errors")

        return is_valid, errors + warnings

    def validate_team_balance(self, players_df: pd.DataFrame) -> Tuple[bool, List[str]]:
        """Validate team balance (11-23 players per team).

        Args:
            players_df: Players DataFrame

        Returns:
            Tuple of (is_valid, error_messages)
        """
        logger.info("Validating team balance")
        errors = []

        team_counts = players_df.groupby('teamId').size()

        for team_id, count in team_counts.items():
            if count < 11:
                errors.append(f"Team {team_id} has only {count} players (minimum 11)")
            elif count > 23:
                errors.append(f"Team {team_id} has {count} players (maximum 23)")

            # Check starting eleven
            starters = players_df[(players_df['teamId'] == team_id) & (players_df['isFirstEleven'] == True)]
            if len(starters) != 11:
                errors.append(f"Team {team_id} has {len(starters)} starters (should be 11)")

        is_valid = len(errors) == 0
        if is_valid:
            logger.info("Team balance validation passed")
        else:
            logger.error(f"Team balance validation failed: {len(errors)} errors")

        return is_valid, errors

    def validate(self, events_csv: str, players_csv: str, output_report: str) -> bool:
        """Run full validation pipeline.

        Args:
            events_csv: Path to events CSV
            players_csv: Path to players CSV
            output_report: Path to save validation report JSON

        Returns:
            True if all validations pass, False otherwise
        """
        logger.info("Starting data quality validation")

        # Load data
        events_df = pd.read_csv(events_csv)
        players_df = pd.read_csv(players_csv)

        logger.info(f"Loaded {len(events_df)} events and {len(players_df)} players")

        # Run validations
        validations = []

        # Schema validation
        events_schema_valid, events_schema_errors = self.validate_schema(events_df, "events")
        validations.append({
            "check": "events_schema",
            "passed": events_schema_valid,
            "errors": events_schema_errors,
            "critical": True
        })

        players_schema_valid, players_schema_errors = self.validate_schema(players_df, "players")
        validations.append({
            "check": "players_schema",
            "passed": players_schema_valid,
            "errors": players_schema_errors,
            "critical": True
        })

        # Coordinate ranges
        coord_valid, coord_errors = self.validate_coordinate_ranges(events_df)
        validations.append({
            "check": "coordinate_ranges",
            "passed": coord_valid,
            "errors": coord_errors,
            "critical": True
        })

        # Referential integrity
        ref_valid, ref_errors = self.validate_referential_integrity(events_df, players_df)
        validations.append({
            "check": "referential_integrity",
            "passed": ref_valid,
            "errors": ref_errors,
            "critical": True
        })

        # Logical checks
        logical_valid, logical_errors = self.validate_logical_checks(events_df)
        validations.append({
            "check": "logical_consistency",
            "passed": logical_valid,
            "errors": logical_errors,
            "critical": False  # Some warnings are acceptable
        })

        # Team balance
        team_valid, team_errors = self.validate_team_balance(players_df)
        validations.append({
            "check": "team_balance",
            "passed": team_valid,
            "errors": team_errors,
            "critical": True
        })

        # Determine overall status
        critical_failures = [v for v in validations if not v["passed"] and v["critical"]]
        overall_status = len(critical_failures) == 0

        # Create report
        report = {
            "status": "PASS" if overall_status else "FAIL",
            "events_count": len(events_df),
            "players_count": len(players_df),
            "validations": validations,
            "critical_failures": len(critical_failures),
            "total_checks": len(validations)
        }

        # Save report
        output_path = Path(output_report)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)

        logger.info(f"Validation report saved to {output_path}")

        if overall_status:
            logger.info("✓ All critical validations passed")
        else:
            logger.error(f"✗ {len(critical_failures)} critical validation(s) failed")
            for failure in critical_failures:
                logger.error(f"  - {failure['check']}: {failure['errors']}")

        return overall_status


def main():
    """Main entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate football event data quality")
    parser.add_argument("--events", default="/data/processed/EventData.csv", help="Path to events CSV")
    parser.add_argument("--players", default="/data/processed/PlayerData.csv", help="Path to players CSV")
    parser.add_argument("--output", default="/data/quality/validation_report.json", help="Output report path")

    args = parser.parse_args()

    validator = FootballDataValidator()
    success = validator.validate(args.events, args.players, args.output)

    # Exit with appropriate code
    import sys
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
