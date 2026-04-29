"""
Run this from inside a Claude Code tab to register it with the Telegram bot.
Usage: python register.py <tagname>
"""
import sys
import os
import json
import glob

SESSIONS_FILE = r"C:\Users\davei\.claude\projects\telegram\sessions.json"


def find_current_session() -> tuple[str, str] | None:
    """Find the most recently modified session for the current working directory."""
    cwd = os.getcwd()
    # Claude encodes the path by replacing :, \, /, . all with -
    encoded = cwd.replace("\\", "-").replace(":", "-").replace("/", "-").replace(".", "-").replace(" ", "-")
    projects_dir = os.path.expanduser(r"~/.claude/projects")
    project_dir = os.path.join(projects_dir, encoded)

    if not os.path.isdir(project_dir):
        # Try case-insensitive match
        for d in os.listdir(projects_dir):
            if d.lower() == encoded.lower():
                project_dir = os.path.join(projects_dir, d)
                break
        else:
            return None

    sessions = glob.glob(os.path.join(project_dir, "*.jsonl"))
    if not sessions:
        return None

    latest = max(sessions, key=os.path.getmtime)
    session_id = os.path.splitext(os.path.basename(latest))[0]
    return session_id, project_dir


def main():
    cwd = os.getcwd()

    if len(sys.argv) >= 2:
        tag = sys.argv[1].lstrip("#").lower()
    else:
        # Auto-detect: use the project folder basename as the tag
        tag = os.path.basename(cwd).lower().replace(" ", "-")

    result = find_current_session()
    if not result:
        print(f"No Claude session found for: {cwd}")
        print("Make sure you're running this from inside a Claude Code tab.")
        sys.exit(1)

    session_id, project_dir = result

    sessions = {}
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE) as f:
            sessions = json.load(f)

    sessions[tag] = {"session_id": session_id, "cwd": cwd}
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)

    print(f"Registered: #{tag} -> {session_id[:8]}... ({cwd})")


if __name__ == "__main__":
    main()
