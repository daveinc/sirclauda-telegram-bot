import os
import re
import sys
import json
import time
import subprocess
import telebot
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")
SESSIONS_FILE = "sessions.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"
HEARTBEAT_FILE = "heartbeat.json"
ACTIVE_TAB_FILE = "active_tab.json"
LOCK_FILE = "bot.lock"
TIMEOUT = 300

bot = telebot.TeleBot(TELEGRAM_TOKEN)


def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID


def load_sessions() -> dict:
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    return {}


def save_sessions(sessions: dict):
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)


def load_active_tab() -> str:
    if os.path.exists(ACTIVE_TAB_FILE):
        with open(ACTIVE_TAB_FILE) as f:
            return json.load(f).get("tab", "default")
    return "default"


def save_active_tab(tab: str):
    with open(ACTIVE_TAB_FILE, "w") as f:
        json.dump({"tab": tab}, f)


def load_subscriptions() -> set:
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE) as f:
            return set(json.load(f))
    return set()


def save_subscriptions(subs: set):
    with open(SUBSCRIPTIONS_FILE, "w") as f:
        json.dump(list(subs), f, indent=2)


def write_heartbeat(state: str, eta_seconds: int = 0):
    hb = {
        "pid": os.getpid(),
        "state": state,
        "ts": time.time(),
        "eta": time.time() + eta_seconds if eta_seconds else 0,
    }
    with open(HEARTBEAT_FILE, "w") as f:
        json.dump(hb, f)


def estimate_eta(task: str) -> int:
    """Ask Haiku to estimate task duration in seconds. Returns 300 as fallback."""
    result = subprocess.run(
        ["claude", "-p", "--bare", "--model", "haiku",
         f"Reply with a single integer — the number of seconds this task will likely take. No other text.\n\nTask: {task}"],
        capture_output=True, text=True, timeout=15
    )
    try:
        return max(30, int(result.stdout.strip()))
    except Exception:
        return 300


def ask_claude(message: str, session: dict | str | None = None) -> tuple[str, str]:
    if isinstance(session, dict):
        session_id = session.get("session_id")
        cwd = session.get("cwd")
    else:
        session_id = session
        cwd = None

    cmd = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(message)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return f"Error: {detail}", session_id

    if not result.stdout.strip():
        return "Error: Claude returned empty response. Session may be busy or rate limited — try again in a moment.", session_id

    data = json.loads(result.stdout)
    return data.get("result", "").strip(), data.get("session_id", session_id)


def parse_tab(text: str) -> tuple[str | None, str]:
    if text.startswith("#"):
        parts = text[1:].split(None, 1)
        if len(parts) == 2:
            return parts[0].lower(), parts[1]
        if len(parts) == 1:
            return parts[0].lower(), ""
    return None, text


def parse_subscription_intent(text: str) -> tuple[str, str] | None:
    t = text.lower().strip()
    m = re.search(r'(unsubscribe|subscribe).*?#?(\w+)', t)
    if m:
        action = "unsubscribe" if m.group(1) == "unsubscribe" else "subscribe"
        return action, m.group(2)
    return None


