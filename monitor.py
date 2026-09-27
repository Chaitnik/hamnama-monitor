#!/usr/bin/env python3
"""
HamNama uptime monitor.

Designed to run as a short-lived process inside a GitHub Actions job that is
triggered on a cron schedule (every 5 minutes). Each invocation:

  1. Loads persisted state from state.json (subscribers, offsets, history).
  2. Performs real HTTP checks against https://hamnama.net/api/health and
     https://hamnama.net from the GitHub-hosted runner.
  3. Updates consecutive failure/success counters and decides whether an
     outage or recovery alert needs to be sent.
  4. Processes any pending Telegram bot commands (/start, /status) received
     since the last run, using Telegram's getUpdates long-poll-free API.
  5. Sends any Telegram alerts/replies that are due.
  6. Persists the updated state back to state.json so the next Action run
     (which starts from a completely fresh checkout) can pick up where this
     one left off. The GitHub Actions workflow is responsible for committing
     state.json back to the repository.

No component of this script runs continuously; everything here executes to
completion and exits.
"""

import json
import os
import socket
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

API_HEALTH_URL = "https://hamnama.net/api/health"
HOMEPAGE_URL = "https://hamnama.net"

REQUEST_TIMEOUT = float(os.environ.get("MONITOR_TIMEOUT_SECONDS", "10"))

# How many consecutive runs must see BOTH endpoints fail before we declare
# the service DOWN and alert subscribers. Kept configurable via env var so
# it can be tuned without touching code.
FAILURE_THRESHOLD = int(os.environ.get("MONITOR_FAILURE_THRESHOLD", "2"))

# How many consecutive runs must see exactly one endpoint fail before we
# alert about a DEGRADED state (e.g. API broken but homepage fine). Low by
# default since a partial outage is worth flagging quickly.
DEGRADED_THRESHOLD = int(os.environ.get("MONITOR_DEGRADED_THRESHOLD", "1"))

# How many consecutive fully-healthy runs are required before we declare
# recovery from DOWN or DEGRADED and alert subscribers. Once an external
# check confirms the site is responding again, there's little value in
# waiting for a second confirmation -- default is a single healthy run.
RECOVERY_THRESHOLD = int(os.environ.get("MONITOR_RECOVERY_THRESHOLD", "1"))

STATE_FILE = os.environ.get("MONITOR_STATE_FILE", "state.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Max number of Telegram updates to pull per run. Comfortably higher than
# what a 5-minute window would realistically accumulate.
TELEGRAM_UPDATE_LIMIT = 50


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def now_dt():
    return datetime.now(timezone.utc)


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# State persistence
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "subscribers": [],
    "telegram_offset": 0,
    "latest_check": None,
    "previous_check": None,
    # Independent streak counters -- only one of these is non-zero at a
    # time, since each run's overall status is exactly one of the three.
    "consecutive_down": 0,
    "consecutive_degraded": 0,
    "consecutive_healthy": 0,
    # The status subscribers were last alerted about. Alerts fire only on
    # a *transition* away from this value (once the relevant streak
    # threshold is met), not on every qualifying run.
    "alert_state": "HEALTHY",
    "incident_start": None,
    "failed_checks_during_incident": 0,
    "last_successful_check": None,
    "last_failed_check": None,
    # Legacy/back-compat fields kept so old state.json files still load.
    "consecutive_failures": 0,
    "consecutive_successes": 0,
    "alert_sent": False,
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: could not read {STATE_FILE} ({e}); starting from default state", file=sys.stderr)
        return dict(DEFAULT_STATE)

    # Merge onto defaults so new fields introduced later don't break old
    # state files.
    merged = dict(DEFAULT_STATE)
    merged.update(data)
    return merged


def save_state(state):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, STATE_FILE)


# --------------------------------------------------------------------------
# HTTP health checks
# --------------------------------------------------------------------------

