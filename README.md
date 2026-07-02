# Kalshi BTC Prediction Market Bot

An autonomous trading system for Kalshi's KXBTC15M binary prediction markets — 15-minute Bitcoin settlement contracts. Built in Python with a fully async architecture, live web dashboard, and three independently-operated trading strategies.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)
![Asyncio](https://img.shields.io/badge/Architecture-Async%2FAwait-009688?style=flat)
![Deployment](https://img.shields.io/badge/Deployed-Raspberry%20Pi-C51A4A?style=flat&logo=raspberry-pi&logoColor=white)
![Markets](https://img.shields.io/badge/Exchange-Kalshi-0066FF?style=flat)

---

## Overview

The bot connects to four real-time BTC price feeds simultaneously (Binance, Coinbase, Bitstamp, Gemini), computes a consensus CF Benchmarks estimate, and uses it to trade on Kalshi's prediction market API. All strategy parameters are adjustable live from a web dashboard — no restarts required.

Three strategies run as concurrent async coroutines, each with its own state machine and dashboard:

| Strategy | Approach | Entry condition |
|---|---|---|
| **V1 — Directional** | Momentum-based taker | BTC trending away from strike, momentum threshold met |
| **V2 — Late Window** | Hold-to-settlement | BTC far from strike in final minutes, Kalshi price lagging |
| **V3 — Market Maker** | Passive spread capture | YES price > 85c or < 15c; post limit orders both sides |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         main.py                             │
│                   asyncio.gather(...)                       │
└──────┬──────────┬──────────┬──────────┬──────────┬──────────┘
       │          │          │          │          │
  Binance    Coinbase   Bitstamp   Gemini      Dashboard
  WS Feed    WS Feed    WS Feed   WS Feed      Server
       │          │          │          │    (HTTP :5000)
       └──────────┴──────────┴──────────┘
                       │
                  PriceStore
             (consensus CF estimate)
                       │
          ┌────────────┼────────────┐
          │            │            │
      Strategy V1  Strategy V2  Strategy V3
      Directional  Late Window  Market Maker
          │            │            │
          └────────────┴────────────┘
                       │
                 KalshiClient
          (RSA-signed REST API calls)
                       │
             ┌─────────┴─────────┐
        Kalshi API          Reconciler
     (orders, fills,      (background P&L
      settlements)         verification)
```

Eight async coroutines run in a single event loop: four WebSocket price feeds, three strategy loops on a 5-second tick, and a background reconciler that cross-checks the bot's internal P&L against Kalshi's API truth.

---

## Key Engineering Details

**RSA request signing** — Kalshi's API requires RSA-SHA256 signatures on every request. The client constructs the canonical message, signs with a PEM private key, and attaches the signature header.

**Four-feed consensus** — `PriceStore` ingests ticks from all four exchanges via independent WebSocket coroutines with auto-reconnect. The CF estimate requires at least 2 of 4 feeds live before any trade fires.

**Live parameter control** — The dashboard server handles `POST /api/settings` by updating the in-memory settings dataclass and writing back to `config.yaml` using regex line replacement, preserving all comments. Changes survive restarts.

**Background reconciler** — A separate coroutine polls `GET /portfolio/fills` on a schedule and computes Kalshi-truth P&L independently of the bot's internal tracking, catching ghost trades and fee discrepancies.

**Data-driven strategy calibration** — After accumulating live trading data, a standalone analysis script parsed 99,755 log lines and 62,993 consecutive tick observations to build a volatility calibration across YES price and time remaining. This directly determined the quoting thresholds for the V3 market maker.

**Inventory management (V3)** — The market maker tracks net position per ticker in real time by diffing `get_open_orders()` against previously placed orders. When inventory exceeds a configurable limit, quotes are skewed to reduce exposure. A hard limit triggers full quote withdrawal.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Async runtime | `asyncio` + `websockets` |
| Kalshi API auth | `cryptography` (RSA-SHA256) |
| Config | `PyYAML` + `python-dotenv` |
| Dashboard | Vanilla JS + `http.server` |
| Deployment | Raspberry Pi 4 via Tailscale VPN |
| Alerting | Telegram Bot API |

---

## Project Structure

```
├── main.py                # Entry point; orchestrates all coroutines
├── kalshi_client.py       # Kalshi REST API wrapper
├── price_feeds.py         # WebSocket feed handlers for 4 exchanges
├── signal_engine.py       # Momentum signal computation (V1)
├── risk_manager.py        # Position sizing and daily loss limits
├── strategy_v2.py         # Late-window edge strategy (V2)
├── strategy_mm.py         # Market maker strategy (V3)
├── bot_state.py           # V1 live state + settings dataclass
├── bot_state_v2.py        # V2 live state + settings dataclass
├── bot_state_mm.py        # V3 live state + inventory tracking
├── dashboard_server.py    # HTTP server for all three dashboards
├── dashboard.html         # V1 live dashboard
├── dashboard_v2.html      # V2 dashboard
├── dashboard_mm.html      # V3 market maker dashboard
├── kalshi_reconciler.py   # Background P&L cross-checker
├── analyze_log.py         # Standalone log analysis + calibration tool
├── telegram_kill.py       # Remote kill switch
└── config.yaml            # All parameters (hot-reloadable via dashboard)
```

---

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your Kalshi API credentials, then:

```bash
python main.py
```

The dashboard opens at `http://localhost:5000`. All three strategy dashboards are at `/`, `/v2`, and `/mm`.

---

## Safety

- All three strategies boot in **paper mode** by default. Live trading requires an explicit toggle plus a manual re-arm step.
- A **daily loss limit** halts all trading if cumulative losses exceed the configured threshold.
- Credentials are excluded from version control via `.gitignore`. No keys appear in code.
- A **Telegram kill switch** can halt trading remotely.
