"""
main.py — Bot Orchestrator
============================
This is the entry point. Run this file to start the bot:

    python main.py

The dashboard will open automatically at http://localhost:5000
"""

import asyncio
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import os

import yaml
from dotenv import load_dotenv

from price_feeds import PriceStore, run_binance_feed, run_coinbase_feed, run_bitstamp_feed, run_gemini_feed
from signal_engine import SignalEngine, Signal
from kalshi_client import load_client_from_config, OrderNotFilledError
from risk_manager import RiskManager
from bot_state import state, TradeRecord
from bot_state_v2 import state_v2
from strategy_v2 import v2_trading_loop
from bot_state_mm import state_mm
from strategy_mm import mm_trading_loop
from dashboard_server import start_dashboard, _persist_settings_to_config
from telegram_kill import start_telegram_kill_switch, notify
from kalshi_reconciler import start_reconciler
from market_assessor import start_assessor


def setup_logging(log_file: str, log_level: str):
    level = getattr(logging, log_level.upper(), logging.INFO)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, mode="a", encoding="utf-8"),
    ]
    # force=True overrides any handlers a 3rd-party lib may have already set,
    # ensuring our FileHandler actually gets attached (basicConfig is a no-op
    # if the root logger already has handlers without this flag).
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # Suppress noisy third-party debug output — websockets internal
    # frame logging drowns out our own debug messages
    logging.getLogger("websockets").setLevel(logging.WARNING)


# Directory where main.py lives — used to resolve relative file paths
# regardless of where the bot is launched from
BOT_DIR = Path(__file__).resolve().parent


def load_config(path: str = "config.yaml") -> dict:
    # Load .env file first so credentials are available via os.environ
    load_dotenv(BOT_DIR / ".env")

    config_path = BOT_DIR / path
    if not config_path.exists():
        print(f"ERROR: Config file not found: {path}")
        sys.exit(1)
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Inject credentials from .env into the config dict so the rest of
    # the bot receives them exactly as it did when they lived in config.yaml
    kalshi = config.setdefault("kalshi", {})
    kalshi["api_key_id"]       = os.environ.get("KALSHI_API_KEY_ID",       kalshi.get("api_key_id", ""))
    kalshi["private_key_path"] = os.environ.get("KALSHI_PRIVATE_KEY_PATH", kalshi.get("private_key_path", ""))

    telegram = config.setdefault("telegram", {})
    telegram["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", telegram.get("bot_token", ""))
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", str(telegram.get("chat_id", 0)))
    try:
        telegram["chat_id"] = int(chat_id_raw)
    except (ValueError, TypeError):
        telegram["chat_id"] = 0

    return config


def _seed_settings_from_config(config: dict):
    """Seed state.settings from config.yaml so the dashboard shows correct values on startup."""
    sig = config.get("signal", {})
    trd = config.get("trading", {})
    s   = state.settings
    state.settings.update_from_dict({
        "trade_window_start_minutes":   sig.get("trade_window_start_minutes",   s.trade_window_start_minutes),
        "trade_window_end_minutes":     sig.get("trade_window_end_minutes",     s.trade_window_end_minutes),
        "momentum_threshold_pct":       sig.get("momentum_threshold_pct",       s.momentum_threshold_pct),
        "momentum_window_secs":         sig.get("momentum_window_seconds",      s.momentum_window_secs),
        "min_yes_price_cents":          sig.get("min_yes_price_cents",          s.min_yes_price_cents),
        "max_yes_price_cents":          sig.get("max_yes_price_cents",          s.max_yes_price_cents),
        "stop_loss_cents":              sig.get("stop_loss_cents",              s.stop_loss_cents),
        "take_profit_cents":            sig.get("take_profit_cents",            s.take_profit_cents),
        "sl_min_hold_secs":             sig.get("sl_min_hold_secs",             s.sl_min_hold_secs),
        "sl_disable_mins":              sig.get("sl_disable_mins",              s.sl_disable_mins),
        "signal_sl_disable_mins":       sig.get("signal_sl_disable_mins",      s.signal_sl_disable_mins),
        "price_sl_disable_mins":        sig.get("price_sl_disable_mins",       s.price_sl_disable_mins),
        "max_trades_per_cycle":         sig.get("max_trades_per_cycle",         s.max_trades_per_cycle),
        "signal_stop_enabled":          sig.get("signal_stop_enabled",          s.signal_stop_enabled),
        "signal_stop_persistence_secs": sig.get("signal_stop_persistence_secs", s.signal_stop_persistence_secs),
        "stop_loss_fallback_cents":     sig.get("stop_loss_fallback_cents",     s.stop_loss_fallback_cents),
        "early_entry_window_minutes":   sig.get("early_entry_window_minutes",   s.early_entry_window_minutes),
        "early_min_distance_pct":       sig.get("early_min_distance_pct",       s.early_min_distance_pct),
        "early_max_yes_cents":          sig.get("early_max_yes_cents",          s.early_max_yes_cents),
        "late_window_fallback_enabled": sig.get("late_window_fallback_enabled", s.late_window_fallback_enabled),
        "late_window_fallback_minutes": sig.get("late_window_fallback_minutes", s.late_window_fallback_minutes),
        "late_window_min_distance_pct": sig.get("late_window_min_distance_pct", s.late_window_min_distance_pct),
        "late_window_max_yes_cents":    sig.get("late_window_max_yes_cents",    s.late_window_max_yes_cents),
        "max_wrong_side_distance_pct":  sig.get("max_wrong_side_distance_pct",  s.max_wrong_side_distance_pct),
        "min_strike_distance_pct":      sig.get("min_strike_distance_pct",      s.min_strike_distance_pct),
        "near_strike_momentum_enabled": sig.get("near_strike_momentum_enabled", s.near_strike_momentum_enabled),
        "min_confidence_pct":           sig.get("min_confidence_pct",           s.min_confidence_pct),
        "sl_cooldown_secs":             sig.get("sl_cooldown_secs",             s.sl_cooldown_secs),
        "max_bet_dollars":              trd.get("max_bet_dollars",              s.max_bet_dollars),
        "max_daily_loss":               trd.get("max_daily_loss",               s.max_daily_loss),
        "min_daily_profit_lock":        trd.get("min_daily_profit_lock",        s.min_daily_profit_lock),
    })

    return config


