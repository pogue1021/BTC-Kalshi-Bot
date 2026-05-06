"""
telegram_kill.py — Remote kill switch via Telegram
======================================================
Lets you shut the bot down from your phone by texting "STOP" to a
private Telegram bot you control. Completely OPT-IN: if no token is
configured in config.yaml, this module does nothing and the rest of
the bot is unaffected.

One-time setup
--------------
  1. Open Telegram, talk to @BotFather, send /newbot, save the token.
  2. Open your new bot in Telegram and send it any message.
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser
     and copy the numeric "chat":{"id": <NUMBER>} value.
  4. In config.yaml, add:

         telegram:
           bot_token: "1234567890:ABCdef..."
           chat_id:   123456789

How to use
----------
  - From your phone, text the word STOP (case-insensitive) to your
    bot. The bot replies "Shutting down" and exits the Python process.
  - Manually relaunch python main.py / app.py when you return to your
    computer. There is no auto-restart.
  - Other messages are ignored. Only the configured chat_id can
    trigger a shutdown — strangers who somehow find your bot cannot.

Caveat for LIVE mode
--------------------
  This is a HARD process kill (os._exit). If a position is open on
  Kalshi when you STOP, that position stays open until the market
  settles or you close it manually in the Kalshi web UI. In paper
  mode this doesn't matter.
"""

import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Telegram Bot API endpoint template
_API = "https://api.telegram.org/bot{token}/{method}"

# Long-poll timeout. Telegram holds the connection open up to this many
# seconds waiting for a message — keeps the loop quiet when idle.
_POLL_TIMEOUT_SECS = 25

# Module-level cache of credentials, populated by start_telegram_kill_switch.
# When None/None, notify() is a no-op so the rest of the bot is unaffected.
_token_cache: Optional[str] = None
_chat_id_cache: Optional[int] = None


def _send(token: str, chat_id: int, text: str) -> None:
    """Best-effort message back to the user. Never raises."""
    try:
        requests.post(
            _API.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def _drain_pending_updates(token: str) -> Optional[int]:
    """
    On startup, read and DISCARD any messages already sitting in the
    Telegram queue (e.g. a STOP from the previous run). Returns the
    highest update_id seen so the polling loop can resume cleanly with
    offset = max_id + 1, or None if the queue was empty.

    Without this step, every restart would re-process the last STOP
    and shut the bot down immediately. Best-effort — on failure we
    just start fresh and accept that one stale message might fire.
    """
    try:
        r = requests.get(
            _API.format(token=token, method="getUpdates"),
            params={"timeout": 0},  # return immediately with whatever's queued
            timeout=10,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
        if not updates:
            return None
        return max(u["update_id"] for u in updates)
    except Exception as e:
        logger.warning(f"Could not drain pending Telegram updates: {e}")
        return None


def _poll_loop(token: str, chat_id: int, apply_fn=None) -> None:
    """
    Long-poll Telegram for new messages from `chat_id`.
    Triggers process exit if the user sends STOP.
    Applies pending assessment settings if the user sends APPLY.
    Designed to run forever in a daemon thread; survives transient
    network errors with exponential backoff.
    """
    logger.info("Telegram kill-switch armed — text STOP to shut down, APPLY to apply last assessment")

    # Skip past any messages already queued from previous sessions
    # (e.g. a STOP we already acted on). Otherwise the bot would
    # immediately re-process them and exit on every restart.
    last_update_id: Optional[int] = _drain_pending_updates(token)
    if last_update_id is not None:
        logger.info(f"Skipped stale Telegram updates up to id {last_update_id}")

    _send(token, chat_id, "Kalshi bot online. Text STOP to shut down.")

    backoff = 1

    while True:
        try:
            params = {"timeout": _POLL_TIMEOUT_SECS}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            r = requests.get(
                _API.format(token=token, method="getUpdates"),
                params=params,
                # HTTP read timeout has to exceed the long-poll window
                timeout=_POLL_TIMEOUT_SECS + 10,
            )
            r.raise_for_status()
            data = r.json()
            backoff = 1  # reset backoff on success

            for update in data.get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message")
                if not msg:
                    continue

                # Only obey the configured chat — ignore everyone else
                from_chat = msg.get("chat", {}).get("id")
                if from_chat != chat_id:
                    continue

                text = (msg.get("text") or "").strip().lower()
                if text == "stop":
                    logger.warning("STOP received via Telegram. Exiting.")
                    _send(token, chat_id,
                          "Shutting down now. Manually relaunch when ready.")
                    time.sleep(1)
                    os._exit(0)

                elif text == "apply":
                    if apply_fn is None:
                        _send(token, chat_id, "⚠️ Apply not available.")
                        continue
                    result = apply_fn()
                    _send(token, chat_id, result)

        except requests.exceptions.RequestException as e:
            logger.warning(f"Telegram poll error: {e} — retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            logger.warning(f"Telegram poll unexpected error: {e}")
            time.sleep(5)


def start_telegram_kill_switch(config: dict, apply_fn=None) -> None:
    """
    Start the kill-switch in a background daemon thread, IF the user has
    configured both bot_token and chat_id. Otherwise silently no-op so
    the rest of the bot keeps working exactly as before.

    Also populates the module-level credential cache used by notify().
    """
    global _token_cache, _chat_id_cache

    tg_cfg = config.get("telegram", {}) or {}
    token = (tg_cfg.get("bot_token") or "").strip()
    chat_id_raw = tg_cfg.get("chat_id")

    if not token or chat_id_raw in (None, "", 0):
        logger.info("Telegram kill-switch not configured — skipping")
        return

    try:
        chat_id = int(chat_id_raw)
    except (TypeError, ValueError):
        logger.warning(
            f"Invalid telegram.chat_id: {chat_id_raw!r} — kill-switch disabled"
        )
        return

    # Cache for notify() so trade hooks can push messages without
    # re-reading config or duplicating credential plumbing.
    _token_cache = token
    _chat_id_cache = chat_id

    t = threading.Thread(
        target=_poll_loop,
        args=(token, chat_id, apply_fn),
        daemon=True,
        name="telegram-kill",
    )
    t.start()


def notify(text: str) -> None:
    """
    Push a one-off message to the configured Telegram chat. Safe to call
    from anywhere (sync or async code) — fires off a daemon thread so the
    caller never blocks on network I/O. No-op if Telegram isn't configured.

    Designed for trade alerts and end-of-cycle summaries: cheap to call
    on every event, never raises, never slows down the trading loop.
    """
    if _token_cache is None or _chat_id_cache is None:
        return  # Telegram not configured — nothing to do
    # Snapshot the credentials at call time (in case they ever rotate)
    token   = _token_cache
    chat_id = _chat_id_cache
    threading.Thread(
        target=_send,
        args=(token, chat_id, text),
        daemon=True,
        name="telegram-notify",
    ).start()
