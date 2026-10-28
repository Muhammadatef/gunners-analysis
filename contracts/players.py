"""Player data schema."""

from typing import Optional, List
from pydantic import BaseModel, Field
import pandas as pd


class PlayerData(BaseModel):
    """Schema for player data."""

    playerId: int = Field(..., description="Player unique identifier")
    name: str = Field(..., description="Player full name")
    shirtNo: int = Field(..., ge=0, le=99, description="Shirt number")
    position: str = Field(..., description="Player position (GK, DEF, MID, FW)")
    isFirstEleven: bool = Field(..., description="Is in starting eleven")
    teamId: int = Field(..., description="Team identifier")

    # Optional fields from source
    age: Optional[int] = Field(None, ge=15, le=50, description="Player age")
    height: Optional[int] = Field(None, description="Player height in cm")
    weight: Optional[int] = Field(None, description="Player weight in kg")
    isManOfTheMatch: Optional[bool] = Field(None, description="Man of the match flag")

    class Config:
        """Pydantic config."""
        extra = "allow"
        validate_assignment = True


class PlayerDataList(BaseModel):
    """Container for multiple players."""

    players: List[PlayerData]

    def to_dataframe(self) -> pd.DataFrame:
        """Convert to pandas DataFrame."""
        return pd.DataFrame([player.model_dump() for player in self.players])

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame) -> "PlayerDataList":
        """Create from pandas DataFrame."""
        players = [PlayerData(**row) for row in df.to_dict(orient="records")]
        return cls(players=players)
