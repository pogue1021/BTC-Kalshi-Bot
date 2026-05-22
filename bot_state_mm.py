"""
bot_state_mm.py — State for BOT 3.0 (Market Maker Strategy)
=============================================================
Isolated singleton state. Never imports from V1 or V2 state files.
Mirrors the structure of bot_state_v2.py.
"""

import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict

BOT_MM_VERSION = "3.0.0"


@dataclass
class MMSettings:
    """Tuneable parameters for the market-maker strategy."""
    quote_yes_threshold: int     = 85    # quote when YES price is above this
    quote_no_threshold: int      = 15    # quote when YES price is below this
    quote_inside_cents: int      = 2     # post this many cents inside the spread
    min_market_spread_cents: int = 5     # skip if natural spread is tighter (need 2*inside+1 min)
    min_seconds_remaining: int   = 90    # pull all quotes under this many seconds
    max_inventory_contracts: int = 15    # skew quotes when net position exceeds this
    hard_inventory_limit: int    = 25    # pull all quotes when net position exceeds this
    max_bet_dollars: float       = 10.0  # max dollars per individual order
    max_daily_loss: float        = 20.0  # halt quoting if daily P&L drops below -this

    def to_dict(self) -> dict:
        return {
            "quote_yes_threshold":      self.quote_yes_threshold,
            "quote_no_threshold":       self.quote_no_threshold,
            "quote_inside_cents":       self.quote_inside_cents,
            "min_market_spread_cents":  self.min_market_spread_cents,
            "min_seconds_remaining":    self.min_seconds_remaining,
            "max_inventory_contracts":  self.max_inventory_contracts,
            "hard_inventory_limit":     self.hard_inventory_limit,
            "max_bet_dollars":          self.max_bet_dollars,
            "max_daily_loss":           self.max_daily_loss,
        }

    def update_from_dict(self, d: dict):
        if "quote_yes_threshold"     in d: self.quote_yes_threshold     = int(d["quote_yes_threshold"])
        if "quote_no_threshold"      in d: self.quote_no_threshold      = int(d["quote_no_threshold"])
        if "quote_inside_cents"      in d: self.quote_inside_cents      = int(d["quote_inside_cents"])
        if "min_market_spread_cents" in d: self.min_market_spread_cents = int(d["min_market_spread_cents"])
        if "min_seconds_remaining"   in d: self.min_seconds_remaining   = int(d["min_seconds_remaining"])
        if "max_inventory_contracts" in d: self.max_inventory_contracts = int(d["max_inventory_contracts"])
        if "hard_inventory_limit"    in d: self.hard_inventory_limit    = int(d["hard_inventory_limit"])
        if "max_bet_dollars"         in d: self.max_bet_dollars         = float(d["max_bet_dollars"])
        if "max_daily_loss"          in d: self.max_daily_loss          = float(d["max_daily_loss"])


@dataclass
class MMFillRecord:
    """Record of one executed order fill."""
    fill_id:      str
    ticker:       str
    side:         str               # 'yes_buy' or 'yes_sell'
    price_cents:  int               # YES-equivalent price
    contracts:    int
    filled_at:    float             # unix timestamp
    pnl_dollars:  Optional[float] = None   # set when round-trip closes or market settles

    def to_dict(self) -> dict:
        return {
            "fill_id":     self.fill_id,
            "ticker":      self.ticker,
            "side":        self.side,
            "price_cents": self.price_cents,
            "contracts":   self.contracts,
            "filled_at":   self.filled_at,
            "pnl_dollars": self.pnl_dollars,
        }


class MMBotState:
    """
    Central state store for BOT 3.0 (market maker).
    Written by strategy_mm.py, read by the dashboard HTTP server.
    All fields safe for concurrent read from the dashboard thread.
    """

    def __init__(self):
        self.settings        = MMSettings()
        self.trading_enabled = False
        self.paper_mode      = True
        self.status          = "Disarmed — press ARM to start"
        self.started_at      = time.time()

        # Live price data (synced from main price feeds each loop)
        self.cf_estimate:     Optional[float] = None
        self.feeds_connected: bool            = False

        # Per-market inventory: ticker → net YES contracts
        # Positive = long YES, negative = long NO (short YES)
        self.inventory: Dict[str, int] = {}

        # Per-market resting orders: ticker → list of order dicts
        # Each order: {order_id, side ('yes'/'no'), price_cents, contracts, placed_at, paper}
        self.resting_orders: Dict[str, List[dict]] = {}

        # FIFO cost queue for realized P&L: ticker → [(price_cents, contracts), ...]
        # Entries represent unmatched YES long positions at a given cost basis.
        self._buy_queue: Dict[str, List[tuple]] = {}

        # Dashboard-facing market summaries (rebuilt each loop)
        self.active_markets_info: List[dict] = []

        # P&L counters
        self.daily_pnl_dollars:  float = 0.0
        self.total_pnl_dollars:  float = 0.0
        self.total_fills:        int   = 0
        self.total_round_trips:  int   = 0

        # Fill history (most recent first, capped at 50)
        self.fills: List[MMFillRecord] = []

    def add_fill(self, fill: MMFillRecord):
        self.fills.insert(0, fill)
        if len(self.fills) > 50:
            self.fills = self.fills[:50]
        self.total_fills += 1

    def record_realized_pnl(self, pnl_dollars: float):
        self.daily_pnl_dollars  += pnl_dollars
        self.total_pnl_dollars  += pnl_dollars
        self.total_round_trips  += 1

    def record_settlement_pnl(self, pnl_dollars: float):
        self.daily_pnl_dollars += pnl_dollars
        self.total_pnl_dollars += pnl_dollars

    def to_dict(self) -> dict:
        uptime_secs = int(time.time() - self.started_at)
        h, rem = divmod(uptime_secs, 3600)
        m, s   = divmod(rem, 60)

        # Build per-ticker inventory summary for dashboard
        inventory_summary = {
            k: v for k, v in self.inventory.items() if v != 0
        }

        return {
            "status":          self.status,
            "trading_enabled": self.trading_enabled,
            "paper_mode":      self.paper_mode,
            "uptime":          f"{h:02d}:{m:02d}:{s:02d}",
            "version":         BOT_MM_VERSION,
            "prices": {
                "cf_estimate":     self.cf_estimate,
                "feeds_connected": self.feeds_connected,
            },
            "markets":   self.active_markets_info,
            "account": {
                "daily_pnl":       round(self.daily_pnl_dollars, 4),
                "total_pnl":       round(self.total_pnl_dollars, 4),
                "total_fills":     self.total_fills,
                "total_round_trips": self.total_round_trips,
                "daily_loss_limit":  self.settings.max_daily_loss,
                "loss_remaining":    round(
                    self.settings.max_daily_loss + self.daily_pnl_dollars, 2
                ),
            },
            "inventory": inventory_summary,
            "fills":     [f.to_dict() for f in self.fills],
            "settings":  self.settings.to_dict(),
        }


# Global singleton shared across strategy_mm.py and dashboard_server.py
state_mm = MMBotState()
