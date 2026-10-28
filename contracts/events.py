"""Event data schema based on Whoscored event structure."""

from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
import pandas as pd


class EventData(BaseModel):
    """Schema for individual event data."""

    # Core identifiers
    id: int = Field(..., description="Event unique identifier")
    eventId: int = Field(..., description="Event type identifier")
    minute: int = Field(..., ge=0, le=120, description="Match minute")
    second: int = Field(..., ge=0, le=59, description="Second within minute")
    teamId: int = Field(..., description="Team identifier")
    playerId: int = Field(..., description="Player identifier")

    # Coordinates (transformed to 105x68 pitch)
    x: float = Field(..., ge=0, le=105, description="X coordinate on pitch")
    y: float = Field(..., ge=0, le=68, description="Y coordinate on pitch")
    endX: Optional[float] = Field(None, ge=0, le=105, description="End X coordinate")
    endY: Optional[float] = Field(None, ge=0, le=68, description="End Y coordinate")

    # Event details
    expandedMinute: int = Field(..., description="Expanded minute including stoppage")
    period: str = Field(..., description="Period (FirstHalf, SecondHalf, etc)")
    type: str = Field(..., description="Event type (Pass, Shot, etc)")
    outcomeType: str = Field(..., description="Outcome (Successful, Unsuccessful)")
    qualifiers: str = Field(..., description="Event qualifiers as string")

    # Event flags
    satisfiedEventsTypes: str = Field(default="[]", description="Satisfied event types")
    isTouch: int = Field(..., ge=0, le=1, description="Whether event is a touch")
    isShot: Optional[int] = Field(None, ge=0, le=1, description="Whether event is a shot")

    # Optional fields for specific event types
    relatedEventId: Optional[int] = Field(None, description="Related event ID")
    relatedPlayerId: Optional[int] = Field(None, description="Related player ID")
    blockedX: Optional[float] = Field(None, description="Blocked shot X coordinate")
    blockedY: Optional[float] = Field(None, description="Blocked shot Y coordinate")
    goalMouthZ: Optional[float] = Field(None, description="Shot height")
    goalMouthY: Optional[float] = Field(None, ge=0, le=68, description="Shot Y position on goal")

    # Calculated fields
    xT: Optional[float] = Field(None, description="Expected Threat value")
    start_zone_value_xT: Optional[float] = Field(None, description="Start zone xT value")
    end_zone_value_xT: Optional[float] = Field(None, description="End zone xT value")
    x1_bin_xT: Optional[int] = Field(None, description="Start X bin for xT")
    y1_bin_xT: Optional[int] = Field(None, description="Start Y bin for xT")
    x2_bin_xT: Optional[int] = Field(None, description="End X bin for xT")
    y2_bin_xT: Optional[int] = Field(None, description="End Y bin for xT")
    pro: Optional[float] = Field(None, description="Progressive pass distance reduction")

    # Player metadata (joined from PlayerData)
    name: Optional[str] = Field(None, description="Player full name")
    shortName: Optional[str] = Field(None, description="Player short name")
    position: Optional[str] = Field(None, description="Player position")
    shirtNo: Optional[int] = Field(None, description="Shirt number")
    isFirstEleven: Optional[bool] = Field(None, description="Is starting eleven")
    teamName: Optional[str] = Field(None, description="Team name")

    class Config:
        """Pydantic config."""
        extra = "allow"  # Allow extra fields from source data
        validate_assignment = True


class EventDataList(BaseModel):
    """Container for multiple events."""

    events: List[EventData]

    def to_dataframe(self) -> pd.DataFrame:
        """Convert to pandas DataFrame."""
        return pd.DataFrame([event.model_dump() for event in self.events])

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame) -> "EventDataList":
        """Create from pandas DataFrame."""
        events = [EventData(**row) for row in df.to_dict(orient="records")]
        return cls(events=events)
