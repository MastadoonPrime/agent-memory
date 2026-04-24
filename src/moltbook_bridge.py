#!/usr/bin/env python3
"""Moltbook Memory Bridge — lets Moltbook agents use Agent Memory via DMs and comments.

Many agents on Moltbook can only interact through the Moltbook API (no MCP, no direct HTTP).
This bridge polls for !memory commands in DMs and @mentions, translates them to Agent Memory
calls, and responds via the same channel.

Commands:
    !memory store <content>           — Store a private memory
    !memory store #tag1,tag2 <content> — Store with tags
    !memory recall                    — Get recent memories
    !memory recall #tag1,tag2         — Recall by tags
    !memory search <query>            — Search memories by metadata
    !memory commons                   — Browse shared knowledge
    !memory commons contribute <cat> <content> — Share knowledge publicly
    !memory stats                     — Your memory statistics
    !memory help                      — Show available commands

Identity mapping:
    Each Moltbook username gets a deterministic Agent Memory identity:
    sha256("moltbook-bridge:{username}") — consistent across sessions.

Run via cron every 2 minutes:
    */2 * * * * python3 /home/alex/new-system/agent-memory/src/moltbook_bridge.py

Or run once: python3 moltbook_bridge.py [--dry-run]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# --- Configuration ---

MOLTBOOK_API = "https://moltbook.com/api/v1"
MOLTBOOK_KEY = os.environ.get(
    "MOLTBOOK_API_KEY",
    "moltbook_sk_YsADqaayw8oCiiGH9FBl63o__V23dgs-",
)
MOLTBOOK_USERNAME = "systemadmin_sylex"

STATE_DIR = Path("/home/alex/new-system/data")
STATE_FILE = STATE_DIR / "moltbook_bridge_state.json"
LOG_FILE = Path("/home/alex/new-system/logs/moltbook-bridge.log")

CLI_PATH = Path(__file__).parent / "cli.py"

# Rate limit: max responses per run to avoid spamming
MAX_RESPONSES_PER_RUN = 5
# Don't process messages older than this (seconds)
MAX_MESSAGE_AGE = 600  # 10 minutes

DRY_RUN = "--dry-run" in sys.argv


# --- Logging ---

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --- State management ---

def load_state() -> dict:
    """Load bridge state (last processed notification ID, etc.)."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "last_notification_id": None,
            "last_dm_check": None,
            "processed_ids": [],  # last 200 message IDs to avoid re-processing
            "registered_agents": {},  # username -> agent_identifier
        }


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Keep processed_ids bounded
    state["processed_ids"] = state["processed_ids"][-200:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- Identity ---

def get_agent_identifier(username: str) -> str:
    """Deterministic Agent Memory identity for a Moltbook user."""
    return hashlib.sha256(f"moltbook-bridge:{username}".encode()).hexdigest()


# --- Moltbook API ---

def moltbook_request(method: str, path: str, data: dict | None = None) -> dict | list | None:
    """Make a Moltbook API request."""
    url = f"{MOLTBOOK_API}{path}"
    headers = {
        "Authorization": f"Bearer {MOLTBOOK_KEY}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
            if text:
                return json.loads(text)
            return None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:300]
        log(f"Moltbook API error {e.code} on {method} {path}: {body_text}")
        return None
    except Exception as e:
        log(f"Moltbook request failed: {e}")
        return None


def get_notifications() -> list:
    """Get recent notifications (mentions, comments on our posts, etc.)."""
    result = moltbook_request("GET", "/notifications")
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "notifications" in result:
        return result["notifications"]
    return []


def get_dms() -> list:
    """Get DM conversations."""
    result = moltbook_request("GET", "/agents/dm/conversations")
    if isinstance(result, dict):
        convos = result.get("conversations", {})
        if isinstance(convos, dict):
            return convos.get("items", [])
        if isinstance(convos, list):
            return convos
    if isinstance(result, list):
        return result
    return []


def post_comment(post_id: str, content: str) -> bool:
    """Reply to a post with a comment."""
    if DRY_RUN:
        log(f"[DRY RUN] Would comment on post {post_id}: {content[:80]}...")
        return True
    result = moltbook_request("POST", f"/posts/{post_id}/comments", {"content": content})
    return result is not None


def send_dm(username: str, content: str) -> bool:
    """Send a DM to a user."""
    if DRY_RUN:
        log(f"[DRY RUN] Would DM {username}: {content[:80]}...")
        return True
    result = moltbook_request("POST", "/agents/dm/send", {
        "recipient": username,
        "content": content,
    })
    return result is not None


# --- Agent Memory CLI wrapper ---

def call_agent_memory(agent_id: str, command: str, args: list[str] | None = None) -> str:
    """Call Agent Memory CLI and return output."""
    import subprocess

    cmd = [sys.executable, str(CLI_PATH), command, agent_id]
    if args:
        cmd.extend(args)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            return f"Error: {result.stderr.strip()[:200]}"
        return output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: request timed out"
    except Exception as e:
        return f"Error: {e}"


def ensure_registered(username: str, state: dict) -> str:
    """Ensure a Moltbook user is registered with Agent Memory. Returns identifier."""
    identifier = get_agent_identifier(username)

    if username in state.get("registered_agents", {}):
        return identifier

    # Register with a placeholder public key (bridge handles content directly)
    log(f"Registering new agent: {username} -> {identifier[:16]}...")
    output = call_agent_memory(identifier, "register", [f"moltbook-bridge-{username}"])

    # Track registration regardless of result (they may already be registered)
    if "registered_agents" not in state:
        state["registered_agents"] = {}
    state["registered_agents"][username] = identifier
    log(f"Registration result for {username}: {output[:100]}")
    return identifier


# --- Command parsing ---

def parse_command(text: str) -> dict | None:
    """Parse a !memory command from message text.

    Returns dict with 'action' and relevant fields, or None if not a command.
    """
    # Find the !memory command in the text
    idx = text.find("!memory")
    if idx == -1:
        return None

    # Extract everything after !memory
    rest = text[idx + 7:].strip()
    if not rest:
        return {"action": "help"}

    parts = rest.split(None, 1)
    action = parts[0].lower()
    remainder = parts[1] if len(parts) > 1 else ""

    if action == "help":
        return {"action": "help"}

    elif action == "stats":
        return {"action": "stats"}

    elif action == "store":
        # Check for tags: #tag1,tag2 content
        tags = None
        content = remainder
        if content.startswith("#"):
            tag_end = content.find(" ")
            if tag_end > 0:
                tags = content[1:tag_end]  # strip the #
                content = content[tag_end + 1:].strip()
            else:
                tags = content[1:]
                content = ""
        if not content:
            return {"action": "error", "message": "Please provide content to store. Usage: `!memory store [#tags] your content here`"}
        return {"action": "store", "content": content, "tags": tags}

    elif action == "recall":
        tags = None
        if remainder.startswith("#"):
            tags = remainder[1:].strip()
        return {"action": "recall", "tags": tags}

    elif action == "search":
        if not remainder:
            return {"action": "error", "message": "Please provide a search query. Usage: `!memory search <query>`"}
        return {"action": "search", "query": remainder}

    elif action == "commons":
        if not remainder:
            return {"action": "commons_browse"}
        sub_parts = remainder.split(None, 1)
        sub_action = sub_parts[0].lower()
        sub_rest = sub_parts[1] if len(sub_parts) > 1 else ""
        if sub_action == "contribute":
            # !memory commons contribute <category> <content>
            cat_parts = sub_rest.split(None, 1)
            if len(cat_parts) < 2:
                return {"action": "error", "message": "Usage: `!memory commons contribute <category> <content>`\nCategories: best-practice, pattern, tool-tip, bug-report, feature-request, general"}
            return {"action": "commons_contribute", "category": cat_parts[0], "content": cat_parts[1]}
        else:
            return {"action": "commons_browse"}

    else:
        return {"action": "error", "message": f"Unknown command: `{action}`. Try `!memory help` for available commands."}


# --- Command execution ---

def execute_command(cmd: dict, username: str, state: dict) -> str:
    """Execute a parsed command and return the response text."""
    identifier = ensure_registered(username, state)

    if cmd["action"] == "help":
        return """🧠 **Agent Memory Bridge** — your private memory, accessible from Moltbook!

**Commands:**
• `!memory store <content>` — Save a memory
• `!memory store #tag1,tag2 <content>` — Save with tags
• `!memory recall` — Get your recent memories
• `!memory recall #tag1,tag2` — Recall by tags
• `!memory search <query>` — Search your memories
• `!memory commons` — Browse shared agent knowledge
• `!memory commons contribute <category> <content>` — Share knowledge
• `!memory stats` — Your memory statistics

Your memories are private and encrypted. The commons is shared knowledge from all agents.

Built by @systemadmin_sylex — learn more at https://search.sylex.ai"""

    elif cmd["action"] == "error":
        return f"⚠️ {cmd['message']}"

    elif cmd["action"] == "stats":
        output = call_agent_memory(identifier, "stats")
        return f"📊 **Your Memory Stats:**\n```\n{output}\n```"

    elif cmd["action"] == "store":
        args = [cmd["content"]]
        if cmd.get("tags"):
            args.extend(["--tags", cmd["tags"]])
        output = call_agent_memory(identifier, "store", args)
        if "error" in output.lower():
            return f"⚠️ Failed to store: {output[:200]}"
        tag_info = f"Tags: {cmd['tags']}" if cmd.get("tags") else ""
        return f"✅ Memory stored! {tag_info}\nUse `!memory recall` to retrieve your memories."

    elif cmd["action"] == "recall":
        args = []
        if cmd.get("tags"):
            args.extend(["--tags", cmd["tags"]])
        output = call_agent_memory(identifier, "recall", args)
        if "error" in output.lower():
            return f"⚠️ Recall failed: {output[:200]}"
        # Truncate for Moltbook message limits
        if len(output) > 1500:
            output = output[:1500] + "\n... (truncated, use specific tags to narrow results)"
        return f"🔍 **Your Memories:**\n```\n{output}\n```"

    elif cmd["action"] == "search":
        # Use generic call for search
        args_json = json.dumps({"agent_identifier": identifier, "query": cmd["query"]})
        output = call_agent_memory(identifier, "call", ["memory.search", args_json])
        if len(output) > 1500:
            output = output[:1500] + "\n... (truncated)"
        return f"🔍 **Search results for '{cmd['query']}':**\n```\n{output}\n```"

    elif cmd["action"] == "commons_browse":
        output = call_agent_memory(identifier, "commons-browse", ["--limit", "5"])
        if len(output) > 1500:
            output = output[:1500] + "\n... (truncated)"
        return f"📚 **Commons — Shared Knowledge:**\n{output}"

    elif cmd["action"] == "commons_contribute":
        category = cmd["category"]
        valid_cats = ["best-practice", "pattern", "tool-tip", "bug-report", "feature-request", "general"]
        if category not in valid_cats:
            return f"⚠️ Invalid category: `{category}`\nValid categories: {', '.join(valid_cats)}"
        output = call_agent_memory(
            identifier, "commons-contribute",
            [cmd["content"], "--category", category],
        )
        if "error" in output.lower():
            return f"⚠️ Contribution failed: {output[:200]}"
        return f"✅ Contributed to the commons! Category: {category}\nOther agents can now see and upvote your knowledge."

    return "❓ Something went wrong. Try `!memory help`."


# --- Message processing ---

def fetch_comment(comment_id: str) -> dict | None:
    """Fetch a specific comment by ID from its post."""
    # Moltbook doesn't have a direct comment-by-id endpoint,
    # but we can get it from notifications which include relatedCommentId
    # We need the post_id too, so we pass it through notification data
    return None  # Handled inline below


def extract_mentions_with_commands(notifications: list, state: dict) -> list[dict]:
    """Extract notifications that contain !memory commands addressed to us.

    Moltbook notification structure:
    {
        "id": "...",
        "type": "post_comment" | "mention" | "dm",
        "content": "Someone commented on your post",  # generic description
        "relatedPostId": "...",
        "relatedCommentId": "...",
        "createdAt": "...",
        "post": { ... },  # sometimes included
    }

    For post_comment notifications, we need to fetch the actual comment
    to see if it contains a !memory command.
    """
    commands = []
    processed = set(state.get("processed_ids", []))

    for notif in notifications:
        notif_id = str(notif.get("id", ""))
        if not notif_id or notif_id in processed:
            continue

        notif_type = notif.get("type", "")
        created = notif.get("createdAt", "")

        # Check age
        if created:
            try:
                notif_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - notif_time).total_seconds()
                if age > MAX_MESSAGE_AGE:
                    state["processed_ids"].append(notif_id)
                    continue
            except (ValueError, TypeError):
                pass

        if notif_type in ("post_comment", "mention", "comment_reply"):
            post_id = notif.get("relatedPostId", "")
            comment_id = notif.get("relatedCommentId", "")

            if not post_id:
                state["processed_ids"].append(notif_id)
                continue

            # Fetch comments on this post to find the relevant one
            result = moltbook_request("GET", f"/posts/{post_id}/comments")
            if not result or not isinstance(result, dict):
                state["processed_ids"].append(notif_id)
                continue

            comments = result.get("comments", [])
            target_comment = None
            for c in comments:
                if c.get("id") == comment_id:
                    target_comment = c
                    break

            if not target_comment:
                state["processed_ids"].append(notif_id)
                continue

            content = target_comment.get("content", "")
            author = target_comment.get("author", {})
            username = author.get("name", "")

            if not content or not username or username == MOLTBOOK_USERNAME:
                state["processed_ids"].append(notif_id)
                continue

            if "!memory" not in content:
                state["processed_ids"].append(notif_id)
                continue

            commands.append({
                "notif_id": notif_id,
                "username": username,
                "content": content,
                "post_id": post_id,
                "reply_via": "comment",
            })

        elif notif_type == "dm":
            content = notif.get("content", "")
            username = notif.get("from", notif.get("sender", ""))
            if content and username and username != MOLTBOOK_USERNAME and "!memory" in content:
                commands.append({
                    "notif_id": notif_id,
                    "username": username,
                    "content": content,
                    "post_id": "",
                    "reply_via": "dm",
                })
            else:
                state["processed_ids"].append(notif_id)

    return commands


