# Kalshi BTC Bot — Setup Guide

Follow these steps in order. The whole setup takes about 10–15 minutes.

---

## Step 1 — Install Python

You need Python 3.10 or newer.

1. Go to https://www.python.org/downloads/
2. Download and install the latest version
3. During install, check **"Add Python to PATH"**
4. Open a terminal (search "Terminal" on Mac, "Command Prompt" on Windows) and verify:
   ```
   python --version
   ```
   You should see something like `Python 3.12.x`

---

## Step 2 — Install Bot Dependencies

Open a terminal, navigate to the folder where this bot lives, and run:

```bash
pip install -r requirements.txt
```

This installs the libraries the bot needs (WebSocket connections, cryptography, etc.).

---

## Step 3 — Create a Kalshi Account

1. Go to https://kalshi.com and sign up
2. Complete identity verification (required to trade)
3. Deposit some funds — even $20 is enough to start testing

**To use the DEMO (practice) account:**
- Go to https://demo.kalshi.com
- Create a separate demo account
- No real money required

---

## Step 4 — Generate Your Kalshi API Key

1. Log into Kalshi (or demo.kalshi.com)
2. Click your profile icon → **Settings** → **API Keys**
3. Click **"Create New API Key"**
4. Give it a name like "btc-bot"
5. You'll receive:
   - A **Key ID** (looks like: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)
   - A **Private Key** (a `.pem` file download)
6. **Save the .pem file** — copy it into the same folder as this bot

> ⚠️ Never share your private key. Anyone with it can trade on your account.

---

## Step 5 — Configure the Bot

Open `config.yaml` in any text editor (Notepad, TextEdit, VS Code, etc.)

Fill in your details:

```yaml
kalshi:
  api_key_id: "paste-your-key-id-here"
  private_key_path: "kalshi_private_key.pem"   # match your .pem filename exactly
  use_demo: true                                 # start with demo!
```

**Leave `paper_mode: true` for now.** This means the bot will find signals and
log exactly what it *would* trade — without actually placing orders. Run it in
paper mode for a few days to see how it performs before going live.

---

## Step 6 — Run the Bot

In your terminal, from the bot's folder:

```bash
python main.py
```

You should see:
```
===========================================================
  🤖  KALSHI BTC BOT  |  Binance→Coinbase Divergence
===========================================================
  ✅ Running in PAPER MODE — no real money at risk
===========================================================

[10:30:01] ✅ Connected to Binance BTC/USDT feed
[10:30:02] ✅ Connected to Coinbase BTC-USD feed
[Binance] $83,241.20  [Coinbase] $83,238.50  [Divergence] +0.003%
```

Stop the bot at any time with **Ctrl+C**.

---

## Step 7 — Review Your Logs

All signals and trades are saved to `bot_trades.log` in the bot folder.
Open it to see what the bot is detecting and why it's trading (or not).

---

## Step 8 — Going Live (When You're Ready)

Only do this after paper trading for several days and you're satisfied with the logic.

1. Open `config.yaml`
2. Change `use_demo: true` → `use_demo: false`
3. Change `paper_mode: true` → `paper_mode: false`
4. Make sure your **live** Kalshi account has funds
5. Run `python main.py`

Start with the minimum bet size (`max_bet_dollars: 5`) and watch the first few
trades carefully before increasing your position size.

---

## Common Issues

**"Config file not found"**
→ Make sure you're running `python main.py` from inside the bot folder, not from your Desktop or Downloads.

**"Private key file not found"**
→ Check that your `.pem` file is in the same folder as `main.py`, and that the filename in `config.yaml` matches exactly (including capitalization).

**"No active 15-minute BTC market found"**
→ Kalshi's BTC markets may not be running 24/7. Try again during US market hours (9am–6pm ET weekdays).

**"Daily loss limit hit"**
→ The bot stopped itself to protect you. The limit resets the next day. To continue sooner, increase `max_daily_loss` in `config.yaml`.

**Signals fire too often / too rarely**
→ Adjust `divergence_threshold_pct` in `config.yaml`:
  - Lower value (e.g. 0.04) = more signals, but noisier
  - Higher value (e.g. 0.15) = fewer signals, but stronger conviction

---

## Understanding the Strategy

The Kalshi 15-minute BTC markets settle based on **Coinbase's BTC-USD price**.

Binance is the world's largest crypto exchange and its BTC price typically **leads** Coinbase by a few seconds to a few minutes — especially during fast-moving markets.

The bot watches for moments where:
1. Binance is significantly **higher** than Coinbase → Coinbase likely catches up → **BET YES**
2. Binance is significantly **lower** than Coinbase → Coinbase likely falls → **BET NO**

This is called a **price feed arbitrage** or **lead-lag strategy**. It doesn't work on every trade, but over many trades it should have a slight edge over random chance.

---

## Questions?

Check the comments inside each `.py` file — every function explains what it does and why.
