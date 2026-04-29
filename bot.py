import os
import re
import sys
import json
import time
import queue as queue_module
import subprocess
import threading
import telebot
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")
SESSIONS_FILE = "sessions.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"
CONVERSATIONS_FILE = "conversations.json"
HEARTBEAT_FILE = "heartbeat.json"
ACTIVE_TAB_FILE = "active_tab.json"
LOCK_FILE = "bot.lock"
TIMEOUT = 300
HISTORY_MAX = 10   # pairs stored per tab
HISTORY_INJECT = 5 # pairs prepended as context

bot = telebot.TeleBot(TELEGRAM_TOKEN)
_sessions_lock = threading.Lock()
_conversations_lock = threading.Lock()
_tab_queues: dict[str, queue_module.Queue] = {}
_tab_workers: dict[str, threading.Thread] = {}
_workers_lock = threading.Lock()


# ── I/O helpers ──────────────────────────────────────────────────────────────

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


def load_conversations() -> dict:
    if os.path.exists(CONVERSATIONS_FILE):
        with open(CONVERSATIONS_FILE) as f:
            return json.load(f)
    return {}


def save_conversations(convs: dict):
    with open(CONVERSATIONS_FILE, "w") as f:
        json.dump(convs, f, indent=2)


def update_conversation(tab: str, user_msg: str, assistant_reply: str):
    with _conversations_lock:
        convs = load_conversations()
        history = convs.get(tab, [])
        history.append({"user": user_msg, "assistant": assistant_reply})
        convs[tab] = history[-HISTORY_MAX:]
        save_conversations(convs)


def build_context_message(tab: str, text: str) -> str:
    with _conversations_lock:
        convs = load_conversations()
    history = convs.get(tab, [])[-HISTORY_INJECT:]
    if not history:
        return text
    lines = ["[Previous conversation context:]"]
    for pair in history:
        lines.append(f"User: {pair['user'][:800]}")
        lines.append(f"Assistant: {pair['assistant'][:800]}")
    lines.append("")
    lines.append(text)
    return "\n".join(lines)


def write_heartbeat(state: str, eta_seconds: int = 0):
    hb = {
        "pid": os.getpid(),
        "state": state,
        "ts": time.time(),
        "eta": time.time() + eta_seconds if eta_seconds else 0,
    }
    with open(HEARTBEAT_FILE, "w") as f:
        json.dump(hb, f)