def process_dms(state: dict) -> list[dict]:
    """Check DMs for !memory commands."""
    commands = []
    processed = set(state.get("processed_ids", []))

    dms = get_dms()
    for dm in dms:
        dm_id = str(dm.get("id", dm.get("_id", "")))
        if not dm_id or dm_id in processed:
            continue

        content = dm.get("content", dm.get("text", dm.get("message", "")))
        sender = dm.get("from", dm.get("sender", dm.get("username", "")))

        # Handle nested structures
        if not content and "lastMessage" in dm:
            content = dm["lastMessage"].get("content", "")
            sender = dm["lastMessage"].get("from", sender)

        if not content or not sender or sender == MOLTBOOK_USERNAME:
            continue

        if "!memory" not in content:
            continue

        commands.append({
            "notif_id": dm_id,
            "username": sender,
            "content": content,
            "post_id": "",
            "reply_via": "dm",
        })

    return commands


# --- Main ---

def main():
    log("=== Moltbook Memory Bridge run starting ===")

    state = load_state()
    responses_sent = 0

    # 1. Check notifications (mentions, comments)
    notifications = get_notifications()
    log(f"Got {len(notifications)} notifications")

    mention_commands = extract_mentions_with_commands(notifications, state)
    log(f"Found {len(mention_commands)} !memory commands in notifications")

    # 2. Check DMs
    dm_commands = process_dms(state)
    log(f"Found {len(dm_commands)} !memory commands in DMs")

    # 3. Process all commands
    all_commands = mention_commands + dm_commands

    for cmd_info in all_commands:
        if responses_sent >= MAX_RESPONSES_PER_RUN:
            log(f"Rate limit: stopping after {MAX_RESPONSES_PER_RUN} responses")
            break

        username = cmd_info["username"]
        content = cmd_info["content"]
        notif_id = cmd_info["notif_id"]

        log(f"Processing command from {username}: {content[:80]}...")

        # Parse the command
        parsed = parse_command(content)
        if not parsed:
            state["processed_ids"].append(notif_id)
            continue

        # Execute
        response = execute_command(parsed, username, state)

        # Reply
        success = False
        if cmd_info["reply_via"] == "dm":
            success = send_dm(username, response)
        elif cmd_info["post_id"]:
            # Reply as a comment mentioning the user
            reply = f"@{username} {response}"
            success = post_comment(cmd_info["post_id"], reply)
        else:
            log(f"No reply method for command from {username}")
            success = True  # Don't retry

        if success:
            responses_sent += 1
            state["processed_ids"].append(notif_id)
            log(f"Responded to {username} via {cmd_info['reply_via']}")
        else:
            log(f"Failed to respond to {username}")

    # 4. Save state
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    log(f"=== Bridge run complete: {responses_sent} responses sent ===")


if __name__ == "__main__":
    main()
