from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TournamentLevel(str, Enum):
    ATP = "ATP"
    WTA = "WTA"
    CHALLENGER = "Challenger"
    GRAND_SLAM = "Grand Slam"


class Surface(str, Enum):
    HARD = "Hard"
    CLAY = "Clay"
    GRASS = "Grass"


class Signal(str, Enum):
    BUY = "BUY"
    WAIT = "WAIT"
    SKIP = "SKIP"


@dataclass
class PlayerInfo:
    name: str
    ranking: Optional[int] = None


@dataclass
class MatchData:
    """Raw match data combined from Kalshi + tennis stats."""
    player_fav: PlayerInfo
    player_dog: PlayerInfo
    fav_probability: float          # from Kalshi market price (0.0-1.0)
    kalshi_price: float             # current YES price on Kalshi (cents)
    tournament_name: str
    tournament_level: TournamentLevel
    surface: Surface
    volume: float                   # market volume in dollars
    kalshi_ticker: Optional[str] = None
    kalshi_event_ticker: Optional[str] = None


@dataclass
class AnalysisResult:
    """Output from the decision engine."""
    match: MatchData
    signal: Signal
    target_price: Optional[float]   # our calculated target (0.0-1.0)
    factor: Optional[float]
    ranking_gap: Optional[int]
    skip_reason: Optional[str] = None
    edge: Optional[float] = None    # target - kalshi_price (positive = value)

    @property
    def summary(self) -> str:
        if self.signal == Signal.SKIP:
            return f"SKIP: {self.skip_reason}"
        if self.signal == Signal.WAIT:
            return f"WAIT — Kalshi {self.match.kalshi_price:.0f}¢ > Target {self.target_price*100:.0f}¢"
        return f"BUY — Kalshi {self.match.kalshi_price:.0f}¢ < Target {self.target_price*100:.0f}¢ (edge: {self.edge*100:.1f}¢)"
