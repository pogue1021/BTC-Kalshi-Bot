"""
app.py -- Desktop App Launcher
================================
Run this instead of main.py to open the bot as a native
desktop window (no browser required, no extra installs).

    python app.py

Opens the dashboard in Edge or Chrome app mode -- a clean
borderless window with no address bar or tabs, just like a
real desktop application. Works on all Windows 10/11 machines
because Edge is pre-installed.

Closing the window stops the bot.
"""

import asyncio
import os
import subprocess
import sys
import threading
import time
import webbrowser

import main as bot_main
from dashboard_server import DASHBOARD_PORT


def run_bot_in_thread():
    """Runs the async bot in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot_main.main_no_browser())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Bot error: {e}")


def open_app_window(url):
    """
    Opens the URL as a desktop app window (no browser chrome).
    Tries Edge then Chrome on both Windows and macOS,
    then falls back to the default browser.
    """

    app_flags = [f"--app={url}", "--window-size=1280,820",
                 "--no-first-run", "--no-default-browser-check"]

    # Edge paths (Windows + macOS)
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",  # Windows
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",         # Windows
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",  # macOS
    ]
    for path in edge_paths:
        if os.path.exists(path):
            print("  Opening with Microsoft Edge (app mode)...")
            subprocess.Popen([path] + app_flags)
            return

    # Chrome paths (Windows + macOS)
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",                        # Windows
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",                  # Windows
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),     # Windows user
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",                  # macOS
    ]
    for path in chrome_paths:
        if os.path.exists(path):
            print("  Opening with Google Chrome (app mode)...")
            subprocess.Popen([path] + app_flags)
            return

    # Final fallback: default browser (normal tab)
    print("  Edge/Chrome not found -- opening in default browser...")
    webbrowser.open(url)


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  Kalshi BTC Bot -- Desktop App")
    print("=" * 50)
    print("  Starting bot in background...")
    print("=" * 50 + "\n")

    # Start the bot in a background thread (no browser popup)
    bot_thread = threading.Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()

    # Give the dashboard server a moment to start up
    time.sleep(2)

    url = f"http://localhost:{DASHBOARD_PORT}"
    open_app_window(url)

    print(f"\n  Dashboard open at {url}")
    print("  Press Ctrl+C in this terminal to stop the bot.\n")

    # Keep the main thread alive (bot runs in background thread)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nBot stopped.")
        sys.exit(0)