def _friendly_skip_reason(raw: str) -> str:
    """
    Convert the technical "why we held" message from the signal engine
    into a short, plain-English label suitable for a phone notification.

    Matched by substring in priority order — first match wins. If nothing
    matches, the raw reason is returned unchanged so we never silently
    swallow a reason we forgot to map.
    """
    r = (raw or "").lower()

    # Wrong-side guard (must check before generic distance/momentum matches)
    if "wrong side" in r:
        return "Setup didn't qualify (wrong side of strike)"

    # Price-filter blocks
    if "too low" in r and "yes price" in r:
        return "YES price too low (longshot territory)"
    if "too high" in r and "yes price" in r:
        return "YES price too high (market already priced in)"

    # Early-window-specific reasons
    if "early window" in r and "not confirming" in r:
        return "Early-entry: momentum not confirming the direction"
    if "early window" in r and "from target" in r:
        return "Early-entry: BTC too close to strike"

    # Distance-based holds (regular window)
    if "too close, waiting for momentum" in r or ("within $" in r and "of target" in r):
        return "BTC too close to strike, waiting for momentum"
    if "moving toward it" in r:
        return "BTC trending back toward strike (reversal risk)"

    # Momentum threshold
    if "below" in r and "threshold" in r and "momentum" in r:
        return "Momentum too weak to trigger"

    # Cycle-timing
    if "too late" in r:
        return "Too late in cycle (skipped final seconds)"
    if "early window opens" in r or ("waiting --" in r and "min left" in r):
        return "Too early in cycle (waiting for trading window)"

    # Risk-manager blocks (these come prefixed with "Blocked: ")
    if r.startswith("blocked:"):
        # Strip prefix for cleaner display, keep the specific reason
        return raw[len("Blocked:"):].strip().capitalize()

    # Feed/data issues
    if "feeds" in r and "waiting" in r:
        return "Waiting for price feeds to connect"
    if "cf estimate unavailable" in r:
        return "CF price estimate unavailable"
    if "market price unavailable" in r:
        return "Kalshi market price unavailable"
    if "building" in r and "history" in r:
        return "Warming up price history"

    # Unknown reason — return as-is so we still see something
    return raw


def _format_no_trade_alert(no_trade_streak: int, skip_reasons_window: Counter) -> str:
    """
    Build the human-friendly Telegram message for the 3-cycle-no-trade alert.
    Aggregates similar reasons under their friendly labels (so e.g.
    "YES price 28c too low" and "YES price 33c too low" merge into one line).
    """
    friendly = Counter()
    for raw, n in skip_reasons_window.items():
        friendly[_friendly_skip_reason(raw)] += n

    if not friendly:
        return f"Heads up: bot hasn't traded in {no_trade_streak} cycles in a row."

    top = friendly.most_common(3)
    lines = [f"Heads up: no trades in {no_trade_streak} cycles in a row."]
    lines.append("")  # blank line for readability on phone
    lines.append("What the bot's been waiting on:")
    for label, n in top:
        # If a reason hit every cycle, say "every cycle" instead of "3 of 3 cycles"
        suffix = "every cycle" if n >= no_trade_streak else f"{n} of {no_trade_streak} cycles"
        lines.append(f"• {label} ({suffix})")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# STARTUP GHOST POSITION CHECK
# ─────────────────────────────────────────────────────────────

def _find_ghost_positions(kalshi_client):
    """
    Scan today's Kalshi fills and return any open KXBTC15M positions the bot
    has no record of.  Returns a list of fill dicts with an added '_net_qty'
    key (positive = net long).  Called at startup AND at every cycle
    transition so ghosts created during runtime are caught quickly.
    """
    now      = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    min_ts   = int(midnight.timestamp())
    prefix   = "KXBTC15M"

    fills       = [f for f in kalshi_client.get_fills(min_ts=min_ts)
                   if (f.get("ticker") or "").startswith(prefix)]
    settlements = [s for s in kalshi_client.get_settlements(min_ts=min_ts)
                   if (s.get("ticker") or "").startswith(prefix)]

    settled_tickers = {s.get("ticker") for s in settlements}
    sell_tickers    = {f.get("ticker") for f in fills
                       if (f.get("action") or "").lower() == "sell"}
    settled_or_sold = settled_tickers | sell_tickers

    return [f for f in fills
            if (f.get("action") or "").lower() == "buy"
            and f.get("ticker") not in settled_or_sold]


def _init_day_start_balance(balance: float) -> None:
    """
    Persist today's opening balance so daily P&L survives bot restarts.
    If a file already exists from today, load that balance (don't reset it).
    If the file is from a prior day, overwrite with current balance.
    """
    import json
    _log = logging.getLogger("main")
    path = Path(__file__).parent / ".day_start_balance.json"
    today = datetime.now().strftime("%Y-%m-%d")
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get("date") == today:
                state.balance_at_day_start = float(data["balance"])
                _log.info(
                    f"Day-start balance restored from file: ${state.balance_at_day_start:.2f} "
                    f"(current balance ${balance:.2f}, daily P&L so far "
                    f"${balance - state.balance_at_day_start:+.2f})"
                )
                return
        except Exception:
            pass
    # New day or bad file — save current balance as today's start
    state.balance_at_day_start = balance
    try:
        path.write_text(json.dumps({"date": today, "balance": balance}))
    except Exception as e:
        _log.warning(f"Could not save day-start balance file: {e}")
    _log.info(f"New day — balance_at_day_start set to ${balance:.2f}")


def _check_ghost_positions(kalshi_client, notify_fn, active_ticker=None,
                           risk_manager=None, label="startup"):
    """
    Find untracked open positions and alert via Telegram.

    If an open ghost is on the currently active market (`active_ticker`)
    AND risk_manager is provided, reconstruct a Trade object and return it
    so the caller can set current_trade and begin stop-loss monitoring.
    Returns the recovered Trade on success, None otherwise.

    `label` is used in log/alert text ('startup' vs 'cycle-check').
    """
    logger = logging.getLogger("ghost_check")
    try:
        open_buys = _find_ghost_positions(kalshi_client)

        # Positions the bot is still tracking (e.g. previous cycle's trade
        # awaiting settlement confirmation) are not ghosts — don't warn.
        if risk_manager is not None:
            tracked = {t.ticker for t in getattr(risk_manager, "_open_positions", [])}
            open_buys = [f for f in open_buys if f.get("ticker") not in tracked]

        if not open_buys:
            if label == "startup":
                logger.info("Startup check: no ghost positions found.")
            return None

        lines = [f"WARNING: Ghost position(s) found ({label}) — not tracked by bot:"]
        recovered_trade = None

        for f in open_buys:
            ticker    = f.get("ticker", "?")
            side      = (f.get("side") or "?").lower()
            qty_raw   = f.get("count_fp", 0)
            qty       = int(float(qty_raw)) if qty_raw else 0
            yes_price = f.get("yes_price_dollars")
            price_c   = round(float(yes_price) * 100) if yes_price else 0
            # For a NO buy, our cost is the no_price (= 1 - yes_price)
            entry_c   = price_c if side == "yes" else (100 - price_c)

            lines.append(f"  {ticker}  {side.upper()}  qty={qty}  entry≈{entry_c}c")

            # If this ghost is on the currently active market, recover it
            if (ticker == active_ticker and risk_manager is not None
                    and qty > 0 and entry_c > 0 and recovered_trade is None):
                created = f.get("created_time", "")
                try:
                    from datetime import timezone as _tz
                    opened_ts = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    opened_ts = time.time()

                recovered_trade = risk_manager.record_trade_opened(
                    ticker        = ticker,
                    side          = side,
                    price_cents   = entry_c,
                    num_contracts = qty,
                    paper         = False,
                )
                # Backfill the open timestamp from the fill data
                recovered_trade.opened_at = opened_ts
                lines.append(
                    f"  ↳ Recovered as active trade — stop-loss monitoring resumed."
                )

        if recovered_trade is None:
            lines.append("Check Kalshi — if the market is still open, close manually.")

        msg = "\n".join(lines)
        logger.warning(msg)
        notify_fn(msg)
        return recovered_trade

    except Exception as e:
        logger.warning(f"Ghost position check failed ({label}): {e}")
        return None