def classify_exception(exc):
    """Best-effort classification of a requests exception into a short,
    human-readable error type and message, without guessing root causes we
    can't actually confirm."""

    chain = []
    cur = exc
    while cur is not None:
        chain.append(cur)
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)

    text = " ".join(str(c) for c in chain)

    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error", f"TLS/SSL error: {exc}"

    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout,
                         requests.exceptions.Timeout)):
        return "timeout", f"Request timed out after {REQUEST_TIMEOUT}s: {exc}"

    if any(isinstance(c, socket.gaierror) for c in chain) or "Name or service not known" in text \
            or "nodename nor servname" in text or "getaddrinfo failed" in text:
        return "dns_error", f"DNS resolution failed: {exc}"

    if isinstance(exc, requests.exceptions.ConnectionError):
        if "Connection reset" in text or "ECONNRESET" in text:
            return "connection_reset", f"Connection reset: {exc}"
        if "Connection refused" in text or "ECONNREFUSED" in text:
            return "connection_refused", f"Connection refused: {exc}"
        return "connection_error", f"Connection error: {exc}"

    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "redirect_error", f"Too many redirects: {exc}"

    return "request_error", f"{type(exc).__name__}: {exc}"


def check_endpoint(url, allow_redirects=True, parse_json=False):
    """Perform a real HTTP GET against url and return a structured result.

    A successful TCP connection alone is never treated as healthy -- only an
    actual HTTP 2xx response counts.
    """

    result = {
        "url": url,
        "success": False,
        "status_code": None,
        "status_text": None,
        "final_url": None,
        "response_time_ms": None,
        "redirect_count": 0,
        "redirect_history": [],
        "error_type": None,
        "error": None,
        "json_summary": None,
    }

    start = time.monotonic()
    try:
        resp = requests.get(
            url,
            timeout=(REQUEST_TIMEOUT, REQUEST_TIMEOUT),
            allow_redirects=allow_redirects,
            headers={"User-Agent": "hamnama-uptime-monitor/1.0 (+github-actions)"},
        )
        elapsed_ms = (time.monotonic() - start) * 1000.0

        result["response_time_ms"] = round(elapsed_ms, 1)
        result["status_code"] = resp.status_code
        result["status_text"] = resp.reason
        result["final_url"] = resp.url
        result["redirect_count"] = len(resp.history)
        result["redirect_history"] = [r.url for r in resp.history]
        result["success"] = 200 <= resp.status_code < 300

        if not result["success"]:
            result["error_type"] = "http_error"
            result["error"] = f"HTTP {resp.status_code} {resp.reason or ''}".strip()

        if parse_json and resp.content:
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type.lower():
                try:
                    body = resp.json()
                    # Don't assume a schema. Just record a bounded summary:
                    # top-level keys for dicts, length for lists, or the raw
                    # value (truncated) for scalars.
                    if isinstance(body, dict):
                        result["json_summary"] = {
                            "type": "object",
                            "keys": list(body.keys())[:25],
                            "raw": _truncate_json(body),
                        }
                    elif isinstance(body, list):
                        result["json_summary"] = {
                            "type": "array",
                            "length": len(body),
                            "raw": _truncate_json(body),
                        }
                    else:
                        result["json_summary"] = {"type": type(body).__name__, "raw": body}
                except ValueError as e:
                    result["json_summary"] = {"parse_error": str(e)}

    except requests.exceptions.RequestException as e:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        result["response_time_ms"] = round(elapsed_ms, 1)
        error_type, message = classify_exception(e)
        result["error_type"] = error_type
        result["error"] = message
    except Exception as e:  # noqa: BLE001 - last-resort safety net per endpoint
        elapsed_ms = (time.monotonic() - start) * 1000.0
        result["response_time_ms"] = round(elapsed_ms, 1)
        result["error_type"] = "unexpected_error"
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"Unexpected error checking {url}:\n{traceback.format_exc()}", file=sys.stderr)

    return result


def _truncate_json(value, max_chars=800):
    try:
        s = json.dumps(value)
    except (TypeError, ValueError):
        return None
    if len(s) > max_chars:
        return s[:max_chars] + "...(truncated)"
    return s