def summarize(reply: str) -> str:
    result = subprocess.run(
        ["claude", "-p", "--bare", "--model", "haiku",
         f"Summarize in 10 words or less what was just done:\n\n{reply}"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return " ".join(reply.split()[:10]) + "..."


@bot.message_handler(commands=["start"])
def handle_start(message):
    if not is_allowed(message.chat.id):
        return
    bot.reply_to(message, (
        "Hey! I'm SirClauda.\n\n"
        "Send any message for the default session.\n"
        "Prefix with #name to target a specific tab:\n"
        "  #coach fix the bug\n\n"
        "Commands:\n"
        "  /tab — show active tab\n"
        "  /tab #name — switch active tab\n"
        "  /tabs — all sessions + subscription status\n"
        "  /subscriptions — list subscribed tabs\n"
        "  /clear [tab] — clear a session\n\n"
        "Subscriptions:\n"
        "  subscribe to #coach events\n"
        "  unsubscribe from #coach"
    ))


@bot.message_handler(commands=["tab"])
def handle_tab(message):
    if not is_allowed(message.chat.id):
        return
    parts = message.text.split()
    if len(parts) > 1:
        tab = parts[1].lstrip("#").lower()
        save_active_tab(tab)
        bot.reply_to(message, f"Switched to #{tab}.")
    else:
        bot.reply_to(message, f"Active tab: #{load_active_tab()}")


@bot.message_handler(commands=["tabs"])
def handle_tabs(message):
    if not is_allowed(message.chat.id):
        return
    sessions = load_sessions()
    subs = load_subscriptions()
    if not sessions:
        bot.reply_to(message, "No active sessions yet.")
        return
    lines = []
    for k, v in sessions.items():
        sid = (v["session_id"] if isinstance(v, dict) else v)[:8]
        cwd = (" @ " + v["cwd"]) if isinstance(v, dict) else ""
        sub = " [subscribed]" if k in subs else ""
        lines.append(f"  #{k}: {sid}...{cwd}{sub}")
    bot.reply_to(message, "Sessions:\n" + "\n".join(lines))


@bot.message_handler(commands=["subscriptions"])
def handle_subscriptions(message):
    if not is_allowed(message.chat.id):
        return
    subs = load_subscriptions()
    if not subs:
        bot.reply_to(message, "No active subscriptions.\nSay \"subscribe to #coach events\" to start.")
    else:
        bot.reply_to(message, "Subscribed:\n" + "\n".join(f"  #{s}" for s in sorted(subs)))


@bot.message_handler(commands=["clear"])
def handle_clear(message):
    if not is_allowed(message.chat.id):
        return
    parts = message.text.split()
    sessions = load_sessions()
    if len(parts) > 1:
        tab = parts[1].lstrip("#").lower()
        sessions.pop(tab, None)
        save_sessions(sessions)
        bot.reply_to(message, f"Session #{tab} cleared.")
    else:
        save_sessions({})
        bot.reply_to(message, "All sessions cleared.")


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if not is_allowed(message.chat.id):
        return

    intent = parse_subscription_intent(message.text)
    if intent:
        action, tab = intent
        subs = load_subscriptions()
        if action == "subscribe":
            subs.add(tab)
            save_subscriptions(subs)
            bot.reply_to(message, f"Subscribed to #{tab}. You'll get a summary each time a task completes there.")
        else:
            subs.discard(tab)
            save_subscriptions(subs)
            bot.reply_to(message, f"Unsubscribed from #{tab}.")
        return

    tab, text = parse_tab(message.text)
    if not text:
        bot.reply_to(message, "Message is empty.")
        return

    if tab:
        # Explicit tag — switch active tab
        save_active_tab(tab)
    else:
        # No tag — use current active tab
        tab = load_active_tab()
    sessions = load_sessions()
    session = sessions.get(tab)
    label = f"#{tab}" if tab != "default" else "default"

    ack = bot.reply_to(message, f"[{label}] Received - working on it...")
    bot.send_chat_action(message.chat.id, "typing")
    eta_seconds = estimate_eta(text)
    write_heartbeat("busy", eta_seconds)

    try:
        reply, new_session_id = ask_claude(text, session)

        if new_session_id:
            if isinstance(sessions.get(tab), dict):
                sessions[tab]["session_id"] = new_session_id
            else:
                sessions[tab] = new_session_id
            save_sessions(sessions)

        write_heartbeat("idle")
        bot.edit_message_text(f"[{label}] Done.", chat_id=message.chat.id, message_id=ack.message_id)
        bot.reply_to(message, f"[{label}] {reply}")

        subs = load_subscriptions()
        if tab in subs:
            bot.send_message(message.chat.id, f"Summary [{label}]: {summarize(reply)}")

    except subprocess.TimeoutExpired:
        write_heartbeat("idle")
        bot.edit_message_text(f"[{label}] Timed out.", chat_id=message.chat.id, message_id=ack.message_id)
    except Exception as e:
        write_heartbeat("idle")
        bot.edit_message_text(f"[{label}] Error: {e}", chat_id=message.chat.id, message_id=ack.message_id)


def acquire_lock() -> bool:
    """Returns False if another instance is already running."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            # Check if that PID is actually alive
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            if str(pid) in result.stdout:
                print(f"Another instance already running (PID {pid}). Exiting.")
                return False
        except Exception:
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


if __name__ == "__main__":
    if not acquire_lock():
        sys.exit(1)
    import atexit
    atexit.register(release_lock)
    write_heartbeat("idle")
    print("SirClauda bot is running...")
    bot.infinity_polling()
