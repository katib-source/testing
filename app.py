"""
Flask app for Render deployment.
Exposes HTTP endpoints and runs site_monitor in background thread.
"""

import os
import threading
from flask import Flask

from site_monitor import SiteChecker, SiteMonitor, TelegramHandler, setup_logging

app = Flask(__name__)
logger = setup_logging()

_started = False
_lock = threading.Lock()
_monitor_thread = None

def run_monitor():
    """Run the monitor (called in background thread)."""
    # Read env vars (set in Render dashboard)
    token = (os.getenv("TG_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TG_CHAT_ID") or "").strip()

    # Monitor settings (configurable via Render env vars)
    url = os.getenv("CHECK_URL", "https://sn-apogee-prod.univ-cotedazur.fr/IPwebso/loginInscription.jsf")
    interval = int(os.getenv("INTERVAL", "45"))
    retries = int(os.getenv("RETRIES", "1"))
    cooldown = int(os.getenv("COOLDOWN", "600"))
    until = os.getenv("UNTIL") or None  # e.g., "08:00" or leave empty for no limit
    spam = int(os.getenv("SPAM", "3"))

    ok_codes = {200, 302, 303, 401}

    telegram = TelegramHandler(token=token, chat_id=chat_id, spam_count=spam)
    
    if not telegram.is_configured():
        logger.error("❌ Telegram not configured! Set TG_BOT_TOKEN and TG_CHAT_ID in Render environment.")
        return

    checker = SiteChecker(url=url, timeout=8, ok_codes=ok_codes, check_content=True)

    monitor = SiteMonitor(
        checker=checker,
        interval=interval,
        retries=retries,
        retry_delay=5,
        cooldown=cooldown,
        until=until,
        stop_after=None,
        telegram=telegram,
    )

    logger.info("🚀 Monitor started via Render web service")
    monitor.run()


def start_monitor_once():
    """Start the monitor thread (only once)."""
    global _started, _monitor_thread
    with _lock:
        if _started:
            return False
        _started = True

    _monitor_thread = threading.Thread(target=run_monitor, daemon=True)
    _monitor_thread.start()
    logger.info("🧵 Monitor thread started")
    return True


@app.route("/")
def root():
    """Main endpoint - starts monitor on first request."""
    just_started = start_monitor_once()
    if just_started:
        return "Monitor started! 🚀"
    return "Monitor already running ✅"


@app.route("/health")
def health():
    """Health check endpoint for UptimeRobot pings."""
    return "healthy"


@app.route("/status")
def status():
    """Check if monitor is running."""
    return {
        "monitor_started": _started,
        "monitor_alive": _monitor_thread.is_alive() if _monitor_thread else False
    }


if __name__ == "__main__":
    # For local testing
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