def run_checks():
    api_result = check_endpoint(API_HEALTH_URL, allow_redirects=True, parse_json=True)
    home_result = check_endpoint(HOMEPAGE_URL, allow_redirects=True, parse_json=False)

    if api_result["success"] and home_result["success"]:
        overall = "HEALTHY"
    elif api_result["success"] or home_result["success"]:
        overall = "DEGRADED"
    else:
        overall = "DOWN"

    return {
        "timestamp": now_iso(),
        "overall": overall,
        "api": api_result,
        "homepage": home_result,
    }


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram_request(method, params=None, timeout=15):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    url = f"{TELEGRAM_API_BASE}/{method}"
    resp = requests.post(url, json=params or {}, timeout=timeout)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data["result"]


def telegram_get_updates(offset):
    return telegram_request("getUpdates", {
        "offset": offset,
        "timeout": 0,
        "limit": TELEGRAM_UPDATE_LIMIT,
        "allowed_updates": ["message"],
    })


def telegram_send_message(chat_id, text):
    return telegram_request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    })


def safe_send_to_subscriber(chat_id, text):
    """Send a message to one subscriber. Never raises -- a blocked or
    unreachable user must not crash the monitor or stop other sends."""
    try:
        telegram_send_message(chat_id, text)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: failed to send Telegram message to {chat_id}: {e}", file=sys.stderr)
        return False


def broadcast(state, text):
    for chat_id in list(state["subscribers"]):
        safe_send_to_subscriber(chat_id, text)


def extract_command(text):
    """Return the bare command (lowercase, no leading slash, no @botname
    suffix) from a message text, or None if it isn't a command."""
    if not text or not text.startswith("/"):
        return None
    first_token = text.strip().split()[0]
    command = first_token[1:].split("@")[0].lower()
    return command or None


def process_telegram_updates(state):
    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN not set; skipping Telegram command processing", file=sys.stderr)
        return

    try:
        updates = telegram_get_updates(state["telegram_offset"])
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: failed to fetch Telegram updates: {e}", file=sys.stderr)
        return

    max_update_id = state["telegram_offset"] - 1

    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None and update_id > max_update_id:
            max_update_id = update_id

        message = update.get("message")
        if not message:
            continue

        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "")
        if chat_id is None:
            continue

        command = extract_command(text)
        if command == "start":
            handle_start(state, chat_id)
        elif command == "status":
            handle_status(state, chat_id)
        # Unknown commands/plain messages are silently ignored; this bot
        # intentionally supports only /start and /status.

    if max_update_id >= state["telegram_offset"]:
        state["telegram_offset"] = max_update_id + 1


def handle_start(state, chat_id):
    if chat_id in state["subscribers"]:
        safe_send_to_subscriber(
            chat_id,
            "You're already subscribed to HamNama uptime alerts. "
            "You'll be notified here if the service goes down or recovers.",
        )
        return

    state["subscribers"].append(chat_id)
    welcome = (
        "✅ Subscribed to HamNama uptime alerts.\n\n"
        "I check https://hamnama.net and its /api/health endpoint every "
        "5 minutes from GitHub Actions. You'll get a message here if the "
        "service goes down or degrades, and another when it recovers. "
        "Commands are only processed on the ~5-minute check cycle, so "
        "replies may take a few minutes.\n\n"
        "Send /status any time for the latest health report.\n\n"
        "Current status:\n" + format_status_message(state)
    )
    safe_send_to_subscriber(chat_id, welcome)


def handle_status(state, chat_id):
    safe_send_to_subscriber(chat_id, format_status_message(state))


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------

def _endpoint_lines(label, result):
    icon = "✅" if result["success"] else "❌"
    lines = [f"{label}:", f"{icon} {result['url']}"]

    if result["status_code"] is not None:
        status_text = f"{result['status_code']}"
        if result.get("status_text"):
            status_text += f" {result['status_text']}"
        lines.append(f"HTTP: {status_text}")
    else:
        lines.append("HTTP: (no response)")

    if result.get("final_url") and result["final_url"] != result["url"]:
        lines.append(f"Final URL: {result['final_url']}")

    if result["response_time_ms"] is not None:
        lines.append(f"Response time: {result['response_time_ms']:.0f} ms")

    if result.get("redirect_count"):
        lines.append(f"Redirects: {result['redirect_count']}")

    if result.get("error"):
        lines.append(f"Error: {result['error']}")

    return lines


