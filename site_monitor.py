#!/usr/bin/env python3
"""
Site Availability Monitor with Telegram Alert

Monitors a URL and sends you a Telegram message when the site becomes reachable.
Includes: HEAD->GET, status filtering, maintenance keyword filtering, confirmation retries,
cooldown, and stop conditions (--until / --stop-after).
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta
from typing import Optional

try:
    import requests
except ImportError:
    print("Please install requests: pip install requests")
    sys.exit(1)

# =============================================================================
# Defaults
# =============================================================================
DEFAULT_URL = "https://sn-apogee-prod.univ-cotedazur.fr/IPwebso/loginInscription.jsf"
DEFAULT_INTERVAL = 60
DEFAULT_TIMEOUT = 8
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 5

# Apogée sometimes returns 401 when service is up but not logged in
DEFAULT_OK_CODES = "200,302,303,401"

DOWN_KEYWORDS = [
    "maintenance",
    "indisponible",
    "unavailable",
    "service temporairement",
    "temporarily unavailable",
    "en cours de maintenance",
    "hors service",
]

# =============================================================================
# Logging
# =============================================================================
def setup_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# =============================================================================
# Site Checking
# =============================================================================
class SiteChecker:
    def __init__(self, url: str, timeout: float, ok_codes: set[int], check_content: bool = True):
        self.url = url
        self.timeout = timeout
        self.ok_codes = ok_codes
        self.check_content = check_content
        self.session = requests.Session()
        self.session.verify = True
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def _try_head(self) -> tuple[bool, Optional[requests.Response], str]:
        try:
            r = self.session.head(self.url, timeout=self.timeout, allow_redirects=False)
            if r.status_code == 405:
                return False, None, "HEAD not allowed"
            return True, r, ""
        except requests.exceptions.RequestException:
            return False, None, "HEAD failed"

    def _try_get(self) -> tuple[bool, Optional[requests.Response], str]:
        try:
            r = self.session.get(self.url, timeout=self.timeout, allow_redirects=False)
            return True, r, ""
        except requests.exceptions.Timeout:
            return False, None, "Connection timeout"
        except requests.exceptions.ConnectionError as e:
            return False, None, f"Connection error: {self._simplify_error(e)}"
        except requests.exceptions.SSLError as e:
            return False, None, f"SSL error: {self._simplify_error(e)}"
        except requests.exceptions.RequestException as e:
            return False, None, f"Request failed: {self._simplify_error(e)}"

    def _content_has_down_keywords(self, response: requests.Response) -> tuple[bool, str]:
        if not self.check_content:
            return False, ""
        try:
            text = response.text.lower()
            for kw in DOWN_KEYWORDS:
                if kw in text:
                    return True, f"Page contains '{kw}'"
        except Exception:
            pass
        return False, ""

    def check_once(self) -> tuple[bool, str, Optional[int]]:
        # HEAD first
        head_ok, head_resp, _ = self._try_head()
        if head_ok and head_resp is not None:
            code = head_resp.status_code
            if code not in self.ok_codes:
                return False, f"HTTP {code} (treated as DOWN)", code

            # Only do GET body check when needed (200/401 can have maintenance pages)
            if self.check_content and code in (200, 401):
                get_ok, get_resp, get_err = self._try_get()
                if not get_ok or get_resp is None:
                    return False, get_err, None
                bad, reason = self._content_has_down_keywords(get_resp)
                if bad:
                    return False, f"HTTP {code} but {reason}", code

            return True, f"HTTP {code}", code

        # HEAD failed -> GET
        get_ok, resp, err = self._try_get()
        if not get_ok or resp is None:
            return False, err, None

        code = resp.status_code
        if code not in self.ok_codes:
            return False, f"HTTP {code} (treated as DOWN)", code

        bad, reason = self._content_has_down_keywords(resp)
        if bad:
            return False, f"HTTP {code} but {reason}", code

        return True, f"HTTP {code}", code

    def check_with_confirmation(self, retries: int, delay: int) -> tuple[bool, str]:
        successes = 0
        attempts = 0
        max_attempts = retries * 2
        last_info = ""

        while attempts < max_attempts and successes < retries:
            attempts += 1
            ok, info, _ = self.check_once()
            last_info = info

            if ok:
                successes += 1
                logger.debug(f"Confirm {successes}/{retries} (attempt {attempts}/{max_attempts}): {info}")
            else:
                if successes:
                    logger.debug(f"Confirm reset after {successes} successes: {info}")
                successes = 0

            if successes < retries:
                time.sleep(delay)

        return (successes >= retries), last_info

    @staticmethod
    def _simplify_error(e: Exception) -> str:
        s = str(e)
        return (s[:100] + "...") if len(s) > 100 else s

# =============================================================================
# Telegram Notification
# =============================================================================
class TelegramHandler:
    def __init__(self, token: str, chat_id: str, spam_count: int = 1):
        self.token = token
        self.chat_id = chat_id
        self.spam_count = max(1, spam_count)  # Send multiple messages to wake you up
    
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def notify(self, message: str, retries: int = 3) -> bool:
        """Send Telegram message with retry logic."""
        if not self.is_configured():
            logger.error("Telegram not configured. Set TG_BOT_TOKEN and TG_CHAT_ID.")
            return False
        
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        
        success = False
        for msg_num in range(self.spam_count):
            payload = {
                "chat_id": self.chat_id,
                "text": f"🚨 SITE IS UP 🚨\n\n{message}",
                "disable_notification": False,
            }
            
            # Add urgency for spam messages
            if self.spam_count > 1:
                payload["text"] = f"🚨🚨🚨 WAKE UP ({msg_num + 1}/{self.spam_count}) 🚨🚨🚨\n\n{message}"
            
            for attempt in range(retries):
                try:
                    r = requests.post(url, json=payload, timeout=10)
                    if r.status_code == 200:
                        logger.info(f"📨 Telegram message {msg_num + 1}/{self.spam_count} sent!")
                        success = True
                        break
                    else:
                        logger.warning(f"Telegram attempt {attempt + 1}/{retries} failed: HTTP {r.status_code}")
                except Exception as e:
                    logger.warning(f"Telegram attempt {attempt + 1}/{retries} failed: {e}")
                
                if attempt < retries - 1:
                    time.sleep(2)  # Wait before retry
            
            if self.spam_count > 1 and msg_num < self.spam_count - 1:
                time.sleep(1)  # Small delay between spam messages
        
        return success

# =============================================================================
# Monitor
# =============================================================================
class SiteMonitor:
    def __init__(
        self,
        checker: SiteChecker,
        interval: int,
        retries: int,
        retry_delay: int,
        cooldown: int,
        until: Optional[str],
        stop_after: Optional[int],
        telegram: TelegramHandler,
    ):
        self.checker = checker
        self.interval = interval
        self.retries = retries
        self.retry_delay = retry_delay
        self.cooldown = cooldown
        self.telegram = telegram

        self.start_time = datetime.now()
        self.last_notification: Optional[datetime] = None
        self.stop_after_minutes = stop_after
        self.stop_at_time = self._parse_until(until)

    def _parse_until(self, until: Optional[str]) -> Optional[datetime]:
        if not until:
            return None
        try:
            hh, mm = map(int, until.split(":"))
            now = datetime.now()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        except Exception:
            logger.warning("Invalid --until format. Use HH:MM (e.g., 10:00).")
            return None

    def _should_stop(self) -> tuple[bool, str]:
        now = datetime.now()
        if self.stop_after_minutes is not None:
            elapsed = (now - self.start_time).total_seconds() / 60
            if elapsed >= self.stop_after_minutes:
                return True, f"Time limit reached ({self.stop_after_minutes} minutes)"
        if self.stop_at_time is not None and now >= self.stop_at_time:
            return True, f"Stop time reached ({self.stop_at_time.strftime('%H:%M')})"
        return False, ""

    def run(self) -> int:
        last_state = None
        check_count = 0

        logger.info(f"🔍 Monitoring: {self.checker.url}")
        logger.info(f"⏱️ Interval={self.interval}s Timeout={self.checker.timeout}s Retries={self.retries} Cooldown={self.cooldown}s")
        if self.stop_at_time:
            logger.info(f"⏰ Will stop at {self.stop_at_time.strftime('%H:%M')}")
        if self.stop_after_minutes is not None:
            logger.info(f"⏰ Will stop after {self.stop_after_minutes} minutes")

        try:
            while True:
                stop, reason = self._should_stop()
                if stop:
                    logger.info(f"⏰ {reason}. Stopping.")
                    return 1

                check_count += 1
                ok, info, _ = self.checker.check_once()
                state = "UP" if ok else "DOWN"

                if state != last_state:
                    logger.info(f"{'🟢' if ok else '🔴'} State changed: {state} ({info})")
                    last_state = state
                else:
                    logger.debug(f"Check #{check_count}: {state} ({info})")

                if ok:
                    logger.info("Site appears UP. Confirming...")
                    confirmed, cinfo = self.checker.check_with_confirmation(self.retries, self.retry_delay)
                    if confirmed:
                        now = datetime.now()
                        if self.last_notification is None or (now - self.last_notification).total_seconds() >= self.cooldown:
                            msg = f"The site is now reachable.\n{self.checker.url}\n({cinfo})"
                            self.telegram.notify(msg)
                            self.last_notification = now
                            return 0
                        else:
                            remaining = self.cooldown - (now - self.last_notification).total_seconds()
                            logger.info(f"Skipping notify (cooldown {remaining:.0f}s remaining)")

                time.sleep(self.interval)

        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            return 1

# =============================================================================
# CLI
# =============================================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Monitor a site and notify via Telegram when it becomes available.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  TG_BOT_TOKEN    Your Telegram bot token (from @BotFather)
  TG_CHAT_ID      Your Telegram chat ID (from @userinfobot)
  CHECK_URL       URL to monitor (optional, overrides default)

Examples:
  python site_monitor.py --test                    # Check site once
  python site_monitor.py --test-telegram           # Test Telegram works
  python site_monitor.py --interval 45 --until 10:00
  python site_monitor.py --spam 3 --interval 30   # Send 3 messages when UP

Recommended overnight:
  python site_monitor.py --interval 45 --retries 3 --cooldown 600 --until 10:00
"""
    )
    p.add_argument("--url", default=os.getenv("CHECK_URL", DEFAULT_URL), help="URL to monitor")
    p.add_argument("--interval", type=int, default=int(os.getenv("INTERVAL", DEFAULT_INTERVAL)), help="Seconds between checks")
    p.add_argument("--timeout", type=float, default=float(os.getenv("TIMEOUT", DEFAULT_TIMEOUT)), help="HTTP timeout")
    p.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help="Confirmation retries before alerting")
    p.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY, help="Seconds between confirmation retries")
    p.add_argument("--cooldown", type=int, default=600, help="Seconds between notifications (default: 600)")
    p.add_argument("--until", type=str, default=None, metavar="HH:MM", help="Stop at specific time (e.g., 10:00)")
    p.add_argument("--stop-after", type=int, default=None, metavar="MINUTES", help="Stop after N minutes")
    p.add_argument("--ok-codes", type=str, default=os.getenv("OK_CODES", DEFAULT_OK_CODES), help="HTTP codes considered UP")
    p.add_argument("--no-content-check", action="store_true", help="Skip maintenance keyword check")
    p.add_argument("--spam", type=int, default=1, metavar="N", help="Send N messages when site is UP (to wake you up)")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.add_argument("--test", action="store_true", help="Check site once and exit")
    p.add_argument("--test-telegram", action="store_true", help="Send a test Telegram message and exit")
    return p.parse_args()

