"""
risk_manager.py — Risk Management & Position Tracking
=======================================================
Controls how much money the bot can risk and keeps a running
record of every trade. Acts as a safety layer between the signal
engine and actual order placement.

Rules enforced:
  - Max dollars per single trade
  - Max daily loss (hard stop)
  - Max consecutive losses (kill switch)
  - Cooldown period after a losing trade
  - Only 1 open position at a time
"""

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

# Always save next to this script file, regardless of working directory
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history.json")

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """Record of a single completed trade."""
    trade_id:        str
    ticker:          str
    side:            str           # "yes" or "no"
    price_cents:     int
    num_contracts:   int
    cost_dollars:    float
    opened_at:       float = field(default_factory=time.time)
    closed_at:       Optional[float] = None
    pnl_dollars:     Optional[float] = None
    outcome:         Optional[str]   = None   # "win", "loss", "push"
    paper:           bool = True
    settings:        Optional[dict]  = None


class RiskManager:
    """
    Tracks positions and enforces risk limits.
    Call check_can_trade() before placing any order.
    Call record_trade_opened() and record_trade_closed() to keep state accurate.
    """

    def __init__(self, config: dict):
        trading_cfg = config.get("trading", {})
        risk_cfg    = config.get("risk", {})

        self.max_bet_dollars       = trading_cfg.get("max_bet_dollars", 10)
        self.max_daily_loss        = trading_cfg.get("max_daily_loss", 50)
        self.min_daily_profit_lock = trading_cfg.get("min_daily_profit_lock", 0)
        self.paper_mode            = trading_cfg.get("paper_mode", True)

        self.max_open_positions    = risk_cfg.get("max_open_positions", 1)
        self.cooldown_secs         = risk_cfg.get("cooldown_after_loss_seconds", 120)
        self.max_consecutive_losses = risk_cfg.get("max_consecutive_losses", 3)

        # State
        self._trades:              list[Trade] = []
        self._open_positions:      list[Trade] = []
        self._daily_pnl:           float = 0.0
        self._last_trade_date:     date  = date.today()
        self._last_loss_time:      float = 0.0
        self._consecutive_losses:  int   = 0

    # ─────────────────────────────────────────────────────────
    # POSITION SIZING
    # ─────────────────────────────────────────────────────────

    def calculate_position_size(self, price_cents: int) -> int:
        """
        How many contracts should we buy?
        Each contract pays out $1 if we win. We pay `price_cents / 100` per contract.

        Example: YES price = 60¢ → we risk $0.60 per contract to win $1.00
        We aim to risk at most `max_bet_dollars` per trade.
        """
        if price_cents <= 0 or price_cents >= 100:
            return 0
        cost_per_contract = price_cents / 100
        num_contracts = int(self.max_bet_dollars / cost_per_contract)
        return max(1, num_contracts)  # Always at least 1 contract if we trade

    # ─────────────────────────────────────────────────────────
    # TRADE CHECKS
    # ─────────────────────────────────────────────────────────

    def check_can_trade(self) -> tuple[bool, str]:
        """
        Returns (True, "") if it's safe to place a trade,
        or (False, "reason") if trading should be blocked.
        Call this BEFORE placing any order.
        """

        # Reset daily P&L tracker at the start of each new day
        today = date.today()
        if today != self._last_trade_date:
            logger.info(f"New trading day. Resetting daily P&L (was ${self._daily_pnl:.2f})")
            self._daily_pnl        = 0.0
            self._last_trade_date  = today
            self._consecutive_losses = 0

        # Daily profit lock — stop trading once we've banked enough for the day
        if self.min_daily_profit_lock > 0 and self._daily_pnl >= self.min_daily_profit_lock:
            return False, (
                f"Profit lock: daily P&L ${self._daily_pnl:.2f} reached "
                f"target ${self.min_daily_profit_lock:.2f}. Done for today."
            )

        # Daily loss limit
        if self._daily_pnl <= -self.max_daily_loss:
            return False, (
                f"Daily loss limit hit: ${abs(self._daily_pnl):.2f} lost "
                f"(limit: ${self.max_daily_loss}). Bot stopped for today."
            )

        # Max consecutive losses kill switch
        if self._consecutive_losses >= self.max_consecutive_losses:
            return False, (
                f"Kill switch: {self._consecutive_losses} consecutive losses. "
                f"Stopping to prevent further drawdown."
            )

        # Too many open positions
        if len(self._open_positions) >= self.max_open_positions:
            return False, (
                f"Already have {len(self._open_positions)} open position(s). "
                f"Wait for it to resolve before trading again."
            )

        # Cooldown after a loss
        if self._last_loss_time > 0:
            secs_since_loss = time.time() - self._last_loss_time
            if secs_since_loss < self.cooldown_secs:
                remaining = int(self.cooldown_secs - secs_since_loss)
                return False, (
                    f"Cooling down after loss: {remaining}s remaining "
                    f"(cooldown: {self.cooldown_secs}s)"
                )

        return True, ""

    # ─────────────────────────────────────────────────────────
    # TRADE RECORDING
    # ─────────────────────────────────────────────────────────

    def record_trade_opened(
        self,
        ticker: str,
        side: str,
        price_cents: int,
        num_contracts: int,
        paper: bool = True,
        settings: dict = None,
    ) -> Trade:
        """Call this immediately after a successful order is placed."""
        cost = (price_cents * num_contracts) / 100
        trade = Trade(
            trade_id      = f"trade-{int(time.time())}",
            ticker        = ticker,
            side          = side,
            price_cents   = price_cents,
            num_contracts = num_contracts,
            cost_dollars  = cost,
            paper         = paper,
            settings      = settings,
        )
        self._trades.append(trade)
        self._open_positions.append(trade)

        mode = "[PAPER]" if paper else "[LIVE]"
        logger.info(
            f"{mode} Trade opened: {side.upper()} {num_contracts}x @ {price_cents}¢ "
            f"on {ticker} — risking ${cost:.2f}"
        )
        return trade

    def record_trade_take_profit(self, trade: Trade, exit_price_cents: int) -> Trade:
        """
        Call this when the bot exits a position early via take-profit.

        Counts as a WIN — resets consecutive_losses and does NOT trigger cooldown.
        The bot will sit out the rest of the current cycle (caller sets trade_count
        to max to prevent re-entry).

        P&L = (exit_price - entry_price) * num_contracts
        """
        trade.closed_at = time.time()

        gain_cents   = exit_price_cents - trade.price_cents
        gain_dollars = (gain_cents * trade.num_contracts) / 100

        trade.pnl_dollars = gain_dollars
        trade.outcome     = "take_profit"

        self._consecutive_losses = 0   # it's a win — reset the kill switch
        self._daily_pnl += trade.pnl_dollars

        if trade in self._open_positions:
            self._open_positions.remove(trade)

        mode = "[PAPER]" if trade.paper else "[LIVE]"
        logger.info(
            f"{mode} Take-profit exit: entry {trade.price_cents}c → exit {exit_price_cents}c "
            f"(+{gain_cents}c × {trade.num_contracts} = +${gain_dollars:.2f}) | "
            f"Daily P&L: ${self._daily_pnl:+.2f}"
        )
        self.save_trades()
        return trade

    def record_trade_early_exit(self, trade: Trade, exit_price_cents: int) -> Trade:
        """
        Call this when the bot exits a position early via stop-loss.

        Unlike record_trade_closed(), this does NOT trigger the cooldown timer
        or increment consecutive_losses — the bot should be free to re-enter
        immediately if a new signal fires in the same cycle.

        P&L = (exit_price - entry_price) * num_contracts  (negative = loss)
        """
        trade.closed_at = time.time()

        loss_cents   = trade.price_cents - exit_price_cents
        loss_dollars = (loss_cents * trade.num_contracts) / 100

        trade.pnl_dollars = -loss_dollars    # negative = we lost money
        trade.outcome     = "stop_loss"

        self._daily_pnl += trade.pnl_dollars

        if trade in self._open_positions:
            self._open_positions.remove(trade)

        mode = "[PAPER]" if trade.paper else "[LIVE]"
        logger.info(
            f"{mode} Stop-loss exit: entry {trade.price_cents}c → exit {exit_price_cents}c "
            f"(−{loss_cents}c × {trade.num_contracts} = −${loss_dollars:.2f}) | "
            f"Daily P&L: ${self._daily_pnl:+.2f}"
        )
        self.save_trades()
        return trade

    def record_trade_closed(self, trade: Trade, market_settled_yes: bool):
        """
        Call this when the market settles.

        Args:
            trade:               The Trade object returned by record_trade_opened()
            market_settled_yes:  True if YES won (price went UP), False if NO won
        """
        # Trade already closed via take-profit or stop-loss — don't double-record
        if trade.outcome is not None:
            logger.debug(f"Trade {trade.trade_id} already closed ({trade.outcome}), skipping settlement record")
            return trade

        trade.closed_at = time.time()

        # Did we win?
        we_bet_yes = trade.side == "yes"
        we_won     = (we_bet_yes and market_settled_yes) or \
                     (not we_bet_yes and not market_settled_yes)

        if we_won:
            # Payout = num_contracts * $1 per contract
            payout = trade.num_contracts * 1.0
            trade.pnl_dollars = payout - trade.cost_dollars
            trade.outcome     = "win"
            self._consecutive_losses = 0
        else:
            trade.pnl_dollars = -trade.cost_dollars
            trade.outcome     = "loss"
            self._consecutive_losses += 1
            self._last_loss_time = time.time()

        self._daily_pnl += trade.pnl_dollars

        if trade in self._open_positions:
            self._open_positions.remove(trade)

        mode   = "[PAPER]" if trade.paper else "[LIVE]"
        result = "WIN" if we_won else "LOSS"
        logger.info(
            f"{mode} Trade closed: {result} ${abs(trade.pnl_dollars):.2f} | "
            f"Daily P&L: ${self._daily_pnl:+.2f}"
        )

        self.save_trades()
        return trade

    # ─────────────────────────────────────────────────────────
    # STATS
    # ─────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Returns a summary of trading performance, broken out by paper vs live."""
        closed = [t for t in self._trades if t.outcome is not None]
        wins   = [t for t in closed if t.outcome in ("win", "take_profit")]
        losses = [t for t in closed if t.outcome in ("loss", "stop_loss")]

        total_pnl   = sum(t.pnl_dollars for t in closed if t.pnl_dollars is not None)
        win_rate    = (len(wins) / len(closed) * 100) if closed else 0
        total_cost  = sum(t.cost_dollars for t in closed)

        # Paper vs Live split — each trade's `paper` field decides the bucket
        today = date.today()
        def _bucket(is_paper: bool) -> dict:
            subset = [t for t in closed if bool(t.paper) == is_paper]
            s_wins   = [t for t in subset if t.outcome in ("win", "take_profit")]
            s_losses = [t for t in subset if t.outcome in ("loss", "stop_loss")]
            s_pnl    = sum(t.pnl_dollars for t in subset if t.pnl_dollars is not None)
            s_daily  = sum(
                t.pnl_dollars for t in subset
                if t.pnl_dollars is not None and t.closed_at is not None
                and date.fromtimestamp(t.closed_at) == today
            )
            s_win_rate = (len(s_wins) / len(subset) * 100) if subset else 0
            return {
                "trades":   len(subset),
                "wins":     len(s_wins),
                "losses":   len(s_losses),
                "pnl":      round(s_pnl, 2),
                "daily":    round(s_daily, 2),
                "win_rate": round(s_win_rate, 1),
            }

        paper_stats = _bucket(True)
        live_stats  = _bucket(False)

        return {
            "total_trades":       len(closed),
            "wins":               len(wins),
            "losses":             len(losses),
            "win_rate_pct":       round(win_rate, 1),
            "total_pnl":          round(total_pnl, 2),
            "daily_pnl":          round(self._daily_pnl, 2),
            "total_risked":       round(total_cost, 2),
            "open_positions":     len(self._open_positions),
            "consecutive_losses": self._consecutive_losses,
            # Split buckets
            "paper": paper_stats,
            "live":  live_stats,
        }

    # ─────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────

    def save_trades(self, filepath: str = HISTORY_FILE):
        """Write all closed trades to a JSON file so P&L survives restarts."""
        closed = [t for t in self._trades if t.outcome is not None]
        records = []
        for t in closed:
            records.append({
                "trade_id":      t.trade_id,
                "ticker":        t.ticker,
                "side":          t.side,
                "price_cents":   t.price_cents,
                "num_contracts": t.num_contracts,
                "cost_dollars":  t.cost_dollars,
                "opened_at":     t.opened_at,
                "closed_at":     t.closed_at,
                "pnl_dollars":   t.pnl_dollars,
                "outcome":       t.outcome,
                "paper":         t.paper,
                "settings":      t.settings,
            })
        # Write to a temp file first, then rename — prevents corrupting the
        # history file if the bot crashes mid-write (atomic on most OS/filesystems).
        try:
            dir_path = os.path.dirname(filepath)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=dir_path, suffix=".tmp", delete=False, encoding="utf-8"
            ) as tmp:
                json.dump(records, tmp, indent=2)
                tmp_path = tmp.name
            os.replace(tmp_path, filepath)
        except Exception as e:
            logger.warning(f"Could not save trade history: {e}")

    def load_trades(self, filepath: str = HISTORY_FILE) -> list:
        """
        Load saved trades from disk on startup.
        Returns a list of Trade objects for restoring dashboard state.
        Recalculates daily P&L (today's trades only) and total P&L.
        """
        if not os.path.exists(filepath):
            return []

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                raw = f.read().rstrip()
            # Repair common corruption: trailing comma before the closing bracket
            if raw.endswith(","):
                raw = raw[:-1]
            if not raw.endswith("]"):
                raw += "\n]"
            records = json.loads(raw)
        except Exception as e:
            logger.warning(f"Could not load trade history: {e}")
            return []

        today = date.today()
        loaded = []

        for r in records:
            t = Trade(
                trade_id      = r.get("trade_id", ""),
                ticker        = r.get("ticker", ""),
                side          = r.get("side", "yes"),
                price_cents   = r.get("price_cents", 0),
                num_contracts = r.get("num_contracts", 0),
                cost_dollars  = r.get("cost_dollars", 0.0),
                opened_at     = r.get("opened_at", 0.0),
                closed_at     = r.get("closed_at"),
                pnl_dollars   = r.get("pnl_dollars"),
                outcome       = r.get("outcome"),
                paper         = r.get("paper", True),
                settings      = r.get("settings"),
            )
            self._trades.append(t)
            loaded.append(t)

            # Recalculate daily P&L (today only)
            # Note: _consecutive_losses is intentionally NOT restored from history.
            # The kill switch always resets on restart — if it fires during a session,
            # the user must manually restart the bot to resume trading.
            if t.closed_at and t.pnl_dollars is not None:
                closed_date = date.fromtimestamp(t.closed_at)
                if closed_date == today:
                    self._daily_pnl += t.pnl_dollars

        total = len(loaded)
        wins  = sum(1 for t in loaded if t.outcome == "win")
        total_pnl = sum(t.pnl_dollars for t in loaded if t.pnl_dollars is not None)
        logger.info(
            f"Loaded {total} trades from history | "
            f"{wins}W/{total - wins}L | Total P&L: ${total_pnl:+.2f} | "
            f"Today P&L: ${self._daily_pnl:+.2f}"
        )
        return loaded

    def print_stats(self):
        """Prints a clean performance summary to the console."""
        s = self.get_stats()
        mode = "PAPER MODE" if self.paper_mode else "LIVE MODE"
        print(f"\n{'='*50}")
        print(f"  BOT PERFORMANCE SUMMARY  ({mode})")
        print(f"{'='*50}")
        print(f"  Trades:        {s['total_trades']}  ({s['wins']}W / {s['losses']}L)")
        print(f"  Win Rate:      {s['win_rate_pct']}%")
        print(f"  Daily P&L:     ${s['daily_pnl']:+.2f}")
        print(f"  Total P&L:     ${s['total_pnl']:+.2f}")
        print(f"  Total Risked:  ${s['total_risked']:.2f}")
        print(f"  Open Now:      {s['open_positions']}")
        print(f"{'='*50}\n")