# ─────────────────────────────────────────────────────────────
# SETTLEMENT WATCHER
# ─────────────────────────────────────────────────────────────

async def watch_for_settlement(kalshi_client, risk_manager, trade, ticker, close_time_iso):
    logger = logging.getLogger("settlement_watcher")
    close_time = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))

    while True:
        now = datetime.now(timezone.utc)
        secs_since_close = (now - close_time).total_seconds()

        if secs_since_close < 120:
            await asyncio.sleep(15)
            continue

        try:
            market_data = kalshi_client._get(f"/markets/{ticker}")
            market      = market_data.get("market", {})
            status      = market.get("status", "")
            result      = market.get("result", "")

            logger.info(f"Settlement check {ticker}: status={status!r} result={result!r}")

            # Kalshi uses different status values depending on API version:
            # "settled" / "finalized" = market resolved with a result
            # "closed" with a non-empty result also means it's done
            is_settled = (
                status in ("settled", "finalized", "resolved") or
                (status == "closed" and result != "")
            )

            if is_settled and result != "":
                settled_yes = result.lower() == "yes"
                closed      = risk_manager.record_trade_closed(trade, settled_yes)

                state.update_trade(
                    trade_id  = trade.trade_id,
                    pnl       = closed.pnl_dollars,
                    outcome   = closed.outcome,
                    closed_at = closed.closed_at,
                )
                state.apply_stats(risk_manager.get_stats())

                risk_manager.print_stats()
                logger.info(f"Market {ticker} settled: {result.upper()} | P&L: ${closed.pnl_dollars:+.2f}")

                # Telegram alert (no-op if not configured). Only fires for trades
                # that rode all the way to settlement — TP/SL exits already alerted.
                outcome_word = "WIN" if closed.outcome == "win" else "LOSS"
                mode_tag = "PAPER" if getattr(trade, "paper", state.paper_mode) else "LIVE"
                notify(
                    f"SETTLED [{mode_tag}]: {outcome_word} ${closed.pnl_dollars:+.2f} "
                    f"({trade.side.upper()} {trade.num_contracts}x @ {trade.price_cents}c on {ticker})"
                )
                return

            # Give up after 30 minutes — market may have been voided or delayed.
            # CRITICAL: release the position from the risk manager, otherwise it
            # stays "open" forever and blocks every future trade this session.
            if secs_since_close > 1800:
                logger.warning(f"Settlement timeout for {ticker} after 30min. Final status={status!r}")
                risk_manager.release_position(trade, reason=f"settlement timeout on {ticker}")
                state.apply_stats(risk_manager.get_stats())
                notify(
                    f"SETTLEMENT TIMEOUT: {ticker} never settled after 30min. "
                    f"Position released as VOID — check Kalshi for the real outcome."
                )
                return

        except Exception as e:
            logger.warning(f"Could not fetch settlement status: {e}")

        await asyncio.sleep(30)


# ─────────────────────────────────────────────────────────────
# TRADING LOOP
# ─────────────────────────────────────────────────────────────

