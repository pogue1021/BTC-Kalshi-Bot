"""
dashboard_server.py — Web Dashboard Server
============================================
Runs a lightweight HTTP server alongside the bot.
Serves the dashboard HTML at http://localhost:5000
and live JSON data at http://localhost:5000/api/state

Runs in a background thread so it doesn't interfere
with the async trading loop.
"""

import json
import logging
import os
import re
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from bot_state import state

CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Map from LiveSettings field name → (yaml_section, yaml_key)
_SETTINGS_TO_CONFIG = {
    "trade_window_start_minutes":  ("signal", "trade_window_start_minutes"),
    "trade_window_end_minutes":    ("signal", "trade_window_end_minutes"),
    "early_entry_window_minutes":  ("signal", "early_entry_window_minutes"),
    "early_min_distance_pct":      ("signal", "early_min_distance_pct"),
    "early_max_yes_cents":         ("signal", "early_max_yes_cents"),
    "late_window_fallback_enabled":("signal", "late_window_fallback_enabled"),
    "late_window_fallback_minutes":("signal", "late_window_fallback_minutes"),
    "late_window_min_distance_pct":("signal", "late_window_min_distance_pct"),
    "late_window_max_yes_cents":   ("signal", "late_window_max_yes_cents"),
    "momentum_threshold_pct":      ("signal", "momentum_threshold_pct"),
    "momentum_window_secs":        ("signal", "momentum_window_seconds"),
    "min_yes_price_cents":         ("signal", "min_yes_price_cents"),
    "max_yes_price_cents":         ("signal", "max_yes_price_cents"),
    "stop_loss_cents":             ("signal", "stop_loss_cents"),
    "take_profit_cents":           ("signal", "take_profit_cents"),
    "sl_min_hold_secs":            ("signal", "sl_min_hold_secs"),
    "sl_disable_mins":             ("signal", "sl_disable_mins"),
    "signal_sl_disable_mins":      ("signal", "signal_sl_disable_mins"),
    "price_sl_disable_mins":       ("signal", "price_sl_disable_mins"),
    "max_trades_per_cycle":        ("signal", "max_trades_per_cycle"),
    "signal_stop_enabled":         ("signal", "signal_stop_enabled"),
    "signal_stop_persistence_secs":("signal", "signal_stop_persistence_secs"),
    "stop_loss_fallback_cents":    ("signal", "stop_loss_fallback_cents"),
    "max_wrong_side_distance_pct": ("signal", "max_wrong_side_distance_pct"),
    "min_confidence_pct":          ("signal", "min_confidence_pct"),
    "sl_cooldown_secs":            ("signal", "sl_cooldown_secs"),
    "max_bet_dollars":             ("trading", "max_bet_dollars"),
}


_persist_logger = logging.getLogger(__name__)


def _persist_settings_to_config(new_vals: dict):
    """
    Write changed slider values back to config.yaml so they survive restarts.
    Uses regex line-replacement so comments and formatting are preserved.
    """
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")

        for field, (_section, key) in _SETTINGS_TO_CONFIG.items():
            if field not in new_vals:
                continue
            value = new_vals[field]
            if isinstance(value, bool):
                yaml_val = "true" if value else "false"
            elif isinstance(value, float):
                # Keep reasonable precision; drop trailing zeros
                yaml_val = f"{value:.6g}"
            else:
                yaml_val = str(value)
            # Replace the value on the matching key line, preserving any trailing comment.
            # Pattern: leading whitespace, key, colon, old value, optional inline comment.
            pattern = rf'^(\s*{re.escape(key)}\s*:)\s*[^\s#][^\n]*?(\s*#[^\n]*)?$'
            replacement = lambda m, v=yaml_val, k=key: (
                m.group(1) + " " + v + ("  " + m.group(2).strip() if m.group(2) else "")
            )
            text, n = re.subn(pattern, replacement, text, flags=re.MULTILINE)
            if n == 0:
                _persist_logger.debug(f"Key '{key}' not found in config.yaml — skipping")

        dir_path = str(CONFIG_PATH.parent)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=dir_path, suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        os.replace(tmp_path, str(CONFIG_PATH))
        _persist_logger.info("Settings persisted to config.yaml")
    except Exception as e:
        _persist_logger.warning(f"Could not persist settings to config.yaml: {e}")

DASHBOARD_PORT = 5000
DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


class DashboardHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/api/state":
            self._serve_json()
        elif self.path == "/" or self.path == "/dashboard":
            self._serve_html()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/settings":
            self._handle_settings_update()
        elif self.path == "/api/toggle":
            self._handle_toggle()
        elif self.path == "/api/toggle_paper":
            self._handle_toggle_paper()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        # Allow cross-origin requests from the browser
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _handle_toggle(self):
        state.trading_enabled = not state.trading_enabled
        status = "ARMED — watching for signals" if state.trading_enabled else "PAUSED — not placing bets"
        state.status = status
        resp = json.dumps({"ok": True, "trading_enabled": state.trading_enabled}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _handle_toggle_paper(self):
        """
        Flip between PAPER and LIVE mode at runtime. Always auto-pauses
        trading_enabled on switch so the user has to re-arm manually after
        changing modes — a deliberate double gate against accidental live trades.
        """
        state.paper_mode      = not state.paper_mode
        state.trading_enabled = False   # force re-arm after mode change
        mode_str = "PAPER" if state.paper_mode else "LIVE"
        state.status = f"Switched to {mode_str} MODE — re-arm with ARBITRAGE BOT button"
        resp = json.dumps({
            "ok": True,
            "paper_mode": state.paper_mode,
            "trading_enabled": state.trading_enabled,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _handle_settings_update(self):
        try:
            length   = int(self.headers.get("Content-Length", 0))
            raw      = self.rfile.read(length)
            new_vals = json.loads(raw.decode("utf-8"))
            state.settings.update_from_dict(new_vals)
            _persist_settings_to_config(new_vals)
            resp = json.dumps({"ok": True, "settings": state.settings.to_dict()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            err = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def _serve_json(self):
        data = json.dumps(state.to_dict()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _serve_html(self):
        if not DASHBOARD_HTML.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"dashboard.html not found")
            return
        html = DASHBOARD_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format, *args):
        # Suppress the default HTTP request logs so they don't clutter the terminal
        pass


def start_dashboard(open_browser: bool = True):
    """
    Starts the dashboard server in a background thread.
    Call this once from main.py before starting the trading loop.
    """
    server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://localhost:{DASHBOARD_PORT}"
    print(f"\nDashboard running at: {url}")
    print("  On this device:  http://localhost:5000")
    print("  On your network: http://<pi-ip>:5000")
    print("  Via Tailscale:   http://<tailscale-ip>:5000\n")

    if open_browser:
        # Small delay so the server is ready before the browser opens
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    return server
