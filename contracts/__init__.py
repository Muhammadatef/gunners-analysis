"""Data contracts for football analytics pipeline."""

from .events import EventData, EventDataList
from .players import PlayerData, PlayerDataList
from .metadata import MatchMetadata

__all__ = [
    "EventData",
    "EventDataList",
    "PlayerData",
    "PlayerDataList",
    "MatchMetadata",
]