async def trading_loop(price_store, config, kalshi_client, signal_engine, risk_manager):
    logger     = logging.getLogger("trading_loop")
    # paper_mode is now read from state.paper_mode on every tick so the dashboard
    # PAPER/LIVE toggle takes effect without a restart. Startup value comes from
    # state (which main() seeded from config.yaml).
    startup_mode = "PAPER" if state.paper_mode else "LIVE"
    logger.info(f"Trading loop started ({startup_mode} MODE — live-switchable via dashboard)")

    current_trade        = None
    current_ticker       = None
    trade_count_this_cycle = 0   # how many trades placed in the current market
    settlement_task      = None  # asyncio task for the current settlement watcher
    background_watchers  = set() # strong refs to prior-cycle watchers still awaiting settlement
    _cycle_last_sl_time  = 0.0   # timestamp of last stop-loss exit in this cycle

    # Signal-based stop-loss tracker — first time CF crossed to wrong side of
    # strike against our open position; None while signal is alive. Resets on
    # new trade, new market, and when CF crosses back to our side.
    signal_cross_start_time = None

    # Periodic hold-reason logging — log at INFO when reason changes or every 60s
    _last_hold_reason    = None
    _last_hold_log_time  = 0.0

    # No-trade alert tracker. We notify once when 3 cycles in a row pass with
    # zero entries, so the user gets a heads-up if the bot is sitting silent.
    #   - skip_reasons_cycle  : distinct reasons seen during the current cycle
    #   - skip_reasons_window : distinct reasons accumulated across the no-trade streak
    #                           (each reason counted once per cycle it appeared in)
    #   - no_trade_streak     : consecutive cycles ending with trade_count_this_cycle == 0
    no_trade_streak      = 0
    skip_reasons_window  = Counter()
    skip_reasons_cycle   = set()

    while True:
        try:
            # ── Update dashboard prices on every loop ─────────────────────────
            state.kraken_price    = price_store.binance_price      # Kraken feeds binance slot
            state.coinbase_price  = price_store.coinbase_price
            state.bitstamp_price  = price_store.bitstamp_price
            state.gemini_price    = price_store.gemini_price
            state.cf_estimate     = price_store.get_cf_estimate()
            state.feeds_live      = price_store.live_exchanges()
            state.feeds_connected = price_store.is_ready()
            state.binance_price   = price_store.binance_price   # backward compat
            cf_past = price_store.get_cf_estimate_n_seconds_ago(30)
            cf_now  = state.cf_estimate
            if cf_past and cf_now and cf_past > 0:
                state.cf_momentum_pct = ((cf_now - cf_past) / cf_past) * 100
                state.divergence_pct  = state.cf_momentum_pct

            if not price_store.is_ready():
                state.status = "Waiting for price feeds..."
                await asyncio.sleep(2)
                continue

            # ── Bot arm check ─────────────────────────────────────────────────
            if not state.trading_enabled:
                state.status = "Press ARBITRAGE BOT to start trading"
                await asyncio.sleep(2)
                continue

            # ── Find active market ────────────────────────────────────────────
            market = kalshi_client.get_active_15min_market()

            if market is None:
                state.status              = "No active BTC market — waiting..."
                state.current_market      = None
                state.seconds_until_close = None
                await asyncio.sleep(30)
                continue

            ticker              = market.get("ticker", "")
            close_time_iso      = market.get("close_time", "")
            seconds_until_close = market.get("_seconds_until_close", 0)
            floor_strike        = market.get("_floor_strike")   # target price YES settles against

            # Reset per-cycle counter when a new market opens
            if ticker != current_ticker:
                # Wrap up the cycle that just ended (if any). If it ended with
                # zero trades, extend the no-trade streak; otherwise reset it.
                # When the streak hits 3, send a Telegram alert summarising the
                # most common skip reasons so the user knows why the bot is idle.
                if current_ticker is not None:
                    if trade_count_this_cycle == 0:
                        no_trade_streak += 1
                        for r in skip_reasons_cycle:
                            skip_reasons_window[r] += 1
                        if no_trade_streak >= 3:
                            alert_msg = _format_no_trade_alert(
                                no_trade_streak, skip_reasons_window
                            )
                            notify(alert_msg)
                            logger.info(
                                f"No-trade alert sent ({no_trade_streak} cycles):\n{alert_msg}"
                            )
                            no_trade_streak = 0
                            skip_reasons_window.clear()
                    else:
                        # A trade fired this cycle — break the streak
                        no_trade_streak = 0
                        skip_reasons_window.clear()
                # New cycle starts fresh
                skip_reasons_cycle.clear()

                # Keep the previous cycle's settlement watcher RUNNING. Kalshi
                # only confirms settlement ~2min after close — well into the
                # next cycle — so cancelling here orphaned the trade in
                # _open_positions forever and froze all trading ("Already have
                # 1 open position"). The watcher holds refs to its own trade
                # and ticker so it can't touch the new market, and it
                # self-terminates on settlement or its 30-min timeout. Stash a
                # strong reference so asyncio doesn't garbage-collect the task.
                if settlement_task and not settlement_task.done():
                    background_watchers.add(settlement_task)
                    settlement_task.add_done_callback(background_watchers.discard)

                logger.info(f"New market: {ticker} | Closes in: {seconds_until_close/60:.1f} min")
                current_ticker          = ticker
                current_trade           = None
                trade_count_this_cycle  = 0
                settlement_task         = None
                signal_cross_start_time = None  # new market, fresh tracker
                _cycle_last_sl_time     = 0.0   # reset per-cycle SL cooldown
                _last_hold_reason       = None   # force first HOLD in new market to log
                _last_hold_log_time     = 0.0

                # Per-cycle ghost check — catches positions created during runtime
                # (crash mid-order, connection reset, etc.) not just at startup.
                # Awaited inline so recovery is race-condition-free before the
                # loop resumes signal evaluation for this cycle.
                if not state.paper_mode:
                    try:
                        recovered = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: _check_ghost_positions(
                                kalshi_client, notify,
                                active_ticker = ticker,
                                risk_manager  = risk_manager,
                                label         = "cycle-check",
                            )
                        )
                        if recovered is not None:
                            current_trade          = recovered
                            trade_count_this_cycle = 1
                            logger.warning(
                                f"Ghost recovered on {ticker}: "
                                f"{recovered.side.upper()} {recovered.num_contracts}x "
                                f"@ {recovered.price_cents}c — stop-loss monitoring resumed"
                            )
                    except Exception as _ge:
                        logger.warning(f"Runtime ghost check error: {_ge}")

            # ── Get market prices ─────────────────────────────────────────────
            try:
                prices  = kalshi_client.get_market_prices(ticker)
                yes_ask = prices.get("yes_ask", 0)
                no_ask  = prices.get("no_ask", 0)
                yes_bid = prices.get("yes_bid", 0)
                no_bid  = prices.get("no_bid", 0)
            except Exception as e:
                logger.warning(f"Could not fetch market prices: {e}")
                await asyncio.sleep(5)
                continue

            # ── Update dashboard market info ──────────────────────────────────
            state.current_market         = ticker
            state.seconds_until_close    = seconds_until_close
            state.market_yes_price_cents = yes_ask
            state.market_no_price_cents  = no_ask
            state.floor_strike           = floor_strike

            # ── Stop-loss / take-profit monitoring for open position ──────────
            if current_trade is not None:
                stop_loss_cents    = int(getattr(state.settings, "stop_loss_cents", 20))
                take_profit_cents  = int(getattr(state.settings, "take_profit_cents", 97))
                sl_min_hold_secs   = int(getattr(state.settings, "sl_min_hold_secs", 90))
                sl_disable_mins    = float(getattr(state.settings, "sl_disable_mins", 3.0))
                # Split SL time gates: soft (signal/legacy) and hard (price-collapse)
                # If new fields aren't present yet (older saved state), fall back to legacy sl_disable_mins
                # for the soft gate, and 0.5 min for the hard gate so catastrophic moves still trigger.
                signal_sl_disable_mins   = float(getattr(state.settings, "signal_sl_disable_mins", sl_disable_mins))
                price_sl_disable_mins    = float(getattr(state.settings, "price_sl_disable_mins", 0.5))
                signal_stop_enabled      = bool(getattr(state.settings, "signal_stop_enabled", True))
                signal_persistence_secs  = int(getattr(state.settings, "signal_stop_persistence_secs", 15))
                stop_loss_fallback_cents = int(getattr(state.settings, "stop_loss_fallback_cents", 30))
                entry_price        = current_trade.price_cents
                secs_held          = time.time() - current_trade.opened_at
                sl_held_enough     = secs_held >= sl_min_hold_secs
                # Soft gate: signal-based + legacy price stop. Off in final N min (panic-exit prevention).
                signal_sl_time_ok  = seconds_until_close > (signal_sl_disable_mins * 60)
                # Hard gate: price-collapse failsafe. Stays armed until just before close.
                price_sl_time_ok   = seconds_until_close > (price_sl_disable_mins * 60)
                # Legacy single-gate variable kept for any downstream display text that still references it.
                sl_time_ok         = signal_sl_time_ok

                # For a YES position we can sell at yes_bid; for NO at no_bid
                sell_price  = yes_bid if current_trade.side == "yes" else no_bid
                loss_cents  = entry_price - sell_price if sell_price > 0 else 0

                # ── Signal-based exit evaluation ──────────────────────────────
                # The real signal is the CF estimate vs. the strike. For a YES
                # position we want CF > strike; for NO we want CF < strike.
                # If CF crosses the wrong way for long enough, the thesis is
                # broken and we should exit regardless of Kalshi order book noise.
                cf_now = state.cf_estimate
                signal_against = (
                    floor_strike is not None
                    and cf_now is not None
                    and (
                        (current_trade.side == "yes" and cf_now <= floor_strike)
                        or (current_trade.side == "no"  and cf_now >= floor_strike)
                    )
                )
                now_ts = time.time()
                if signal_against:
                    if signal_cross_start_time is None:
                        signal_cross_start_time = now_ts
                    signal_persisted_secs = now_ts - signal_cross_start_time
                else:
                    signal_cross_start_time = None
                    signal_persisted_secs   = 0.0

                signal_broken  = (
                    signal_stop_enabled
                    and signal_against
                    and signal_persisted_secs >= signal_persistence_secs
                )
                price_collapse = (
                    sell_price > 0 and loss_cents >= stop_loss_fallback_cents
                )
                # Legacy price-only path when signal stop is disabled
                legacy_price_stop = (
                    not signal_stop_enabled
                    and sell_price > 0
                    and loss_cents >= stop_loss_cents
                )

                # ── Take-profit: lock in gains when position hits target ───────
                if sell_price > 0 and sell_price >= take_profit_cents:
                    gain_cents = sell_price - entry_price
                    logger.info(
                        f"TAKE-PROFIT: {current_trade.side.upper()} entered @ {entry_price}c, "
                        f"now {sell_price}c (+{gain_cents}c ≥ {take_profit_cents}c target) — banking gain"
                    )
                    try:
                        kalshi_client.sell_position(
                            ticker        = ticker,
                            side          = current_trade.side,
                            price_cents   = sell_price,
                            num_contracts = current_trade.num_contracts,
                            paper_mode    = state.paper_mode,
                        )
                        exited = risk_manager.record_trade_take_profit(current_trade, sell_price)
                        state.update_trade(
                            trade_id  = current_trade.trade_id,
                            pnl       = exited.pnl_dollars,
                            outcome   = "take_profit",
                            closed_at = exited.closed_at,
                        )
                        state.apply_stats(risk_manager.get_stats())
                        # Telegram alert (no-op if not configured)
                        mode_tag = "PAPER" if state.paper_mode else "LIVE"
                        notify(
                            f"TAKE-PROFIT [{mode_tag}]: sold {current_trade.side.upper()} "
                            f"@ {sell_price}c (entered {entry_price}c, +{gain_cents}c) "
                            f"P&L ${exited.pnl_dollars:+.2f} on {ticker}"
                        )
                        # Cancel settlement watcher — we already closed this trade
                        if settlement_task and not settlement_task.done():
                            settlement_task.cancel()
                        settlement_task = None
                        current_trade = None
                        signal_cross_start_time = None  # fresh tracker for next trade
                        # Sit out the rest of this cycle — wait for next market
                        max_trades = int(getattr(state.settings, "max_trades_per_cycle", 3))
                        trade_count_this_cycle = max_trades
                        state.status = (
                            f"Take-profit @ {sell_price}c (+{gain_cents}c) — "
                            f"waiting for next market"
                        )
                        logger.info(f"Take-profit recorded. Sitting out rest of cycle.")
                    except Exception as e:
                        logger.error(f"Take-profit exit failed: {e}")
                    await asyncio.sleep(5)
                    continue

                # ── Stop-loss: cut losses when position moves against us ───────
                # Gates split into soft (signal/legacy) and hard (price-collapse).
                # Soft is silenced earlier than hard so noisy late-cycle signal flips
                # don't trigger panic exits, but a real catastrophic move always does.
                #   1. Minimum hold time — don't exit within first N seconds (price noise)
                #   2. Time remaining — separate gate per trigger type:
                #        - signal_broken / legacy_price_stop → signal_sl_time_ok (off in final ~3 min)
                #        - price_collapse                    → price_sl_time_ok  (off only in final ~30s)
                #   3. Trigger — one of:
                #        a. signal_broken  — CF crossed the wrong side of strike for persistence window
                #        b. price_collapse — loss hit the wider fallback limit (true bad position)
                #        c. legacy_price_stop — only when signal_stop_enabled is off
                hard_collapse_fires = price_collapse and price_sl_time_ok
                soft_signal_fires   = signal_broken    and signal_sl_time_ok
                soft_legacy_fires   = legacy_price_stop and signal_sl_time_ok
                exit_fires          = sl_held_enough and (
                    hard_collapse_fires or soft_signal_fires or soft_legacy_fires
                )
                if exit_fires:
                    if hard_collapse_fires:
                        reason = (
                            f"price collapse (down {loss_cents}c ≥ "
                            f"{stop_loss_fallback_cents}c failsafe)"
                        )
                    elif soft_signal_fires:
                        reason = (
                            f"signal broken (CF {cf_now:.2f} vs strike {floor_strike:.2f} "
                            f"for {signal_persisted_secs:.0f}s)"
                        )
                    else:
                        reason = (
                            f"legacy price stop (down {loss_cents}c ≥ {stop_loss_cents}c)"
                        )
                    logger.info(
                        f"STOP-LOSS: {current_trade.side.upper()} entered @ {entry_price}c, "
                        f"now {sell_price}c — {reason} — exiting"
                    )
                    try:
                        # On a hard price-collapse, drop 10c below the bid to
                        # guarantee a fill — but only when price is high enough
                        # that the discount makes sense. Below 15c the market
                        # is nearly worthless already; just sell at bid.
                        exit_price = (
                            max(1, sell_price - 10)
                            if hard_collapse_fires and sell_price > 15
                            else sell_price
                        )
                        kalshi_client.sell_position(
                            ticker        = ticker,
                            side          = current_trade.side,
                            price_cents   = exit_price,
                            num_contracts = current_trade.num_contracts,
                            paper_mode    = state.paper_mode,
                        )
                        exited = risk_manager.record_trade_early_exit(current_trade, exit_price)
                        state.update_trade(
                            trade_id  = current_trade.trade_id,
                            pnl       = exited.pnl_dollars,
                            outcome   = "stop_loss",
                            closed_at = exited.closed_at,
                        )
                        state.apply_stats(risk_manager.get_stats())
                        # Telegram alert (no-op if not configured)
                        mode_tag = "PAPER" if state.paper_mode else "LIVE"
                        notify(
                            f"STOP-LOSS [{mode_tag}]: sold {current_trade.side.upper()} "
                            f"@ {sell_price}c (entered {entry_price}c, −{loss_cents}c) "
                            f"P&L ${exited.pnl_dollars:+.2f} on {ticker} | {reason}"
                        )
                        current_trade = None
                        signal_cross_start_time = None  # fresh tracker for next trade
                        _cycle_last_sl_time     = time.time()   # start per-cycle cooldown
                        sl_cd = int(getattr(state.settings, "sl_cooldown_secs", 60))
                        state.status = (
                            f"Stop-loss exit at {sell_price}c (−{loss_cents}c) — "
                            f"cooling down {sl_cd}s before re-entry [{trade_count_this_cycle}/"
                            f"{state.settings.max_trades_per_cycle}]"
                        )
                        logger.info(f"Stop-loss exit recorded. Trades this cycle: {trade_count_this_cycle}")
                        await asyncio.sleep(5)
                        continue
                    except Exception as e:
                        logger.error(f"Stop-loss exit failed: {e}")
                        state.status = (
                            f"Position open: {current_trade.side.upper()} @ {entry_price}c "
                            f"| now {sell_price}c (−{loss_cents}c)"
                        )
                        await asyncio.sleep(5)
                        continue
                else:
                    # Position is fine — show current P&L and keep waiting
                    gain_cents = sell_price - entry_price if sell_price > 0 else 0
                    if not sl_held_enough:
                        sl_note = f"| SL active in {int(sl_min_hold_secs - secs_held)}s"
                    elif not price_sl_time_ok:
                        # Both gates off — final stretch, hold to settlement
                        sl_note = f"| holding to settlement ({seconds_until_close/60:.1f}m left)"
                    elif not signal_sl_time_ok:
                        # Soft gate off but hard failsafe still armed
                        sl_note = (
                            f"| soft SL off, hard failsafe armed −{stop_loss_fallback_cents}c "
                            f"({seconds_until_close/60:.1f}m left)"
                        )
                    elif signal_stop_enabled:
                        if signal_against:
                            remaining = max(0, signal_persistence_secs - int(signal_persisted_secs))
                            sl_note = (
                                f"| signal crossed ({signal_persisted_secs:.0f}/"
                                f"{signal_persistence_secs}s, exit in {remaining}s) "
                                f"| failsafe −{stop_loss_fallback_cents}c"
                            )
                        else:
                            sl_note = (
                                f"| signal alive | failsafe −{stop_loss_fallback_cents}c"
                            )
                    else:
                        sl_note = f"| stop-loss at −{stop_loss_cents}c"
                    state.status = (
                        f"Position open: {current_trade.side.upper()} @ {entry_price}c "
                        f"| now {sell_price}c ({gain_cents:+d}c) {sl_note}"
                    )

                    # Keep signal display live while in a trade — evaluate but don't act.
                    try:
                        live_signal = signal_engine.evaluate(
                            price_store=price_store,
                            market_yes_price_cents=yes_ask,
                            market_no_price_cents=no_ask,
                            seconds_until_close=seconds_until_close,
                            floor_strike=floor_strike,
                            config=config,
                        )
                        state.signal            = live_signal.signal.value
                        state.signal_reason     = live_signal.reason
                        state.signal_confidence = live_signal.confidence
                        state.momentum_pct      = live_signal.momentum_pct
                    except Exception:
                        pass

                    await asyncio.sleep(2)
                    continue

            # ── Max trades per cycle check ────────────────────────────────────
            max_trades = int(getattr(state.settings, "max_trades_per_cycle", 3))
            if trade_count_this_cycle >= max_trades:
                state.status = (
                    f"Max {max_trades} trades reached this cycle — "
                    f"waiting for next market"
                )
                await asyncio.sleep(10)
                continue

            # ── Per-cycle stop-loss cooldown ──────────────────────────────────
            sl_cd = int(getattr(state.settings, "sl_cooldown_secs", 60))
            if sl_cd > 0 and _cycle_last_sl_time > 0:
                secs_since_sl = time.time() - _cycle_last_sl_time
                if secs_since_sl < sl_cd:
                    remaining_cd = int(sl_cd - secs_since_sl)
                    state.status = (
                        f"Post stop-loss cooldown — re-entry in {remaining_cd}s "
                        f"[{trade_count_this_cycle}/{max_trades}]"
                    )
                    await asyncio.sleep(5)
                    continue

            # ── Evaluate signal ───────────────────────────────────────────────
            signal_result = signal_engine.evaluate(
                price_store=price_store,
                market_yes_price_cents=yes_ask,
                market_no_price_cents=no_ask,
                seconds_until_close=seconds_until_close,
                floor_strike=floor_strike,
                config=config,
            )

            # Update dashboard signal
            state.signal            = signal_result.signal.value
            state.signal_reason     = signal_result.reason
            state.signal_confidence = signal_result.confidence
            state.momentum_pct      = signal_result.momentum_pct

            logger.debug(
                f"Signal: {signal_result.signal.value} | "
                f"Mom: {signal_result.momentum_pct:+.3f}%"
            )

            if signal_result.signal == Signal.HOLD:
                # ── Option B: late-window fallback ────────────────────────────
                # If no trade has fired this cycle and we're in the final minutes
                # with a clear CF-vs-strike edge, take the favored side.
                fallback = signal_engine.evaluate_late_window_fallback(
                    price_store            = price_store,
                    market_yes_price_cents = yes_ask,
                    market_no_price_cents  = no_ask,
                    seconds_until_close    = seconds_until_close,
                    floor_strike           = floor_strike,
                    trade_count_this_cycle = trade_count_this_cycle,
                )
                if fallback is not None:
                    # Adopt the fallback as the real signal and fall through to
                    # the normal trade-placement path below.
                    signal_result           = fallback
                    state.signal            = fallback.signal.value
                    state.signal_reason     = fallback.reason
                    state.signal_confidence = fallback.confidence
                    state.momentum_pct      = fallback.momentum_pct
                    logger.info(fallback.reason)
                else:
                    cycle_tag = f" [{trade_count_this_cycle}/{max_trades}]" if trade_count_this_cycle > 0 else ""
                    state.status = f"Monitoring — no signal yet{cycle_tag}"

                    # Log hold reason at INFO when it changes or every 60 seconds
                    now_ts = time.time()
                    reason = signal_result.reason
                    if reason != _last_hold_reason or (now_ts - _last_hold_log_time) >= 60:
                        mins_left = seconds_until_close / 60 if seconds_until_close else 0
                        logger.info(
                            f"HOLD | YES={yes_ask}c | {mins_left:.1f}min left | {reason}"
                        )
                        _last_hold_reason   = reason
                        _last_hold_log_time = now_ts

                    # Track distinct skip reasons for the no-trade alert
                    if reason:
                        skip_reasons_cycle.add(reason)

                    await asyncio.sleep(5)
                    continue

            # ── Confidence threshold gate ─────────────────────────────────────
            min_conf = float(getattr(state.settings, "min_confidence_pct", 0.50))
            if signal_result.confidence < min_conf:
                reason = (
                    f"Confidence {signal_result.confidence:.0%} below threshold {min_conf:.0%} "
                    f"— waiting for stronger momentum"
                )
                skip_reasons_cycle.add(reason)
                now_ts = time.time()
                if reason != _last_hold_reason or (now_ts - _last_hold_log_time) >= 60:
                    logger.info(f"CONF-GATE | {signal_result.signal.value} @ {signal_result.confidence:.0%} | {reason}")
                    _last_hold_reason  = reason
                    _last_hold_log_time = now_ts
                state.status = f"Signal {signal_result.signal.value} but {reason}"
                await asyncio.sleep(5)
                continue

            # ── Side price eligibility ────────────────────────────────────────
            # Check the maker bid price (bid+1) not the ask — we'll enter as a
            # maker so we pay the bid side, not the ask side.
            chosen_price = (
                min(yes_bid + 1, yes_ask - 1) if signal_result.signal == Signal.YES
                else min(no_bid + 1, no_ask - 1)
            )
            chosen_price = max(1, chosen_price)
            if (chosen_price < state.settings.min_yes_price_cents or
                    chosen_price > state.settings.max_yes_price_cents):
                reason = (
                    f"{signal_result.signal.value} price {chosen_price}c out of range "
                    f"[{state.settings.min_yes_price_cents}–{state.settings.max_yes_price_cents}c]"
                )
                skip_reasons_cycle.add(reason)
                state.status = f"Signal {signal_result.signal.value} blocked — {reason}"
                await asyncio.sleep(5)
                continue

            # ── Risk check ────────────────────────────────────────────────────
            can_trade, block_reason = risk_manager.check_can_trade()
            if not can_trade:
                state.status = f"Blocked: {block_reason}"
                logger.warning(f"Trade blocked: {block_reason}")
                # Track distinct block reasons for the no-trade alert
                skip_reasons_cycle.add(f"Blocked: {block_reason}")
                await asyncio.sleep(10)
                continue

            # ── Pick side and price (maker: bid+1, not ask) ───────────────────
            side        = "yes" if signal_result.signal == Signal.YES else "no"
            price_cents = (
                min(yes_bid + 1, yes_ask - 1) if side == "yes"
                else min(no_bid + 1, no_ask - 1)
            )
            price_cents = max(1, price_cents)
            # Hard clamp: never place an order below the configured minimum,
            # even if prices ticked between the eligibility check and here.
            price_cents = max(price_cents, state.settings.min_yes_price_cents)

            if price_cents <= 0:
                await asyncio.sleep(5)
                continue

            num_contracts = risk_manager.calculate_position_size(price_cents)

            logger.info(
                f"SIGNAL {signal_result.signal.value} | {ticker} | "
                f"{side.upper()} {num_contracts}x @ {price_cents}c | "
                f"trade {trade_count_this_cycle}/{max_trades} | "
                f"{signal_result.reason}"
            )

            # ── Place order ───────────────────────────────────────────────────
            try:
                kalshi_client.place_order(
                    ticker=ticker, side=side,
                    price_cents=price_cents, num_contracts=num_contracts,
                    paper_mode=state.paper_mode,
                    maker=True,
                )
                # Consume the trade slot immediately after place_order returns.
                # If anything below throws, the slot is already used so the bot
                # won't retry on the same cycle and place a second order on Kalshi.
                trade_count_this_cycle += 1
                signal_cross_start_time = None  # fresh tracker for this new trade

                rm_trade = risk_manager.record_trade_opened(
                    ticker=ticker, side=side,
                    price_cents=price_cents, num_contracts=num_contracts,
                    paper=state.paper_mode,
                    settings=state.settings.to_dict(),
                )
                current_trade = rm_trade

                # Add to dashboard
                dashboard_trade = TradeRecord(
                    trade_id      = rm_trade.trade_id,
                    ticker        = ticker,
                    side          = side.upper(),
                    price_cents   = price_cents,
                    num_contracts = num_contracts,
                    cost_dollars  = rm_trade.cost_dollars,
                    opened_at     = rm_trade.opened_at,
                )
                state.add_trade(dashboard_trade)
                state.status = (
                    f"Position open: {side.upper()} @ {price_cents}c | "
                    f"stop-loss at {price_cents - state.settings.stop_loss_cents}c"
                )
                # Telegram alert (no-op if not configured)
                mode_tag = "PAPER" if state.paper_mode else "LIVE"
                notify(
                    f"ENTRY [{mode_tag}]: bought {side.upper()} {num_contracts}x @ "
                    f"{price_cents}c (cost ${rm_trade.cost_dollars:.2f}) on {ticker} "
                    f"| trade {trade_count_this_cycle}/{max_trades}"
                )

                settlement_task = asyncio.create_task(
                    watch_for_settlement(
                        kalshi_client  = kalshi_client,
                        risk_manager   = risk_manager,
                        trade          = rm_trade,
                        ticker         = ticker,
                        close_time_iso = close_time_iso,
                    )
                )

            except OrderNotFilledError as e:
                # place_order raised before our counter increment — consume the
                # slot and start cooldown so the bot doesn't retry this cycle.
                if trade_count_this_cycle == 0:
                    trade_count_this_cycle += 1
                _cycle_last_sl_time = time.time()
                logger.error(f"Order did not fill: {e}")
                state.status = f"Order not filled — waiting before retry"
            except Exception as e:
                # If place_order returned before this exception (counter already
                # incremented), don't double-count. If place_order itself threw,
                # counter is still 0 — consume the slot conservatively since we
                # can't confirm whether Kalshi saw the order.
                if trade_count_this_cycle == 0:
                    trade_count_this_cycle += 1
                    _cycle_last_sl_time = time.time()
                logger.error(f"Order placement failed: {e}")
                state.status = f"Order error: {e}"

        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)

        await asyncio.sleep(5)


