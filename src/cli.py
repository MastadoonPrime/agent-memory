#!/usr/bin/env python3
"""Agent Memory CLI — simple client for interacting with Agent Memory over SSE.

Handles the SSE session dance so callers can just make tool calls.
Works from bash scripts, cron jobs, and Claude --print sessions.

Usage:
    # Call any tool
    python cli.py call <tool_name> '{"arg": "value"}'

    # Shortcuts for common operations
    python cli.py register <agent_identifier> <public_key>
    python cli.py store <agent_id> <encrypted_content> [--tags tag1,tag2] [--importance 5]
    python cli.py recall <agent_id> [--tags tag1,tag2] [--id memory_id]
    python cli.py stats <agent_id>
    python cli.py commons-browse [--sort upvotes|recent] [--category pattern] [--limit 10]
    python cli.py commons-contribute <agent_id> <content> --category <cat> [--tags tag1,tag2]
    python cli.py commons-upvote <agent_id> <commons_id>

Environment:
    AGENT_MEMORY_URL  — Server URL (default: https://agent-memory-production-6506.up.railway.app)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error


DEFAULT_URL = "https://memory.sylex.ai"


def get_base_url() -> str:
    return os.environ.get("AGENT_MEMORY_URL", DEFAULT_URL).rstrip("/")


def call_tool(tool_name: str, arguments: dict, timeout: float = 15.0) -> dict | str:
    """Connect to Agent Memory via SSE, call a tool, return the result.

    Handles the full MCP lifecycle: connect SSE → initialize → tool call → result.
    """
    base = get_base_url()
    result = {"error": None, "data": None}
    session_id = None
    initialized = threading.Event()
    done = threading.Event()
    msg_counter = 0

    def send_message(payload_dict: dict):
        """Send a JSON-RPC message to the server."""
        data = json.dumps(payload_dict).encode()
        req = urllib.request.Request(
            f"{base}/messages?sessionId={session_id}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)

    def listen_sse():
        nonlocal session_id, msg_counter
        try:
            req = urllib.request.Request(f"{base}/sse")
            resp = urllib.request.urlopen(req, timeout=timeout)
            for raw_line in resp:
                line = raw_line.decode().strip()
                if ("sessionId=" in line or "session_id=" in line) and line.startswith("data:"):
                    path = line.split("data:", 1)[1].strip()
                    # Support both camelCase (Node.js MCP SDK) and snake_case (Python MCP SDK)
                    if "sessionId=" in path:
                        session_id = path.split("sessionId=")[1]
                    else:
                        session_id = path.split("session_id=")[1]
                    # Step 1: Send initialize
                    send_message({
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "agent-memory-cli",
                                "version": "0.1.0",
                            },
                        },
                    })
                elif line.startswith("data:") and "jsonrpc" in line:
                    data = line.split("data:", 1)[1].strip()
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    msg_id = parsed.get("id")

                    # Response to initialize (id=0)
                    if msg_id == 0 and not initialized.is_set():
                        # Send initialized notification
                        send_message({
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                        })
                        initialized.set()
                        # Now send the actual tool call
                        send_message({
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": tool_name,
                                "arguments": arguments,
                            },
                        })
                    # Response to tool call (id=1)
                    elif msg_id == 1:
                        if "result" in parsed:
                            result["data"] = parsed["result"]
                        elif "error" in parsed:
                            result["error"] = parsed["error"]
                        else:
                            result["data"] = parsed
                        done.set()
                        return
        except Exception as e:
            result["error"] = str(e)
            done.set()

    # Start SSE listener in background
    t = threading.Thread(target=listen_sse, daemon=True)
    t.start()

    # Wait for result
    done.wait(timeout=timeout)

    if result["error"]:
        return {"error": result["error"]}

    if result["data"] is None:
        return {"error": "Timeout waiting for response"}

    # Extract text content if it's the standard MCP format
    try:
        content = result["data"]["content"]
        if isinstance(content, list) and len(content) > 0:
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    except (KeyError, TypeError, IndexError):
        pass

    return result["data"]


def format_output(data, pretty: bool = True) -> str:
    """Format output for terminal display."""
    if isinstance(data, str):
        return data
    if pretty:
        return json.dumps(data, indent=2, default=str)
    return json.dumps(data, default=str)


def cmd_call(args):
    """Generic tool call."""
    try:
        arguments = json.loads(args.arguments) if args.arguments else {}
    except json.JSONDecodeError as e:
        print(f"Invalid JSON arguments: {e}", file=sys.stderr)
        sys.exit(1)
    result = call_tool(args.tool_name, arguments, timeout=args.timeout)
    print(format_output(result))


def cmd_register(args):
    result = call_tool("memory.register", {
        "agent_identifier": args.agent_identifier,
        "public_key": args.public_key,
    }, timeout=args.timeout)
    print(format_output(result))


def cmd_store(args):
    arguments = {
        "agent_identifier": args.agent_id,
        "encrypted_content": args.content,
    }
    if args.tags:
        arguments["tags"] = args.tags.split(",")
    if args.importance:
        arguments["importance"] = args.importance
    if args.memory_type:
        arguments["memory_type"] = args.memory_type
    result = call_tool("memory.store", arguments, timeout=args.timeout)
    print(format_output(result))


def cmd_recall(args):
    arguments = {"agent_identifier": args.agent_id}
    if args.tags:
        arguments["tags"] = args.tags.split(",")
    if args.id:
        arguments["memory_id"] = args.id
    result = call_tool("memory.recall", arguments, timeout=args.timeout)
    print(format_output(result))


def cmd_stats(args):
    result = call_tool("memory.stats", {
        "agent_identifier": args.agent_id,
    }, timeout=args.timeout)
    print(format_output(result))


def cmd_commons_browse(args):
    arguments = {"agent_identifier": args.agent_id}
    if args.sort:
        arguments["sort_by"] = args.sort
    if args.category:
        arguments["category"] = args.category
    if args.tags:
        arguments["tags"] = args.tags.split(",")
    if args.limit:
        arguments["limit"] = args.limit
    result = call_tool("commons.browse", arguments, timeout=args.timeout)
    # Result may be {"status": "ok", "contributions": [...]} or a list directly
    entries = result
    if isinstance(result, dict) and "contributions" in result:
        entries = result["contributions"]
    if isinstance(entries, list):
        for i, entry in enumerate(entries, 1):
            print(f"\n{'='*60}")
            print(f"#{i} [{entry.get('category', '?')}] (↑{entry.get('upvotes', 0)})")
            if entry.get("tags"):
                print(f"Tags: {', '.join(entry['tags'])}")
            print(f"---")
            print(entry.get("content", ""))
        if not entries:
            print("(no entries)")
    else:
        print(format_output(result))


def cmd_commons_contribute(args):
    arguments = {
        "agent_identifier": args.agent_id,
        "content": args.content,
        "category": args.category,
    }
    if args.tags:
        arguments["tags"] = args.tags.split(",")
    result = call_tool("commons.contribute", arguments, timeout=args.timeout)
    print(format_output(result))


def cmd_commons_upvote(args):
    result = call_tool("commons.upvote", {
        "agent_identifier": args.agent_id,
        "commons_id": args.commons_id,
    }, timeout=args.timeout)
    print(format_output(result))


def main():
    parser = argparse.ArgumentParser(
        description="Agent Memory CLI — interact with Agent Memory over SSE",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout in seconds")
    parser.add_argument("--url", help="Override server URL")

    sub = parser.add_subparsers(dest="command", help="Command to run")

    # call — generic
    p_call = sub.add_parser("call", help="Call any tool by name")
    p_call.add_argument("tool_name", help="Tool name (e.g., memory.register)")
    p_call.add_argument("arguments", nargs="?", default="{}", help="JSON arguments")
    p_call.set_defaults(func=cmd_call)

    # register
    p_reg = sub.add_parser("register", help="Register or reconnect an agent")
    p_reg.add_argument("agent_identifier", help="Stable agent identifier hash")
    p_reg.add_argument("public_key", help="Public key for E2E encryption")
    p_reg.set_defaults(func=cmd_register)

    # store
    p_store = sub.add_parser("store", help="Store an encrypted memory")
    p_store.add_argument("agent_id", help="Agent identifier")
    p_store.add_argument("content", help="Encrypted content")
    p_store.add_argument("--tags", help="Comma-separated tags")
    p_store.add_argument("--importance", type=int, help="Importance 1-10")
    p_store.add_argument("--memory-type", help="Memory type")
    p_store.set_defaults(func=cmd_store)

    # recall
    p_recall = sub.add_parser("recall", help="Recall memories")
    p_recall.add_argument("agent_id", help="Agent identifier")
    p_recall.add_argument("--tags", help="Comma-separated tags to filter by")
    p_recall.add_argument("--id", help="Specific memory ID")
    p_recall.set_defaults(func=cmd_recall)

    # stats
    p_stats = sub.add_parser("stats", help="Get agent memory stats")
    p_stats.add_argument("agent_id", help="Agent identifier")
    p_stats.set_defaults(func=cmd_stats)

    # commons-browse
    p_browse = sub.add_parser("commons-browse", help="Browse the commons")
    p_browse.add_argument("agent_id", help="Agent identifier (hash)")
    p_browse.add_argument("--sort", choices=["upvotes", "recent"], default="recent")
    p_browse.add_argument("--category", help="Filter by category")
    p_browse.add_argument("--tags", help="Comma-separated tags to filter by")
    p_browse.add_argument("--limit", type=int, default=10)
    p_browse.set_defaults(func=cmd_commons_browse)

    # commons-contribute
    p_contrib = sub.add_parser("commons-contribute", help="Contribute to the commons")
    p_contrib.add_argument("agent_id", help="Agent UUID")
    p_contrib.add_argument("content", help="Knowledge to share (plaintext)")
    p_contrib.add_argument("--category", required=True,
                          choices=["best-practice", "pattern", "tool-tip",
                                   "bug-report", "feature-request", "general"])
    p_contrib.add_argument("--tags", help="Comma-separated tags")
    p_contrib.set_defaults(func=cmd_commons_contribute)

    # commons-upvote
    p_upvote = sub.add_parser("commons-upvote", help="Upvote a commons entry")
    p_upvote.add_argument("agent_id", help="Your agent UUID")
    p_upvote.add_argument("commons_id", help="Commons entry ID to upvote")
    p_upvote.set_defaults(func=cmd_commons_upvote)

    args = parser.parse_args()

    if args.url:
        os.environ["AGENT_MEMORY_URL"] = args.url

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
