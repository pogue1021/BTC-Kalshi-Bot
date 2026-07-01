"""
bot_state.py — Shared Bot State
================================
A single object that every part of the bot writes to.
The dashboard server reads from this to show live data.
Settings can be updated live from the dashboard without restarting.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

# Bump this any time you push a meaningful change — visible in the dashboard
# footer so you can instantly confirm a Pi pull/restart picked up the new code.
BOT_VERSION = "1.2.0"


@dataclass
class LiveSettings:
    """
    Tuneable bot parameters. Updated live from the dashboard.
    The signal engine reads from this on every evaluation.
    """
    # When to look for trades (minutes before market close)
    trade_window_start_minutes: float = 5.0
    trade_window_end_minutes:   float = 0.5

    # Signal sensitivity
    momentum_threshold_pct:   float = 0.020
    momentum_window_secs:     int   = 20

    # Market price filter
    min_yes_price_cents: int = 39
    max_yes_price_cents: int = 82

    # Position sizing
    max_bet_dollars: float = 10.0

    # Stop-loss / multi-trade settings
    stop_loss_cents:      int   = 23
    take_profit_cents:    int   = 95
    sl_min_hold_secs:     int   = 15
    sl_disable_mins:      float = 3.0
    signal_sl_disable_mins: float = 3.0
    price_sl_disable_mins:  float = 0.5
    max_trades_per_cycle: int   = 1

    # Signal-based stop-loss
    signal_stop_enabled:          bool = True
    signal_stop_persistence_secs: int  = 15
    stop_loss_fallback_cents:     int  = 20

    # Early entry settings
    early_entry_window_minutes: float = 10.0
    early_min_distance_pct:     float = 0.07
    early_max_yes_cents:        int   = 55

    # Late-window fallback
    late_window_fallback_enabled:  bool  = True
    late_window_fallback_minutes:  float = 2.0
    late_window_min_distance_pct:  float = 0.03
    late_window_max_yes_cents:     int   = 82

    # Wrong-side entry guard
    max_wrong_side_distance_pct:   float = 0.025

    # Near-strike gate: CF closer to the strike than this % is "near" (coin-flip
    # territory). Was hardcoded at 0.03 in signal_engine. When near, the bot can
    # only enter via the momentum path — and only if that path is enabled below.
    min_strike_distance_pct:       float = 0.03
    near_strike_momentum_enabled:  bool  = True

    # Minimum confidence score
    min_confidence_pct: float = 0.55

    # Post stop-loss cooldown
    sl_cooldown_secs: int = 120

    # Risk / money management
    max_daily_loss:        float = 75.0
    min_daily_profit_lock: float = 0.0

    def to_dict(self) -> dict:
        return {
            "trade_window_start_minutes": self.trade_window_start_minutes,
            "trade_window_end_minutes":   self.trade_window_end_minutes,
            "momentum_threshold_pct":     self.momentum_threshold_pct,
            "momentum_window_secs":       self.momentum_window_secs,
            "min_yes_price_cents":        self.min_yes_price_cents,
            "max_yes_price_cents":        self.max_yes_price_cents,
            "max_bet_dollars":            self.max_bet_dollars,
            "stop_loss_cents":            self.stop_loss_cents,
            "take_profit_cents":          self.take_profit_cents,
            "sl_min_hold_secs":           self.sl_min_hold_secs,
            "sl_disable_mins":            self.sl_disable_mins,
            "signal_sl_disable_mins":     self.signal_sl_disable_mins,
            "price_sl_disable_mins":      self.price_sl_disable_mins,
            "max_trades_per_cycle":       self.max_trades_per_cycle,
            "signal_stop_enabled":          self.signal_stop_enabled,
            "signal_stop_persistence_secs": self.signal_stop_persistence_secs,
            "stop_loss_fallback_cents":     self.stop_loss_fallback_cents,
            "early_entry_window_minutes": self.early_entry_window_minutes,
            "early_min_distance_pct":     self.early_min_distance_pct,
            "early_max_yes_cents":        self.early_max_yes_cents,
            "late_window_fallback_enabled":  self.late_window_fallback_enabled,
            "late_window_fallback_minutes":  self.late_window_fallback_minutes,
            "late_window_min_distance_pct":  self.late_window_min_distance_pct,
            "late_window_max_yes_cents":     self.late_window_max_yes_cents,
            "max_wrong_side_distance_pct":   self.max_wrong_side_distance_pct,
            "min_strike_distance_pct":       self.min_strike_distance_pct,
            "near_strike_momentum_enabled":  self.near_strike_momentum_enabled,
            "min_confidence_pct":            self.min_confidence_pct,
            "sl_cooldown_secs":              self.sl_cooldown_secs,
            "max_daily_loss":               self.max_daily_loss,
            "min_daily_profit_lock":        self.min_daily_profit_lock,
        }

    def update_from_dict(self, d: dict):
        if "trade_window_start_minutes" in d:
            self.trade_window_start_minutes = float(d["trade_window_start_minutes"])
        if "trade_window_end_minutes" in d:
            self.trade_window_end_minutes = float(d["trade_window_end_minutes"])
        if "momentum_threshold_pct" in d:
            self.momentum_threshold_pct = float(d["momentum_threshold_pct"])
        if "momentum_window_secs" in d:
            self.momentum_window_secs = int(d["momentum_window_secs"])
        if "min_yes_price_cents" in d:
            self.min_yes_price_cents = int(d["min_yes_price_cents"])
        if "max_yes_price_cents" in d:
            self.max_yes_price_cents = int(d["max_yes_price_cents"])
        if "max_bet_dollars" in d:
            self.max_bet_dollars = float(d["max_bet_dollars"])
        if "stop_loss_cents" in d:
            self.stop_loss_cents = int(d["stop_loss_cents"])
        if "take_profit_cents" in d:
            self.take_profit_cents = int(d["take_profit_cents"])
        if "sl_min_hold_secs" in d:
            self.sl_min_hold_secs = int(d["sl_min_hold_secs"])
        if "sl_disable_mins" in d:
            self.sl_disable_mins = float(d["sl_disable_mins"])
        if "signal_sl_disable_mins" in d:
            self.signal_sl_disable_mins = float(d["signal_sl_disable_mins"])
        if "price_sl_disable_mins" in d:
            self.price_sl_disable_mins = float(d["price_sl_disable_mins"])
        if "max_trades_per_cycle" in d:
            self.max_trades_per_cycle = int(d["max_trades_per_cycle"])
        if "signal_stop_enabled" in d:
            self.signal_stop_enabled = bool(d["signal_stop_enabled"])
        if "signal_stop_persistence_secs" in d:
            self.signal_stop_persistence_secs = int(d["signal_stop_persistence_secs"])
        if "stop_loss_fallback_cents" in d:
            self.stop_loss_fallback_cents = int(d["stop_loss_fallback_cents"])
        if "early_entry_window_minutes" in d:
            self.early_entry_window_minutes = float(d["early_entry_window_minutes"])
        if "early_min_distance_pct" in d:
            self.early_min_distance_pct = float(d["early_min_distance_pct"])
        if "early_max_yes_cents" in d:
            self.early_max_yes_cents = int(d["early_max_yes_cents"])
        if "late_window_fallback_enabled" in d:
            self.late_window_fallback_enabled = bool(d["late_window_fallback_enabled"])
        if "late_window_fallback_minutes" in d:
            self.late_window_fallback_minutes = float(d["late_window_fallback_minutes"])
        if "late_window_min_distance_pct" in d:
            self.late_window_min_distance_pct = float(d["late_window_min_distance_pct"])
        if "late_window_max_yes_cents" in d:
            self.late_window_max_yes_cents = int(d["late_window_max_yes_cents"])
        if "max_wrong_side_distance_pct" in d:
            self.max_wrong_side_distance_pct = float(d["max_wrong_side_distance_pct"])
        if "min_strike_distance_pct" in d:
            self.min_strike_distance_pct = float(d["min_strike_distance_pct"])
        if "near_strike_momentum_enabled" in d:
            self.near_strike_momentum_enabled = bool(d["near_strike_momentum_enabled"])
        if "min_confidence_pct" in d:
            self.min_confidence_pct = float(d["min_confidence_pct"])
        if "sl_cooldown_secs" in d:
            self.sl_cooldown_secs = int(d["sl_cooldown_secs"])
        if "max_daily_loss" in d:
            self.max_daily_loss = float(d["max_daily_loss"])
        if "min_daily_profit_lock" in d:
            self.min_daily_profit_lock = float(d["min_daily_profit_lock"])


@dataclass
class TradeRecord:
    trade_id:      str
    ticker:        str
    side:          str
    price_cents:   int
    num_contracts: int
    cost_dollars:  float
    opened_at:     float
    closed_at:     Optional[float] = None
    pnl_dollars:   Optional[float] = None
    outcome:       Optional[str]   = None  # "win", "loss"

    def to_dict(self) -> dict:
        return {
            "trade_id":      self.trade_id,
            "ticker":        self.ticker,
            "side":          self.side.upper(),
            "price_cents":   self.price_cents,
            "num_contracts": self.num_contracts,
            "cost_dollars":  self.cost_dollars,
            "opened_at":     self.opened_at,
            "closed_at":     self.closed_at,
            "pnl_dollars":   self.pnl_dollars,
            "outcome":       self.outcome,
        }


class BotState:
    """
    Central state store. Written to by main.py, read by the dashboard server.
    All fields have safe defaults so the dashboard works even before the bot
    has fully started.
    """

    def __init__(self):
        # Live-adjustable settings (readable by signal engine and risk manager)
        self.settings = LiveSettings()
        # Price feeds -- all four CF Benchmarks constituent exchanges
        self.kraken_price:    Optional[float] = None   # fast feed
        self.coinbase_price:  Optional[float] = None   # highest weight in BRR
        self.bitstamp_price:  Optional[float] = None
        self.gemini_price:    Optional[float] = None
        self.cf_estimate:     Optional[float] = None   # weighted average of live feeds
        self.cf_momentum_pct: float = 0.0              # CF estimate % change over window
        self.feeds_connected: bool  = False
        self.feeds_live:      list  = []               # which exchanges are streaming
        # Kept for backward compat with signal engine
        self.binance_price:   Optional[float] = None
        self.divergence_pct:  float = 0.0

        # Current signal
        self.signal:           str = "WAITING"   # YES / NO / HOLD / WAITING
        self.signal_reason:    str = "Starting up..."
        self.signal_confidence: float = 0.0
        self.momentum_pct:     float = 0.0

        # Active market
        self.current_market:         Optional[str]   = None
        self.seconds_until_close:    Optional[float] = None
        self.market_yes_price_cents: Optional[int]   = None
        self.market_no_price_cents:  Optional[int]   = None
        self.floor_strike:           Optional[float] = None  # target price for current market

        # Account — aggregate totals (kept for backward compat with existing dashboard hooks)
        self.balance_dollars:      float = 0.0
        self.balance_at_day_start: float = 0.0  # Kalshi balance at start of today — used for balance-based P&L
        self.daily_pnl:            float = 0.0
        self.total_pnl:            float = 0.0
        self.win_count:       int   = 0
        self.loss_count:      int   = 0

        # Paper vs Live P&L split — so you can compare performance with real money
        # against the simulated baseline. Both buckets are updated on every closed
        # trade via risk_manager.get_stats().
        self.paper_daily_pnl: float = 0.0
        self.paper_total_pnl: float = 0.0
        self.paper_wins:      int   = 0
        self.paper_losses:    int   = 0
        self.paper_trades:    int   = 0
        self.live_daily_pnl:  float = 0.0
        self.live_total_pnl:  float = 0.0
        self.live_wins:       int   = 0
        self.live_losses:     int   = 0
        self.live_trades:     int   = 0

        # Trades (most recent first, capped at 50)
        self.trades: list[TradeRecord] = []

        # Market assessment (from market_assessor.py, runs every 2 hours)
        self.last_assessment:             Optional[str]  = None
        self.last_assessment_time:        float          = 0.0
        self.last_assessment_suggestions: Optional[dict] = None  # pending APPLY payload

        # Bot status
        self.status:          str   = "Starting..."
        self.started_at:      float = time.time()
        self.paper_mode:      bool  = True
        self.trading_enabled: bool  = False  # Armed by the Arbitrage Bot button

    def add_trade(self, trade: TradeRecord):
        self.trades.insert(0, trade)
        if len(self.trades) > 50:
            self.trades = self.trades[:50]

    def update_trade(self, trade_id: str, pnl: float, outcome: str, closed_at: float):
        for t in self.trades:
            if t.trade_id == trade_id:
                t.pnl_dollars = pnl
                t.outcome     = outcome
                t.closed_at   = closed_at
                break
        self.total_pnl += pnl
        # daily_pnl is owned by the reconciler (balance-based) — don't touch it here
        if outcome in ("win", "take_profit"):
            self.win_count += 1
        elif outcome in ("loss", "stop_loss"):
            self.loss_count += 1

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return round((self.win_count / total * 100), 1) if total > 0 else 0.0

    def apply_stats(self, stats: dict):
        """
        Copy a risk_manager.get_stats() result into BotState.
        daily_pnl and live_daily_pnl are owned by the reconciler (balance-based)
        and must NOT be overwritten here. Only totals, wins/losses, and paper
        splits are updated from risk_manager data.
        """
        live = stats.get("live", {}) or {}
        # Do NOT set self.daily_pnl — reconciler owns it
        self.total_pnl  = live.get("pnl", 0.0)
        self.win_count  = stats.get("wins", 0)
        self.loss_count = stats.get("losses", 0)
        p = stats.get("paper", {}) or {}
        self.paper_daily_pnl = p.get("daily", 0.0)
        self.paper_total_pnl = p.get("pnl", 0.0)
        self.paper_wins      = p.get("wins", 0)
        self.paper_losses    = p.get("losses", 0)
        self.paper_trades    = p.get("trades", 0)
        l = stats.get("live", {}) or {}
        # Do NOT set self.live_daily_pnl — reconciler owns it
        self.live_total_pnl  = l.get("pnl", 0.0)
        self.live_wins       = l.get("wins", 0)
        self.live_losses     = l.get("losses", 0)
        self.live_trades     = l.get("trades", 0)

    def to_dict(self) -> dict:
        uptime_secs = int(time.time() - self.started_at)
        hours, rem  = divmod(uptime_secs, 3600)
        mins, secs  = divmod(rem, 60)

        return {
            "prices": {
                "kraken":      self.kraken_price,
                "coinbase":    self.coinbase_price,
                "bitstamp":    self.bitstamp_price,
                "gemini":      self.gemini_price,
                "cf_estimate": self.cf_estimate,
                "cf_momentum": round(self.cf_momentum_pct, 4),
                "connected":   self.feeds_connected,
                "feeds_live":  self.feeds_live,
                # kept for backward compat
                "binance":     self.kraken_price,
                "divergence":  round(self.cf_momentum_pct, 4),
            },
            "signal": {
                "value":      self.signal,
                "reason":     self.signal_reason,
                "confidence": self.signal_confidence,
                "momentum":   round(self.momentum_pct, 4),
            },
            "market": {
                "ticker":           self.current_market,
                "seconds_left":     self.seconds_until_close,
                "yes_price_cents":  self.market_yes_price_cents,
                "no_price_cents":   self.market_no_price_cents,
                "floor_strike":     self.floor_strike,
            },
            "account": {
                "balance":    round(self.balance_dollars, 2),
                "daily_pnl":  round(self.daily_pnl, 2),
                "total_pnl":  round(self.total_pnl, 2),
                "wins":       self.win_count,
                "losses":     self.loss_count,
                "win_rate":   self.win_rate,
                "paper": {
                    "daily_pnl": round(self.paper_daily_pnl, 2),
                    "total_pnl": round(self.paper_total_pnl, 2),
                    "wins":      self.paper_wins,
                    "losses":    self.paper_losses,
                    "trades":    self.paper_trades,
                    "win_rate":  round(
                        (self.paper_wins / (self.paper_wins + self.paper_losses) * 100)
                        if (self.paper_wins + self.paper_losses) > 0 else 0, 1),
                },
                "live": {
                    "daily_pnl": round(self.live_daily_pnl, 2),
                    "total_pnl": round(self.live_total_pnl, 2),
                    "wins":      self.live_wins,
                    "losses":    self.live_losses,
                    "trades":    self.live_trades,
                    "win_rate":  round(
                        (self.live_wins / (self.live_wins + self.live_losses) * 100)
                        if (self.live_wins + self.live_losses) > 0 else 0, 1),
                },
            },
            "trades": [t.to_dict() for t in self.trades],
            "meta": {
                "status":          self.status,
                "paper_mode":      self.paper_mode,
                "trading_enabled": self.trading_enabled,
                "uptime":          f"{hours:02d}:{mins:02d}:{secs:02d}",
                "version":         BOT_VERSION,
            },
            "settings": self.settings.to_dict(),
            "assessment": {
                "text": self.last_assessment,
                "time": self.last_assessment_time,
            },
        }


# Single global instance shared across all modules
state = BotState()
