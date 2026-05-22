"""
bot_state_v2.py — State for BOT 2.0 (Late-Window Edge Strategy)
================================================================
Simpler than V1 state — tracks only what the late-window strategy needs.
Always paper mode until manually switched.
"""

import time
from dataclasses import dataclass
from typing import Optional, List


BOT_V2_VERSION = "2.0.0"


@dataclass
class V2Settings:
    """Tuneable parameters for the late-window strategy."""

    # Only look for entries in the final N minutes of each cycle
    entry_window_minutes: float = 5.0

    # BTC must be at least this many dollars from the strike price.
    # At $75 distance with 3 min left, YES should be ~85c — if Kalshi
    # is still at 65-75c, that's the mispricing edge we're hunting.
    min_distance_dollars: float = 75.0

    # For YES trades: only enter if Kalshi YES price is BELOW this.
    # If YES is already 85c+, the market caught up — no edge left.
    max_entry_yes_cents: int = 80

    # For NO trades: only enter if Kalshi YES price is ABOVE this.
    # If YES is already cheap (<20c), NO has no edge left.
    min_entry_no_yes_cents: int = 20

    # Position size
    max_bet_dollars: float = 5.0

    def to_dict(self) -> dict:
        return {
            "entry_window_minutes":    self.entry_window_minutes,
            "min_distance_dollars":    self.min_distance_dollars,
            "max_entry_yes_cents":     self.max_entry_yes_cents,
            "min_entry_no_yes_cents":  self.min_entry_no_yes_cents,
            "max_bet_dollars":         self.max_bet_dollars,
        }

    def update_from_dict(self, d: dict):
        if "entry_window_minutes" in d:
            self.entry_window_minutes = float(d["entry_window_minutes"])
        if "min_distance_dollars" in d:
            self.min_distance_dollars = float(d["min_distance_dollars"])
        if "max_entry_yes_cents" in d:
            self.max_entry_yes_cents = int(d["max_entry_yes_cents"])
        if "min_entry_no_yes_cents" in d:
            self.min_entry_no_yes_cents = int(d["min_entry_no_yes_cents"])
        if "max_bet_dollars" in d:
            self.max_bet_dollars = float(d["max_bet_dollars"])


@dataclass
class V2TradeRecord:
    trade_id:      str
    ticker:        str
    side:          str
    price_cents:   int
    num_contracts: int
    cost_dollars:  float
    opened_at:     float
    entry_btc:     float   # BTC price at entry
    entry_strike:  float   # strike price at entry
    closed_at:     Optional[float] = None
    pnl_dollars:   Optional[float] = None
    outcome:       Optional[str]   = None  # "win" / "loss"

    def to_dict(self) -> dict:
        return {
            "trade_id":      self.trade_id,
            "ticker":        self.ticker,
            "side":          self.side.upper(),
            "price_cents":   self.price_cents,
            "num_contracts": self.num_contracts,
            "cost_dollars":  self.cost_dollars,
            "opened_at":     self.opened_at,
            "entry_btc":     self.entry_btc,
            "entry_strike":  self.entry_strike,
            "closed_at":     self.closed_at,
            "pnl_dollars":   self.pnl_dollars,
            "outcome":       self.outcome,
        }


class V2BotState:
    """
    Central state store for BOT 2.0.
    Written by strategy_v2.py, read by the dashboard server.
    """

    def __init__(self):
        self.settings        = V2Settings()
        self.trading_enabled = False
        self.paper_mode      = True   # Always paper until manually switched
        self.status          = "Waiting to arm..."

        # Live market data (shared from main price feeds)
        self.cf_estimate:          Optional[float] = None
        self.floor_strike:         Optional[float] = None
        self.distance_dollars:     Optional[float] = None   # positive = above, negative = below
        self.current_market:       Optional[str]   = None
        self.seconds_until_close:  Optional[float] = None
        self.market_yes_price:     Optional[int]   = None
        self.market_no_price:      Optional[int]   = None
        self.feeds_connected:      bool            = False

        # Current open position (None = flat)
        self.current_trade: Optional[V2TradeRecord] = None
        self.traded_this_cycle: bool = False

        # Session stats (paper)
        self.total_pnl:    float = 0.0
        self.daily_pnl:    float = 0.0
        self.win_count:    int   = 0
        self.loss_count:   int   = 0
        self.started_at:   float = time.time()

        # Trade history (most recent first, capped at 50)
        self.trades: List[V2TradeRecord] = []

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return round(self.win_count / total * 100, 1) if total > 0 else 0.0

    def add_trade(self, trade: V2TradeRecord):
        self.trades.insert(0, trade)
        if len(self.trades) > 50:
            self.trades = self.trades[:50]

    def close_trade(self, trade_id: str, pnl: float, outcome: str, closed_at: float):
        for t in self.trades:
            if t.trade_id == trade_id:
                t.pnl_dollars = pnl
                t.outcome     = outcome
                t.closed_at   = closed_at
                break
        self.total_pnl += pnl
        self.daily_pnl += pnl
        if outcome == "win":
            self.win_count += 1
        else:
            self.loss_count += 1

    def to_dict(self) -> dict:
        uptime_secs = int(time.time() - self.started_at)
        h, rem = divmod(uptime_secs, 3600)
        m, s   = divmod(rem, 60)

        current_trade_dict = None
        if self.current_trade:
            current_trade_dict = self.current_trade.to_dict()

        return {
            "status":          self.status,
            "trading_enabled": self.trading_enabled,
            "paper_mode":      self.paper_mode,
            "uptime":          f"{h:02d}:{m:02d}:{s:02d}",
            "version":         BOT_V2_VERSION,
            "market": {
                "ticker":          self.current_market,
                "seconds_left":    self.seconds_until_close,
                "floor_strike":    self.floor_strike,
                "yes_price_cents": self.market_yes_price,
                "no_price_cents":  self.market_no_price,
            },
            "prices": {
                "cf_estimate":     self.cf_estimate,
                "distance_dollars": self.distance_dollars,
                "feeds_connected": self.feeds_connected,
            },
            "position": current_trade_dict,
            "traded_this_cycle": self.traded_this_cycle,
            "account": {
                "total_pnl":  round(self.total_pnl, 2),
                "daily_pnl":  round(self.daily_pnl, 2),
                "wins":       self.win_count,
                "losses":     self.loss_count,
                "win_rate":   self.win_rate,
            },
            "trades":   [t.to_dict() for t in self.trades],
            "settings": self.settings.to_dict(),
        }


# Global singleton
state_v2 = V2BotState()