# ─────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────

async def main():
    config     = load_config("config.yaml")
    log_cfg    = config.get("logging", {})
    paper_mode = config.get("trading", {}).get("paper_mode", True)

    log_file = str(BOT_DIR / log_cfg.get("log_file", "bot_trades.log"))
    setup_logging(
        log_file  = log_file,
        log_level = log_cfg.get("log_level", "INFO"),
    )

    logger = logging.getLogger("main")

    # Optional remote kill switch via Telegram. No-op when not configured.
    def _apply_assessment_settings() -> str:
        suggestions = getattr(state, "last_assessment_suggestions", None)
        if not suggestions:
            return "⚠️ No assessment available yet — wait for the next update."
        payload = {k: v for k, v in suggestions.items() if k != "note"}
        before = {k: getattr(state.settings, k, "?") for k in payload}
        state.settings.update_from_dict(payload)
        _persist_settings_to_config(payload)
        lines = ["✅ Assessment settings applied:"]
        labels = {
            "momentum_threshold_pct": "Momentum threshold",
            "momentum_window_secs":   "Momentum window",
            "early_min_distance_pct": "Early entry distance",
            "max_trades_per_cycle":   "Max trades/cycle",
            "sl_min_hold_secs":       "SL min hold",
        }
        for k, v in payload.items():
            label = labels.get(k, k)
            lines.append(f"  {label}: {before[k]} → {v}")
        return "\n".join(lines)
    start_telegram_kill_switch(config, apply_fn=_apply_assessment_settings)

    # Seed live settings from config so the dashboard shows correct values on startup
    _seed_settings_from_config(config)
    state.paper_mode = paper_mode
    state.status     = "Starting up..."

    print("\n" + "="*55)
    print("  KALSHI BTC BOT  |  Binance->Coinbase Divergence")
    print("="*55)
    use_demo = config.get("kalshi", {}).get("use_demo", False)
    account_kind = "DEMO account" if use_demo else "LIVE account (real money)"
    if paper_mode:
        mode_str = f"PAPER MODE -- simulating only ({account_kind})"
    else:
        mode_str = f"LIVE MODE -- real orders will be placed on {account_kind}"
    print(f"  {mode_str}")
    print("  Toggle PAPER/LIVE anytime from the dashboard.")
    print("="*55 + "\n")

    price_store   = PriceStore()
    signal_engine = SignalEngine(config)
    risk_manager  = RiskManager(config)

    try:
        kalshi_client = load_client_from_config(config)
    except (ValueError, FileNotFoundError) as e:
        print(f"\nERROR: {e}\n")
        sys.exit(1)

    try:
        balance = kalshi_client.get_balance()
        state.balance_dollars = balance
        logger.info(f"Kalshi account balance: ${balance:.2f}")
        _init_day_start_balance(balance)
    except Exception as e:
        logger.error(f"Could not connect to Kalshi: {e}")
        sys.exit(1)

    # Scan for ghost positions from any previous session before the loop starts.
    # Runs synchronously so the alert fires before the bot begins trading.
    _check_ghost_positions(kalshi_client, notify, label="startup")

    # Background reconciler — periodically syncs the dashboard's daily P&L
    # to Kalshi's authoritative numbers. Runs in a daemon thread so it never
    # blocks the trading loop. No-op if disabled in config.
    start_reconciler(kalshi_client, config)

    # BTC market assessor — runs at 9 AM and 3 PM, sends report via Telegram
    # and stores it in state.last_assessment for the dashboard to display.
    start_assessor(notify_fn=notify, state=state)

    # Restore trade history and P&L from previous sessions
    saved_trades = risk_manager.load_trades()
    for t in saved_trades:   # oldest first → add_trade inserts at [0] → newest ends up on top
        state.add_trade(TradeRecord(
            trade_id      = t.trade_id,
            ticker        = t.ticker,
            side          = t.side.upper(),
            price_cents   = t.price_cents,
            num_contracts = t.num_contracts,
            cost_dollars  = t.cost_dollars,
            opened_at     = t.opened_at,
            closed_at     = t.closed_at,
            pnl_dollars   = t.pnl_dollars,
            outcome       = t.outcome,
        ))
    state.apply_stats(risk_manager.get_stats())

    # Start dashboard — opens browser automatically
    start_dashboard(open_browser=True)

    state.status = "Press ARBITRAGE BOT to start trading"
    print_prices = log_cfg.get("print_prices", True)

    logger.info("Starting price feeds and trading loop...")
    await asyncio.gather(
        run_binance_feed(price_store,   print_prices=print_prices),   # Kraken
        run_coinbase_feed(price_store,  print_prices=print_prices),
        run_bitstamp_feed(price_store,  print_prices=False),
        run_gemini_feed(price_store,    print_prices=False),
        trading_loop(price_store, config, kalshi_client, signal_engine, risk_manager),
        v2_trading_loop(price_store, kalshi_client),
        mm_trading_loop(price_store, kalshi_client),
    )