def read_heartbeat() -> dict | None:
    try:
        with open(HEARTBEAT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ── Claude helpers ────────────────────────────────────────────────────────────

def estimate_eta(task: str) -> int:
    result = subprocess.run(
        ["claude", "-p", "--bare", "--model", "haiku",
         f"Reply with a single integer — the number of seconds this task will likely take. No other text.\n\nTask: {task}"],
        capture_output=True, text=True, encoding="utf-8", timeout=15
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
    cmd += ["--", message]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return f"Error: {detail}", session_id

    if not result.stdout.strip():
        return "Error: Claude returned empty response. Session may be busy or rate limited — try again in a moment.", session_id

    data = json.loads(result.stdout)
    return data.get("result", "").strip(), data.get("session_id", session_id)


def summarize(reply: str) -> str:
    result = subprocess.run(
        ["claude", "-p", "--bare", "--model", "haiku",
         f"Summarize in 10 words or less what was just done:\n\n{reply}"],
        capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return " ".join(reply.split()[:10]) + "..."


# ── Telegram helpers ──────────────────────────────────────────────────────────

def send_chunked(chat_id: int, reply_to_id: int, text: str, chunk_size: int = 4000):
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    for i, chunk in enumerate(chunks):
        suffix = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        if i == 0:
            bot.send_message(chat_id, chunk + suffix, reply_to_message_id=reply_to_id)
        else:
            bot.send_message(chat_id, chunk + suffix)


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


# ── Per-tab message queue ─────────────────────────────────────────────────────

def get_queue(tab: str) -> queue_module.Queue:
    with _workers_lock:
        if tab not in _tab_queues:
            _tab_queues[tab] = queue_module.Queue()
            t = threading.Thread(target=worker_loop, args=(tab,), daemon=True)
            _tab_workers[tab] = t
            t.start()
        return _tab_queues[tab]


def worker_loop(tab: str):
    while True:
        job = _tab_queues[tab].get()
        process_job(tab, job)
        _tab_queues[tab].task_done()


def process_job(tab: str, job: dict):
    message = job["message"]
    text = job["text"]
    ack = job["ack"]
    label = job["label"]

    with _sessions_lock:
        sessions = load_sessions()
    session = sessions.get(tab)

    eta_seconds = estimate_eta(text)
    write_heartbeat("busy", eta_seconds)

    try:
        full_message = build_context_message(tab, text)
        reply, new_session_id = ask_claude(full_message, session)

        if new_session_id:
            with _sessions_lock:
                sessions = load_sessions()
                if isinstance(sessions.get(tab), dict):
                    sessions[tab]["session_id"] = new_session_id
                else:
                    sessions[tab] = new_session_id
                save_sessions(sessions)

        update_conversation(tab, text, reply)

        write_heartbeat("idle")
        bot.edit_message_text(f"[{label}] Done.", chat_id=message.chat.id, message_id=ack.message_id)
        send_chunked(message.chat.id, message.message_id, f"[{label}] {reply}")

        subs = load_subscriptions()
        if tab in subs:
            bot.send_message(message.chat.id, f"Summary [{label}]: {summarize(reply)}")

    except subprocess.TimeoutExpired:
        write_heartbeat("idle")
        bot.edit_message_text(f"[{label}] Timed out.", chat_id=message.chat.id, message_id=ack.message_id)
    except Exception as e:
        write_heartbeat("idle")
        bot.edit_message_text(f"[{label}] Error: {e}", chat_id=message.chat.id, message_id=ack.message_id)


# ── Command handlers ──────────────────────────────────────────────────────────

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
        "  /status — live state of each tab\n"
        "  /history [tab] [n] — last N exchanges (default 5)\n"
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


@bot.message_handler(commands=["status"])
def handle_status(message):
    if not is_allowed(message.chat.id):
        return
    sessions = load_sessions()
    hb = read_heartbeat()
    active = load_active_tab()

    if not sessions:
        bot.reply_to(message, f"No active sessions.\nActive tab: #{active}")
        return

    lines = []
    for tab, v in sessions.items():
        cwd = v.get("cwd", "unknown") if isinstance(v, dict) else "unknown"
        project = os.path.basename(cwd) if cwd != "unknown" else "unknown"
        q_depth = _tab_queues[tab].qsize() if tab in _tab_queues else 0

        if hb and hb.get("state") == "busy":
            eta_in = max(0, int(hb.get("eta", 0) - time.time()))
            state = f"busy (ETA ~{eta_in}s)"
        else:
            state = "idle"

        if q_depth > 0:
            state += f", {q_depth} queued"

        star = " *" if tab == active else ""
        lines.append(f"#{tab} [{project}]: {state}{star}")

    bot.reply_to(message, "Status:\n" + "\n".join(lines) + "\n\n* = active tab")


@bot.message_handler(commands=["history"])
def handle_history(message):
    if not is_allowed(message.chat.id):
        return
    parts = message.text.split()
    tab = parts[1].lstrip("#").lower() if len(parts) > 1 else load_active_tab()
    try:
        n = int(parts[2]) if len(parts) > 2 else 5
    except ValueError:
        n = 5

    sessions = load_sessions()
    session = sessions.get(tab)
    if not session:
        bot.reply_to(message, f"No session for #{tab}.")
        return

    session_id = session.get("session_id") if isinstance(session, dict) else session
    projects_dir = os.path.expanduser("~/.claude/projects")
    jsonl_path = None
    for root, _dirs, files in os.walk(projects_dir):
        for fname in files:
            if fname == f"{session_id}.jsonl":
                jsonl_path = os.path.join(root, fname)
                break
        if jsonl_path:
            break

    if not jsonl_path:
        bot.reply_to(message, f"Session file not found for #{tab}.")
        return

    entries = []
    with open(jsonl_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("type") in ("user", "assistant"):
                    entries.append(entry)
            except Exception:
                continue

    last = entries[-(n * 2):]
    lines = []
    for e in last:
        role = "You" if e.get("type") == "user" else "Claude"
        content = e.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        lines.append(f"{role}: {str(content)[:300]}")

    if not lines:
        bot.reply_to(message, f"No messages found in #{tab} history.")
        return

    send_chunked(
        message.chat.id,
        message.message_id,
        f"History [#{tab}] (last {n}):\n\n" + "\n\n".join(lines),
    )


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
        with _conversations_lock:
            convs = load_conversations()
            convs.pop(tab, None)
            save_conversations(convs)
        bot.reply_to(message, f"Session #{tab} cleared.")
    else:
        save_sessions({})
        with _conversations_lock:
            save_conversations({})
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
        save_active_tab(tab)
    else:
        tab = load_active_tab()

    label = f"#{tab}" if tab != "default" else "default"
    q = get_queue(tab)
    depth = q.qsize()

    if depth > 0:
        ack = bot.reply_to(message, f"[{label}] Received - queued (position {depth + 1})")
    else:
        ack = bot.reply_to(message, f"[{label}] Received - working on it...")

    bot.send_chat_action(message.chat.id, "typing")
    q.put({"message": message, "text": text, "tab": tab, "label": label, "ack": ack})


# ── Process lock ──────────────────────────────────────────────────────────────

def acquire_lock() -> bool:
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
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
