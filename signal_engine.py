"""
signal_engine.py -- Trading Signal Engine
==========================================
Generates YES / NO / HOLD signals based on CF Benchmarks estimate momentum.

Strategy:
  CF Benchmarks calculates the BTC settlement rate as a weighted median
  of trades across Coinbase, Kraken, Bitstamp, and Gemini. We approximate
  this rate in real time and measure its momentum over a rolling window.

  When the CF estimate is trending strongly in one direction and the
  Kalshi market hasn't fully repriced yet, we have an edge.

  Signal conditions (ALL must be true):
    1. Within the trading window (X to Y minutes before close)
    2. CF estimate momentum >= threshold
    3. Exchange consensus agrees on direction (optional boost to confidence)
    4. Kalshi market yes-price is in the tradeable range (not already one-sided)
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from price_feeds import PriceStore
from bot_state import state

logger = logging.getLogger(__name__)


class Signal(Enum):
    YES  = "YES"
    NO   = "NO"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    signal:       Signal
    confidence:   float
    cf_estimate:  float
    momentum_pct: float
    feeds_live:   int
    consensus:    Optional[str]   # "up", "down", or None
    reason:       str
    # Kept for backward compat with main.py display code
    binance_price:  float = 0.0
    coinbase_price: float = 0.0
    divergence_pct: float = 0.0


class SignalEngine:

    def __init__(self, config: dict):
        # Seed live settings from config.yaml on startup
        signal_cfg = config.get("signal", {})
        state.settings.momentum_threshold_pct     = signal_cfg.get("momentum_threshold_pct", 0.02)
        state.settings.momentum_window_secs       = signal_cfg.get("momentum_window_seconds", 15)
        state.settings.min_yes_price_cents        = signal_cfg.get("min_yes_price_cents", 20)
        state.settings.max_yes_price_cents        = signal_cfg.get("max_yes_price_cents", 80)
        state.settings.trade_window_start_minutes = signal_cfg.get("trade_window_start_minutes", 12)
        state.settings.stop_loss_cents            = signal_cfg.get("stop_loss_cents", 20)
        state.settings.take_profit_cents          = signal_cfg.get("take_profit_cents", 97)
        state.settings.sl_min_hold_secs           = signal_cfg.get("sl_min_hold_secs", 90)
        state.settings.sl_disable_mins            = signal_cfg.get("sl_disable_mins", 3.0)
        # Split SL gates: soft (signal/legacy) defaults to legacy sl_disable_mins
        # for backward compat; hard (price-collapse) defaults to 0.5 min so it
        # stays armed almost to the bell against catastrophic moves.
        state.settings.signal_sl_disable_mins     = float(signal_cfg.get(
            "signal_sl_disable_mins", state.settings.sl_disable_mins))
        state.settings.price_sl_disable_mins      = float(signal_cfg.get(
            "price_sl_disable_mins", 0.5))
        state.settings.max_trades_per_cycle       = signal_cfg.get("max_trades_per_cycle", 3)
        state.settings.signal_stop_enabled          = bool(signal_cfg.get("signal_stop_enabled", True))
        state.settings.signal_stop_persistence_secs = int(signal_cfg.get("signal_stop_persistence_secs", 15))
        state.settings.stop_loss_fallback_cents     = int(signal_cfg.get("stop_loss_fallback_cents", 30))
        state.settings.min_yes_price_cents        = signal_cfg.get("min_yes_price_cents", 36)
        state.settings.early_entry_window_minutes = signal_cfg.get("early_entry_window_minutes", 12)
        state.settings.early_min_distance_pct     = signal_cfg.get("early_min_distance_pct", 0.10)
        state.settings.early_max_yes_cents        = signal_cfg.get("early_max_yes_cents", 65)
        state.settings.trade_window_end_minutes   = signal_cfg.get("trade_window_end_minutes", 1)
        # Late-window fallback (Option B)
        state.settings.late_window_fallback_enabled  = bool(signal_cfg.get("late_window_fallback_enabled", True))
        state.settings.late_window_fallback_minutes  = float(signal_cfg.get("late_window_fallback_minutes", 3.0))
        state.settings.late_window_min_distance_pct  = float(signal_cfg.get("late_window_min_distance_pct", 0.04))
        state.settings.late_window_max_yes_cents     = int(signal_cfg.get("late_window_max_yes_cents", 72))
        # How far CF can be on the WRONG side of the strike and still allow a
        # "near target" momentum entry. Above this, we refuse to bet against
        # the current CF position even if momentum looks good — the gap is
        # too wide for a 30s momentum push to plausibly close.
        # 0.015% ≈ $11 on $76k BTC. Set to 0 to require CF on the favorable side.
        state.settings.max_wrong_side_distance_pct   = float(signal_cfg.get("max_wrong_side_distance_pct", 0.015))
        state.settings.min_confidence_pct         = float(signal_cfg.get("min_confidence_pct", 0.50))
        state.settings.sl_cooldown_secs           = int(signal_cfg.get("sl_cooldown_secs", 60))

    def evaluate(
        self,
        price_store: PriceStore,
        market_yes_price_cents: int,
        seconds_until_close: float,
        config: dict,
        floor_strike: float = None,
        market_no_price_cents: int = 0,
    ) -> SignalResult:
        cfg = state.settings

        # ── 1. Feed readiness check ───────────────────────────────────────────
        if not price_store.is_ready():
            return SignalResult(
                signal=Signal.HOLD, confidence=0.0,
                cf_estimate=0.0, momentum_pct=0.0,
                feeds_live=len(price_store.live_exchanges()),
                consensus=None,
                reason=f"Waiting for feeds ({', '.join(price_store.live_exchanges()) or 'none'} live)"
            )

        cf_now  = price_store.get_cf_estimate()
        live_ex = price_store.live_exchanges()

        if cf_now is None:
            return SignalResult(
                signal=Signal.HOLD, confidence=0.0,
                cf_estimate=0.0, momentum_pct=0.0,
                feeds_live=len(live_ex), consensus=None,
                reason="CF estimate unavailable"
            )

        # ── 2. Trading window check ───────────────────────────────────────────
        window_start        = cfg.trade_window_start_minutes * 60   # e.g. 6 min = 360s
        window_end          = cfg.trade_window_end_minutes   * 60   # e.g. 1 min = 60s
        early_window        = getattr(cfg, "early_entry_window_minutes", 12) * 60  # e.g. 12 min
        early_min_dist_pct  = getattr(cfg, "early_min_distance_pct", 0.10)
        early_max_yes_cents = getattr(cfg, "early_max_yes_cents", 65)

        # Completely outside all windows
        if seconds_until_close > early_window:
            return SignalResult(
                signal=Signal.HOLD, confidence=0.0,
                cf_estimate=cf_now, momentum_pct=0.0,
                feeds_live=len(live_ex), consensus=None,
                reason=f"Waiting -- {seconds_until_close/60:.1f} min left "
                       f"(early window opens at {early_window/60:.0f} min)"
            )

        # Too close to close
        if seconds_until_close < window_end:
            return SignalResult(
                signal=Signal.HOLD, confidence=0.0,
                cf_estimate=cf_now, momentum_pct=0.0,
                feeds_live=len(live_ex), consensus=None,
                reason=f"Too late — only {seconds_until_close:.0f}s left"
            )

        # Flag whether we're in early window (6–12 min) or normal window (< 6 min)
        is_early_window = seconds_until_close > window_start

        # ── 3. Market price range check ───────────────────────────────────────
        # Early window uses a tighter ceiling — if Kalshi has already priced
        # the move in (YES > 65c), there's no edge left to capture early.
        effective_max_yes = early_max_yes_cents if is_early_window else cfg.max_yes_price_cents

        if market_yes_price_cents <= 0:
            return SignalResult(
                signal=Signal.HOLD, confidence=0.0,
                cf_estimate=cf_now, momentum_pct=0.0,
                feeds_live=len(live_ex), consensus=None,
                reason="Market price unavailable"
            )

        # Use actual NO ask if provided, otherwise approximate from YES price.
        # Check both sides — a low YES price means a potentially attractive NO
        # trade, not a reason to skip the entire market.
        no_price     = market_no_price_cents if market_no_price_cents > 0 else (100 - market_yes_price_cents)
        yes_in_range = cfg.min_yes_price_cents <= market_yes_price_cents <= effective_max_yes
        # NO price uses the regular max (80c), not the early-window YES ceiling (70c).
        # The early ceiling only applies to YES entries — NO at 74c is valid even early.
        no_in_range  = cfg.min_yes_price_cents <= no_price <= cfg.max_yes_price_cents

        if not yes_in_range and not no_in_range:
            if market_yes_price_cents < cfg.min_yes_price_cents:
                return SignalResult(
                    signal=Signal.HOLD, confidence=0.0,
                    cf_estimate=cf_now, momentum_pct=0.0,
                    feeds_live=len(live_ex), consensus=None,
                    reason=f"YES {market_yes_price_cents}c too low, NO {no_price}c also out of range "
                           f"(min {cfg.min_yes_price_cents}c)"
                )
            else:
                tag = " [early window]" if is_early_window else ""
                return SignalResult(
                    signal=Signal.HOLD, confidence=0.0,
                    cf_estimate=cf_now, momentum_pct=0.0,
                    feeds_live=len(live_ex), consensus=None,
                    reason=f"YES price {market_yes_price_cents}c too high (max {effective_max_yes}c{tag})"
                )
        # At least one side is in range — fall through to CF/momentum logic.

        # ── 4. CF Estimate momentum ───────────────────────────────────────────
        momentum_window = int(getattr(cfg, "momentum_window_secs", 15))
        cf_past = price_store.get_cf_estimate_n_seconds_ago(momentum_window)

        if cf_past is None or cf_past == 0:
            return SignalResult(
                signal=Signal.HOLD, confidence=0.0,
                cf_estimate=cf_now, momentum_pct=0.0,
                feeds_live=len(live_ex), consensus=None,
                reason=f"Building {momentum_window}s price history..."
            )

        momentum_pct = ((cf_now - cf_past) / cf_past) * 100

        # ── 5. Exchange consensus (confidence booster) ────────────────────────
        consensus = price_store.exchange_consensus()

        # ── 6. Signal generation ──────────────────────────────────────────────
        #
        # PRIMARY signal: where is CF estimate relative to the target price?
        #   If BTC is comfortably above target → YES is likely to win
        #   If BTC is comfortably below target → NO is likely to win
        #
        # SECONDARY signal: momentum confirms direction
        #   If above target AND trending up   → stronger YES
        #   If below target AND trending down → stronger NO
        #   If moving toward target (against us) → lower confidence or HOLD
        #
        # FALLBACK (no target available): pure momentum as before

        threshold   = cfg.momentum_threshold_pct
        abs_mom     = abs(momentum_pct)

        if floor_strike is not None and floor_strike > 0:
            # How far is CF estimate from target, as a percentage?
            cf_vs_target_pct = ((cf_now - floor_strike) / floor_strike) * 100
            distance_dollars = cf_now - floor_strike

            # Determine base direction from target position
            if cf_vs_target_pct > 0:
                base_signal = Signal.YES   # BTC above target
                base_dir    = "up"
            else:
                base_signal = Signal.NO    # BTC below target
                base_dir    = "down"

            # ── Early window gate ─────────────────────────────────────────────
            # In the early window (6–12 min), only trade if:
            #   1. CF is very far from target (overwhelming signal)
            #   2. Momentum is confirming (not fighting the direction)
            if is_early_window:
                momentum_confirms_early = (
                    (base_signal == Signal.YES and momentum_pct > 0) or
                    (base_signal == Signal.NO  and momentum_pct < 0)
                )
                if abs(cf_vs_target_pct) < early_min_dist_pct:
                    return SignalResult(
                        signal=Signal.HOLD, confidence=0.0,
                        cf_estimate=round(cf_now, 2),
                        momentum_pct=round(momentum_pct, 4),
                        feeds_live=len(live_ex), consensus=consensus,
                        reason=f"Early window: CF only ${abs(distance_dollars):.0f} from target "
                               f"(need ${floor_strike * early_min_dist_pct / 100:.0f}+ for early entry)"
                    )
                if not momentum_confirms_early:
                    return SignalResult(
                        signal=Signal.HOLD, confidence=0.0,
                        cf_estimate=round(cf_now, 2),
                        momentum_pct=round(momentum_pct, 4),
                        feeds_live=len(live_ex), consensus=consensus,
                        reason=f"Early window: CF ${distance_dollars:+.0f} from target but "
                               f"momentum {momentum_pct:+.4f}% not confirming — waiting"
                    )

            # Is momentum moving us toward or away from the target?
            # "toward" = bad (could cross target), "away" = good (more certain)
            momentum_toward_target = (
                (base_signal == Signal.YES and momentum_pct < 0) or
                (base_signal == Signal.NO  and momentum_pct > 0)
            )

            # Minimum distance from target to trade (0.03% ≈ $22 on $73k BTC)
            min_distance_pct = 0.03

            if abs(cf_vs_target_pct) < min_distance_pct:
                # Too close to target — outcome is a coin flip, use momentum instead
                if abs_mom < threshold:
                    return SignalResult(
                        signal=Signal.HOLD, confidence=0.0,
                        cf_estimate=round(cf_now, 2),
                        momentum_pct=round(momentum_pct, 4),
                        feeds_live=len(live_ex), consensus=consensus,
                        reason=f"CF within ${abs(distance_dollars):.0f} of target "
                               f"(${floor_strike:,.2f}) — too close, waiting for momentum"
                    )
                # Near target but strong momentum — signal in momentum direction
                signal    = Signal.YES if momentum_pct > 0 else Signal.NO
                direction = "up" if momentum_pct > 0 else "down"

                # Wrong-side guard: if the chosen side puts CF on the wrong side
                # of the strike (e.g. buying YES while CF is BELOW strike), we
                # require CF to be very close. Beyond max_wrong_side_distance_pct,
                # the gap is too wide for momentum to plausibly close in time.
                # This is what was burning the bot — buying YES with CF $21 below
                # strike, hoping a 0.03% momentum tick would carry it across.
                chose_wrong_side = (
                    (signal == Signal.YES and cf_now <= floor_strike) or
                    (signal == Signal.NO  and cf_now >= floor_strike)
                )
                max_wrong_side_pct = float(getattr(cfg, "max_wrong_side_distance_pct", 0.015))
                if chose_wrong_side and abs(cf_vs_target_pct) > max_wrong_side_pct:
                    max_wrong_dollars = floor_strike * max_wrong_side_pct / 100
                    return SignalResult(
                        signal=Signal.HOLD, confidence=0.0,
                        cf_estimate=round(cf_now, 2),
                        momentum_pct=round(momentum_pct, 4),
                        feeds_live=len(live_ex), consensus=consensus,
                        reason=(
                            f"Wrong side: would buy {signal.value} but CF is ${abs(distance_dollars):.0f} "
                            f"{'below' if cf_now < floor_strike else 'above'} strike — "
                            f"max ${max_wrong_dollars:.0f} on wrong side"
                        ),
                        binance_price=price_store.binance_price or 0.0,
                        coinbase_price=price_store.coinbase_price or 0.0,
                        divergence_pct=momentum_pct,
                    )

                mom_score = min(abs_mom / (threshold * 4), 1.0)
                feed_score = len(live_ex) / 4
                consensus_bonus = 0.1 if consensus == direction else 0.0
                confidence = min(0.35 + 0.4 * ((mom_score + feed_score) / 2) + consensus_bonus, 0.75)
                wrong_tag = " [WRONG-SIDE, close]" if chose_wrong_side else ""
                return SignalResult(
                    signal=signal, confidence=round(confidence, 2),
                    cf_estimate=round(cf_now, 2),
                    momentum_pct=round(momentum_pct, 4),
                    feeds_live=len(live_ex), consensus=consensus,
                    reason=f"Near target (${floor_strike:,.2f}, Δ${distance_dollars:+.0f}){wrong_tag} — "
                           f"momentum {momentum_pct:+.4f}% pushing {'above' if signal==Signal.YES else 'below'}",
                    binance_price=price_store.binance_price or 0.0,
                    coinbase_price=price_store.coinbase_price or 0.0,
                    divergence_pct=momentum_pct,
                )

            # CF is clearly above or below target
            # Don't trade if momentum is strongly heading toward target (reversal risk)
            if momentum_toward_target and abs_mom > threshold * 3:
                return SignalResult(
                    signal=Signal.HOLD, confidence=0.0,
                    cf_estimate=round(cf_now, 2),
                    momentum_pct=round(momentum_pct, 4),
                    feeds_live=len(live_ex), consensus=consensus,
                    reason=f"CF ${distance_dollars:+.0f} {'above' if base_signal==Signal.YES else 'below'} "
                           f"target but momentum moving toward it — waiting"
                )

            signal    = base_signal
            direction = base_dir

            # Confidence: momentum is now the primary factor (30% weight).
            # CF distance and feed count are secondary. Penalise if momentum
            # is moving toward the target (even if not strong enough to HOLD).
            dist_score = min(abs(cf_vs_target_pct) / 0.15, 1.0)   # maxes out at 0.15% away
            feed_score = len(live_ex) / 4
            mom_score  = 0.0 if momentum_toward_target else min(abs_mom / (threshold * 3), 1.0)
            consensus_bonus = 0.05 if consensus == direction else 0.0
            confidence = min(0.40 + 0.20 * dist_score + 0.25 * mom_score + 0.10 * feed_score + consensus_bonus, 1.0)

            mom_str   = f", momentum {momentum_pct:+.4f}%" if abs_mom >= threshold else ""
            early_tag = " [EARLY ENTRY]" if is_early_window else ""
            return SignalResult(
                signal=signal, confidence=round(confidence, 2),
                cf_estimate=round(cf_now, 2),
                momentum_pct=round(momentum_pct, 4),
                feeds_live=len(live_ex), consensus=consensus,
                reason=f"{early_tag}CF ${distance_dollars:+.0f} {'above' if signal==Signal.YES else 'below'} "
                       f"target ${floor_strike:,.2f}{mom_str} ({len(live_ex)}/4 feeds)",
                binance_price=price_store.binance_price or 0.0,
                coinbase_price=price_store.coinbase_price or 0.0,
                divergence_pct=momentum_pct,
            )

        else:
            # ── Fallback: no target price available, use pure momentum ────────
            if abs_mom < threshold:
                return SignalResult(
                    signal=Signal.HOLD, confidence=0.0,
                    cf_estimate=round(cf_now, 2),
                    momentum_pct=round(momentum_pct, 4),
                    feeds_live=len(live_ex), consensus=consensus,
                    reason=f"CF momentum {momentum_pct:+.4f}% below {threshold}% threshold "
                           f"(no target price available)"
                )

            direction = "up" if momentum_pct > 0 else "down"
            signal    = Signal.YES if direction == "up" else Signal.NO
            mom_score  = min(abs_mom / (threshold * 4), 1.0)
            feed_score = len(live_ex) / 4
            consensus_bonus = 0.1 if consensus == direction else 0.0
            confidence = min(0.4 + 0.5 * ((mom_score + feed_score) / 2) + consensus_bonus, 1.0)

            return SignalResult(
                signal=signal, confidence=round(confidence, 2),
                cf_estimate=round(cf_now, 2),
                momentum_pct=round(momentum_pct, 4),
                feeds_live=len(live_ex), consensus=consensus,
                reason=f"[No target] CF momentum {momentum_pct:+.4f}% over {momentum_window}s",
                binance_price=price_store.binance_price or 0.0,
                coinbase_price=price_store.coinbase_price or 0.0,
                divergence_pct=momentum_pct,
            )

    # ─────────────────────────────────────────────────────────
    # LATE-WINDOW FALLBACK  (Option B)
    # ─────────────────────────────────────────────────────────
    def evaluate_late_window_fallback(
        self,
        price_store: PriceStore,
        market_yes_price_cents: int,
        market_no_price_cents:  int,
        seconds_until_close:    float,
        floor_strike:           float,
        trade_count_this_cycle: int,
    ) -> Optional[SignalResult]:
        """
        Secondary signal path: if the primary evaluate() returned HOLD but we're
        in the final minutes of a cycle with no trade yet, take whichever side
        CF clearly favors — as long as the market hasn't already priced it in.

        Returns a SignalResult (YES or NO) if conditions met, otherwise None
        (meaning: caller should keep holding).

        Toggle off via `late_window_fallback_enabled: false` in config or the
        dashboard to fully revert to the prior "wait for momentum" behavior.
        """
        cfg = state.settings
        if not bool(getattr(cfg, "late_window_fallback_enabled", True)):
            return None
        if trade_count_this_cycle > 0:
            return None                     # already traded this cycle
        if not price_store.is_ready() or floor_strike is None or floor_strike <= 0:
            return None

        # Only fires in the final N minutes
        late_mins = float(getattr(cfg, "late_window_fallback_minutes", 3.0))
        if seconds_until_close > late_mins * 60:
            return None
        # Don't fire in the dying seconds when we'd have to pay whatever spread exists
        window_end = cfg.trade_window_end_minutes * 60
        if seconds_until_close < window_end:
            return None

        cf_now  = price_store.get_cf_estimate()
        live_ex = price_store.live_exchanges()
        if cf_now is None:
            return None

        cf_vs_target_pct = ((cf_now - floor_strike) / floor_strike) * 100
        distance_dollars = cf_now - floor_strike
        min_dist_pct = float(getattr(cfg, "late_window_min_distance_pct", 0.04))
        if abs(cf_vs_target_pct) < min_dist_pct:
            return None  # CF too close to strike — coin flip, skip

        # Which side does CF favor?
        side_signal = Signal.YES if cf_now > floor_strike else Signal.NO
        side_price  = market_yes_price_cents if side_signal == Signal.YES else market_no_price_cents
        max_price   = int(getattr(cfg, "late_window_max_yes_cents", 72))

        if side_price <= 0 or side_price > max_price:
            return None  # no real edge left — market already priced it in

        # Also respect the global min price (skip dust longshots)
        if side_price < cfg.min_yes_price_cents:
            return None

        mins_left = seconds_until_close / 60
        return SignalResult(
            signal      = side_signal,
            confidence  = 0.60,
            cf_estimate = round(cf_now, 2),
            momentum_pct= 0.0,
            feeds_live  = len(live_ex),
            consensus   = None,
            reason      = (
                f"[LATE-WINDOW FALLBACK] CF ${distance_dollars:+.0f} "
                f"{'above' if side_signal == Signal.YES else 'below'} strike ${floor_strike:,.0f}, "
                f"{mins_left:.1f}min left, {side_signal.value} @ {side_price}c — no prior trade this cycle"
            ),
            binance_price  = price_store.binance_price or 0.0,
            coinbase_price = price_store.coinbase_price or 0.0,
            divergence_pct = momentum_pct,
        )
