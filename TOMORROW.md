# Build Plan — SirClauda Bot Features

When Dave says "good morning, keep building what we discussed", implement all features below
in order. After each feature: kill the watchdog+bot, restart, verify it works, then move on.

Kill command:   wmic process where "commandline like '%bot.py%' or commandline like '%watchdog%'" delete
Start command:  cd "C:\Users\davei\.claude\projects\telegram" && python watchdog.py (background)
Verify:         wmic process where "commandline like '%bot.py%' or commandline like '%watchdog%'" get processid,commandline

---

## 1. Response chunking

**Problem:** Telegram has a 4096 char message limit. Long Claude responses are silently cut off.

**Fix:** In `bot.py`, replace all `bot.reply_to(message, f"[{label}] {reply}")` with a chunked sender:

```python
def send_chunked(chat_id: int, reply_to_id: int, text: str, chunk_size: int = 4000):
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    for i, chunk in enumerate(chunks):
        suffix = f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""
        if i == 0:
            bot.reply_to_message(chat_id, reply_to_id, chunk + suffix)
        else:
            bot.send_message(chat_id, chunk + suffix)
```

Use `bot.reply_to` for first chunk, `bot.send_message` for subsequent ones.

---

## 2. Message queuing

**Problem:** If Dave sends multiple messages while a tab is busy, they collide — second message
fires a new `claude -p` before the first finishes.

**Fix:** Use `queue.Queue` per tab. Each tab gets a worker thread that processes messages one
by one. Main handler just enqueues, thread dequeues and calls `ask_claude`.

```python
import threading
import queue

tab_queues: dict[str, queue.Queue] = {}
tab_workers: dict[str, threading.Thread] = {}

def get_worker(tab: str):
    if tab not in tab_queues:
        tab_queues[tab] = queue.Queue()
        t = threading.Thread(target=worker_loop, args=(tab,), daemon=True)
        tab_workers[tab] = t
        t.start()
    return tab_queues[tab]

def worker_loop(tab: str):
    while True:
        job = tab_queues[tab].get()
        process_job(tab, job)  # contains the ask_claude + reply logic
        tab_queues[tab].task_done()
```

Enqueue in `handle_message` instead of calling `ask_claude` directly.
ACK message should say "Received - queued (position N)" if queue depth > 0.

---

## 3. /status command

**Problem:** No way to see what each tab is currently doing remotely.

**Fix:** Add `/status` handler that reads `heartbeat.json` and `sessions.json`:

```python
@bot.message_handler(commands=["status"])
def handle_status(message):
    sessions = load_sessions()
    hb = read_heartbeat()  # already exists in watchdog, move to shared utils or re-read in bot
    lines = []
    for tab, v in sessions.items():
        cwd = v.get("cwd", "unknown") if isinstance(v, dict) else "unknown"
        project = os.path.basename(cwd)
        if hb and hb.get("state") == "busy":
            eta_in = max(0, int(hb.get("eta", 0) - time.time()))
            state = f"busy (ETA ~{eta_in}s)"
        else:
            state = "idle"
        lines.append(f"#{tab} [{project}]: {state}")
    active = load_active_tab()
    bot.reply_to(message, "Status:\n" + "\n".join(lines) + f"\n\nActive tab: #{active}")
```

---

## 4. /history [tab] [n]

**Problem:** No way to review recent exchanges remotely.

**Fix:** Read the session's `.jsonl` file and extract last N user/assistant pairs.

```python
@bot.message_handler(commands=["history"])
def handle_history(message):
    parts = message.text.split()
    tab = parts[1].lstrip("#").lower() if len(parts) > 1 else load_active_tab()
    n = int(parts[2]) if len(parts) > 2 else 5

    sessions = load_sessions()
    session = sessions.get(tab)
    if not session:
        bot.reply_to(message, f"No session for #{tab}.")
        return

    session_id = session.get("session_id") if isinstance(session, dict) else session
    # Find the jsonl file
    projects_dir = os.path.expanduser("~/.claude/projects")
    jsonl_path = None
    for root, dirs, files in os.walk(projects_dir):
        for f in files:
            if f == f"{session_id}.jsonl":
                jsonl_path = os.path.join(root, f)
                break

    if not jsonl_path:
        bot.reply_to(message, "Session file not found.")
        return

    messages = []
    with open(jsonl_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("type") == "user" or entry.get("type") == "assistant":
                    messages.append(entry)
            except Exception:
                continue

    last = messages[-(n*2):]
    lines = []
    for m in last:
        role = "You" if m.get("type") == "user" else "Claude"
        content = m.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        lines.append(f"{role}: {str(content)[:200]}")

    send_chunked(message.chat.id, message.message_id, "\n\n".join(lines))
```

---

## 5. Auto-register on startup

**Problem:** Every time a new Claude Code tab opens, Dave has to manually run register.py.

**Fix:** Add a hook in `.claude/settings.json` (project-level) that runs `register.py <dirname>`
automatically when a Claude Code session starts in any project directory.

In each project's `.claude/settings.json`:
```json
{
  "hooks": {
    "PostSessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:\\Users\\davei\\.claude\\projects\\telegram\\register.py\" auto"
          }
        ]
      }
    ]
  }
}
```

The tag `auto` would be overwritten each time — or better, use the project folder name as the tag
so each project auto-registers under its own name. Update `register.py` to support being called
with no args (auto-detect tag from cwd basename).

---

## Meta note
The TOMORROW.md approach worked well here — a structured build plan that a fresh claude -p session
can pick up and execute autonomously. Worth exploring this pattern further so we can set it up
organically for all projects, not just this one. Add to the master templates as a blueprint.

---

## After all features are done

1. Update `blueprint-telegram-claude-bot.md` in master templates with all new features
2. Restart watchdog one final time
3. Send Dave a Telegram message: "All done. Here's what's new: [list]"
