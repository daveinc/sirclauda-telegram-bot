"""
Watchdog for SirClauda bot.

- Checks bot health every TIMEOUT seconds (5 min)
- Dead process -> restart
- Busy past ETA + buffer -> frozen, restart
- 3 failures within 6x TIMEOUT -> kill and alert Dave
"""
import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.parse

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("ALLOWED_CHAT_ID")
PYTHON = sys.executable
BOT_SCRIPT = os.path.join(BASE, "bot.py")
HEARTBEAT_FILE = os.path.join(BASE, "heartbeat.json")

TIMEOUT = 300          # 5 min — watchdog check interval
CRASH_WINDOW = 6 * TIMEOUT   # 30 min — window to count failures
MAX_FAILURES = 3
ETA_BUFFER = 0.25      # allow 25% over ETA before calling it frozen


def notify(text: str):
    print(f"[watchdog] {text}")
    if not TOKEN or not CHAT_ID:
        return
    try:
        params = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": f"[watchdog] {text}"})
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage?{params}", timeout=10
        )
    except Exception:
        pass


def read_heartbeat() -> dict | None:
    try:
        with open(HEARTBEAT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def process_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def kill_process(pid: int):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    except Exception:
        pass


def find_existing_bot() -> int | None:
    """Return PID of already-running bot.py, or None."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe' and commandline like '%bot.py%'", "get", "processid"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def start_bot() -> int:
    existing = find_existing_bot()
    if existing:
        notify(f"Bot already running (PID {existing}), not spawning a new one.")
        return existing
    proc = subprocess.Popen(
        [PYTHON, BOT_SCRIPT],
        cwd=BASE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    notify(f"Bot started (PID {proc.pid}).")
    return proc.pid


def main():
    notify("Watchdog started.")
    failures: list[float] = []
    current_pid = start_bot()

    while True:
        time.sleep(TIMEOUT)
        now = time.time()

        # Prune old failures outside the window
        failures = [t for t in failures if now - t < CRASH_WINDOW]

        hb = read_heartbeat()
        pid = hb.get("pid", current_pid) if hb else current_pid

        # --- Check 1: process alive? ---
        if not process_alive(pid):
            failures.append(now)
            if len(failures) >= MAX_FAILURES:
                notify(f"Bot died {MAX_FAILURES} times in {CRASH_WINDOW // 60} min. Giving up — fix me.")
                return
            notify(f"Bot dead (PID {pid}). Restarting. ({len(failures)}/{MAX_FAILURES})")
            current_pid = start_bot()
            continue

        # --- Check 2: frozen? (busy past ETA + buffer) ---
        if hb and hb.get("state") == "busy":
            eta = hb.get("eta", 0)
            if eta and now > eta * (1 + ETA_BUFFER):
                frozen_for = int(now - hb.get("ts", now))
                failures.append(now)
                if len(failures) >= MAX_FAILURES:
                    notify(f"Bot frozen {MAX_FAILURES} times in {CRASH_WINDOW // 60} min. Killing — fix me.")
                    kill_process(pid)
                    return
                notify(f"Bot frozen for {frozen_for}s (past ETA). Restarting. ({len(failures)}/{MAX_FAILURES})")
                kill_process(pid)
                time.sleep(2)
                current_pid = start_bot()


if __name__ == "__main__":
    main()