def format_status_message(state):
    check = state.get("latest_check")
    if not check:
        return "ℹ️ No monitoring data is available yet. The first check runs within 5 minutes of setup."

    overall = check["overall"]
    icon = {"HEALTHY": "🟢", "DEGRADED": "🟡", "DOWN": "🔴"}.get(overall, "⚪")

    lines = [f"{icon} HAMNAMA {overall}", "", f"Last check: {check['timestamp']}", ""]
    lines += _endpoint_lines("API", check["api"])
    lines.append("")
    lines += _endpoint_lines("Website", check["homepage"])
    lines.append("")
    streak = {"DOWN": state["consecutive_down"], "DEGRADED": state["consecutive_degraded"]}.get(overall, 0)
    lines.append(f"Consecutive failed checks: {streak}")

    if overall != "HEALTHY" and state.get("incident_start"):
        lines.append(f"Incident started: {state['incident_start']}")
        downtime = _format_duration_since(state["incident_start"])
        if downtime:
            lines.append(f"Approximate downtime: {downtime}")

    lines.append(f"Last successful check: {state.get('last_successful_check') or 'never'}")
    lines.append(f"Last failed check: {state.get('last_failed_check') or 'never'}")

    return "\n".join(lines)


def format_down_alert(state):
    check = state["latest_check"]
    lines = [
        "🔴 HAMNAMA IS DOWN",
        "",
        f"Incident started:\n{state['incident_start']}",
        "",
        f"Overall status:\n{check['overall']}",
        "",
    ]
    lines += _endpoint_lines("API", check["api"])
    lines.append("")
    lines += _endpoint_lines("Website", check["homepage"])
    lines.append("")
    lines.append(f"Consecutive failed checks: {state['consecutive_down']}")
    lines.append(f"Last successful check: {state.get('last_successful_check') or 'unknown'}")
    lines.append("")
    lines.append("Possible failure:")
    lines.append(_summarize_failure(check))
    return "\n".join(lines)


def format_degraded_alert(state):
    check = state["latest_check"]
    lines = [
        "🟡 HAMNAMA DEGRADED",
        "",
        f"Since:\n{state.get('incident_start') or check['timestamp']}",
        "",
    ]
    lines += _endpoint_lines("API", check["api"])
    lines.append("")
    lines += _endpoint_lines("Website", check["homepage"])
    lines.append("")

    if not check["api"]["success"]:
        lines.append("The website is reachable, but the API health endpoint is failing.")
    else:
        lines.append("The API is healthy, but the website itself is failing.")

    lines.append("")
    lines.append(f"Consecutive degraded checks: {state['consecutive_degraded']}")
    lines.append(f"Last fully healthy check: {state.get('last_successful_check') or 'unknown'}")
    return "\n".join(lines)


def format_recovery_alert(state):
    check = state["latest_check"]
    lines = [
        "🟢 HAMNAMA IS BACK UP",
        "",
        f"Recovered:\n{check['timestamp']}",
        "",
        f"Estimated downtime:\n{_format_duration_since(state.get('incident_start'), until=check['timestamp']) or 'unknown'}",
        "",
    ]
    lines += _endpoint_lines("API", check["api"])
    lines.append("")
    lines += _endpoint_lines("Website", check["homepage"])
    lines.append("")
    lines.append(f"Failed checks during incident: {state.get('failed_checks_during_incident', 0)}")
    lines.append(f"Last successful check before incident:\n{state.get('last_successful_check') or 'unknown'}")
    return "\n".join(lines)


def _summarize_failure(check):
    """Describe only what was actually observed -- never guess a root
    cause we have no evidence for."""
    parts = []
    for label, result in (("API endpoint", check["api"]), ("Website", check["homepage"])):
        if not result["success"]:
            reason = result.get("error") or "request did not succeed"
            parts.append(f"{label} failed: {reason}")
    if not parts:
        return "Both endpoints reported failures on the last check."
    return "\n".join(parts)


