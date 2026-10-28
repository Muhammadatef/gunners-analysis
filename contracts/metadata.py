"""Match metadata schema."""

from typing import Dict
from pydantic import BaseModel, Field
from datetime import datetime


class MatchMetadata(BaseModel):
    """Schema for match metadata."""

    matchId: str = Field(..., description="Match identifier")
    date: str = Field(..., description="Match date")
    league: str = Field(..., description="League/competition name")
    gameweek: int = Field(..., ge=1, description="Gameweek number")

    # Team information
    teams: Dict[int, str] = Field(..., description="Team ID to name mapping")
    home_team_id: int = Field(..., description="Home team ID")
    away_team_id: int = Field(..., description="Away team ID")

    # Manual input values (from Fotmob or other source)
    home_xg: float = Field(..., ge=0, description="Home team expected goals")
    away_xg: float = Field(..., ge=0, description="Away team expected goals")
    home_xgot: float = Field(..., ge=0, description="Home team xG on target")
    away_xgot: float = Field(..., ge=0, description="Away team xG on target")

    # Visual configuration
    bg_color: str = Field(default="#FFFFFF", description="Background color")
    line_color: str = Field(default="#000000", description="Line color")
    home_color: str = Field(default="#239F40", description="Home team color")
    away_color: str = Field(default="#012169", description="Away team color")

    class Config:
        """Pydantic config."""
        extra = "allow"
        validate_assignment = True