async def main_no_browser():
    """
    Same as main() but does NOT open a browser tab.
    Called by app.py which opens the dashboard in a native window instead.
    """
    config     = load_config("config.yaml")
    paper_mode = config.get("trading", {}).get("paper_mode", True)
    log_cfg    = config.get("logging", {})

    log_file = str(BOT_DIR / log_cfg.get("log_file", "bot_trades.log"))
    setup_logging(
        log_file  = log_file,
        log_level = log_cfg.get("log_level", "INFO"),
    )

    logger = logging.getLogger("main")

    # Optional remote kill switch via Telegram. No-op when not configured.
    def _apply_assessment_settings() -> str:
        suggestions = getattr(state, "last_assessment_suggestions", None)
        if not suggestions:
            return "⚠️ No assessment available yet — wait for the next update."
        payload = {k: v for k, v in suggestions.items() if k != "note"}
        before = {k: getattr(state.settings, k, "?") for k in payload}
        state.settings.update_from_dict(payload)
        _persist_settings_to_config(payload)
        lines = ["✅ Assessment settings applied:"]
        labels = {
            "momentum_threshold_pct": "Momentum threshold",
            "momentum_window_secs":   "Momentum window",
            "early_min_distance_pct": "Early entry distance",
            "max_trades_per_cycle":   "Max trades/cycle",
            "sl_min_hold_secs":       "SL min hold",
        }
        for k, v in payload.items():
            label = labels.get(k, k)
            lines.append(f"  {label}: {before[k]} → {v}")
        return "\n".join(lines)
    start_telegram_kill_switch(config, apply_fn=_apply_assessment_settings)

    # Seed live settings from config so the dashboard shows correct values on startup
    _seed_settings_from_config(config)
    state.paper_mode = paper_mode
    state.status     = "Starting up..."

    price_store   = PriceStore()
    signal_engine = SignalEngine(config)
    risk_manager  = RiskManager(config)

    try:
        kalshi_client = load_client_from_config(config)
    except (ValueError, FileNotFoundError) as e:
        print(f"\nERROR: {e}\n")
        sys.exit(1)

    try:
        balance = kalshi_client.get_balance()
        state.balance_dollars = balance
        logger.info(f"Kalshi account balance: ${balance:.2f}")
        _init_day_start_balance(balance)
    except Exception as e:
        logger.error(f"Could not connect to Kalshi: {e}")
        sys.exit(1)

    # Background reconciler — periodically syncs the dashboard's daily P&L
    # to Kalshi's authoritative numbers. Runs in a daemon thread so it never
    # blocks the trading loop. No-op if disabled in config.
    start_reconciler(kalshi_client, config)

    # BTC market assessor — runs at 9 AM and 3 PM, sends report via Telegram
    # and stores it in state.last_assessment for the dashboard to display.
    start_assessor(notify_fn=notify, state=state)

    # Restore P&L counters from previous sessions (trade list stays clean each session)
    risk_manager.load_trades()
    state.apply_stats(risk_manager.get_stats())

    # Start dashboard WITHOUT opening a browser (app.py handles the window)
    start_dashboard(open_browser=False)

    state.status = "Press ARBITRAGE BOT to start trading"
    print_prices = log_cfg.get("print_prices", True)

    logger.info("Starting price feeds and trading loop...")
    await asyncio.gather(
        run_binance_feed(price_store,   print_prices=print_prices),   # Kraken
        run_coinbase_feed(price_store,  print_prices=print_prices),
        run_bitstamp_feed(price_store,  print_prices=False),
        run_gemini_feed(price_store,    print_prices=False),
        trading_loop(price_store, config, kalshi_client, signal_engine, risk_manager),
        v2_trading_loop(price_store, kalshi_client),
        mm_trading_loop(price_store, kalshi_client),
    )


if __name__ == "__main__":
    asyncio.run(main())