def _format_duration_since(start_iso, until=None):
    start = parse_iso(start_iso)
    if not start:
        return None
    end = parse_iso(until) if until else now_dt()
    if not end:
        end = now_dt()
    seconds = max(0, int((end - start).total_seconds()))
    minutes = seconds // 60
    if minutes < 1:
        return f"~{seconds} seconds"
    if minutes < 60:
        return f"~{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"~{hours}h {remaining_minutes}m"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def update_state_with_check(state, check):
    """Update streak counters and incident bookkeeping for the latest check.

    Each run's overall status is exactly one of HEALTHY / DEGRADED / DOWN,
    so exactly one streak counter increments and the other two reset to
    zero -- there's no attempt to treat DEGRADED as "partial success"
    toward a DOWN recovery, or vice versa. Alert *decisions* live in
    decide_and_send_alerts(); this function only tracks state.
    """
    state["previous_check"] = state.get("latest_check")
    state["latest_check"] = check
    overall = check["overall"]

    if overall == "HEALTHY":
        state["consecutive_healthy"] += 1
        state["consecutive_degraded"] = 0
        state["consecutive_down"] = 0
        state["last_successful_check"] = check["timestamp"]
    elif overall == "DEGRADED":
        state["consecutive_degraded"] += 1
        state["consecutive_healthy"] = 0
        state["consecutive_down"] = 0
        state["last_failed_check"] = check["timestamp"]
    else:  # DOWN
        state["consecutive_down"] += 1
        state["consecutive_healthy"] = 0
        state["consecutive_degraded"] = 0
        state["last_failed_check"] = check["timestamp"]

    if overall != "HEALTHY":
        if state.get("incident_start") is None:
            # Tentatively mark when trouble started. If it heals before any
            # threshold is crossed, this is cleared below without alerting.
            state["incident_start"] = check["timestamp"]
        state["failed_checks_during_incident"] = state.get("failed_checks_during_incident", 0) + 1
    elif state.get("alert_state", "HEALTHY") == "HEALTHY":
        # We're healthy now and weren't mid-alerted-incident, so any
        # tentative incident_start was just a blip -- clear it.
        state["incident_start"] = None
        state["failed_checks_during_incident"] = 0


def decide_and_send_alerts(state):
    """Send a Telegram alert exactly on a confirmed transition away from
    the status subscribers were last alerted about (`alert_state`)."""
    check = state["latest_check"]
    overall = check["overall"]
    alert_state = state.get("alert_state", "HEALTHY")

    if overall == alert_state:
        return  # No transition to report.

    if overall == "DOWN" and state["consecutive_down"] >= FAILURE_THRESHOLD:
        broadcast(state, format_down_alert(state))
        state["alert_state"] = "DOWN"

    elif overall == "DEGRADED" and state["consecutive_degraded"] >= DEGRADED_THRESHOLD:
        broadcast(state, format_degraded_alert(state))
        state["alert_state"] = "DEGRADED"

    elif overall == "HEALTHY" and state["consecutive_healthy"] >= RECOVERY_THRESHOLD:
        broadcast(state, format_recovery_alert(state))
        state["alert_state"] = "HEALTHY"
        state["incident_start"] = None
        state["failed_checks_during_incident"] = 0


def main():
    state = load_state()

    check = run_checks()
    update_state_with_check(state, check)
    decide_and_send_alerts(state)

    try:
        process_telegram_updates(state)
    except Exception:  # noqa: BLE001 - Telegram problems must never fail the run
        print(f"WARNING: unhandled error while processing Telegram updates:\n{traceback.format_exc()}",
              file=sys.stderr)

    save_state(state)

    print(f"[{check['timestamp']}] overall={check['overall']} "
          f"api={check['api']['status_code']}({check['api']['error_type']}) "
          f"home={check['homepage']['status_code']}({check['homepage']['error_type']}) "
          f"consecutive_down={state['consecutive_down']} "
          f"consecutive_degraded={state['consecutive_degraded']} "
          f"alert_state={state['alert_state']} "
          f"subscribers={len(state['subscribers'])}")


if __name__ == "__main__":
    main()