def main():
    global logger
    args = parse_args()
    logger = setup_logging(args.verbose)

    try:
        ok_codes = {int(x.strip()) for x in args.ok_codes.split(",")}
    except ValueError:
        logger.error("Invalid --ok-codes format. Example: 200,302,303,401")
        return 2

    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    
    telegram = TelegramHandler(token=token, chat_id=chat_id, spam_count=args.spam)

    # Test Telegram
    if args.test_telegram:
        if not telegram.is_configured():
            logger.error("❌ Telegram not configured. Set TG_BOT_TOKEN and TG_CHAT_ID.")
            return 1
        if telegram.notify("✅ Telegram is configured correctly!"):
            logger.info("✅ Test message sent successfully!")
            return 0
        return 1

    checker = SiteChecker(
        url=args.url,
        timeout=args.timeout,
        ok_codes=ok_codes,
        check_content=not args.no_content_check,
    )
    
    # Test mode: single check
    if args.test:
        logger.info("🧪 Test mode: checking site once")
        ok, info, code = checker.check_once()
        if ok:
            logger.info(f"✅ Site is UP: {info}")
        else:
            logger.warning(f"❌ Site is DOWN: {info}")
        return 0 if ok else 1
    
    # Check Telegram is configured before starting monitor
    if not telegram.is_configured():
        logger.error("❌ Telegram not configured. Set TG_BOT_TOKEN and TG_CHAT_ID environment variables.")
        logger.info("See: https://core.telegram.org/bots#creating-a-new-bot")
        return 1

    monitor = SiteMonitor(
        checker=checker,
        interval=args.interval,
        retries=args.retries,
        retry_delay=args.retry_delay,
        cooldown=args.cooldown,
        until=args.until,
        stop_after=args.stop_after,
        telegram=telegram,
    )

    return monitor.run()

if __name__ == "__main__":
    raise SystemExit(main())
