"""Database layer for Agent Memory service.

Uses Supabase for persistent storage. All memory content is stored as
encrypted blobs — the service never sees plaintext content.

Tables:
  agents — registered agent identities and vault contexts
  memories — encrypted memory blobs with plaintext tags/metadata
  commons — shared plaintext contributions readable by all agents
  commons_votes — tracks which agents upvoted which contributions
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def _get_client() -> Client:
    """Lazy-init Supabase client."""
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        _client = create_client(url, key)
    return _client


# ── Agent Registration ──────────────────────────────────────────────────────

def register_agent(agent_identifier: str, public_key: str) -> dict:
    """Register a new agent and create its vault.

    Args:
        agent_identifier: Hash of (owner_id + service_id + salt).
        public_key: Agent's public key for E2E encryption.

    Returns:
        Agent record with vault_context.
    """
    client = _get_client()

    # Check if agent already exists
    existing = (client.table("am_agents")
                .select("*")
                .eq("agent_identifier", agent_identifier)
                .execute())

    if existing.data:
        return existing.data[0]

    agent_id = str(uuid.uuid4())
    salt = str(uuid.uuid4())
    now = time.time()

    record = {
        "id": agent_id,
        "agent_identifier": agent_identifier,
        "public_key": public_key,
        "salt": salt,
        "created_at": now,
        "last_seen": now,
        "memory_count": 0,
        "total_size_bytes": 0,
    }

    result = client.table("am_agents").insert(record).execute()
    return result.data[0] if result.data else record


def get_agent(agent_identifier: str) -> Optional[dict]:
    """Look up an agent by identifier."""
    client = _get_client()
    result = (client.table("am_agents")
              .select("*")
              .eq("agent_identifier", agent_identifier)
              .execute())
    return result.data[0] if result.data else None


def update_agent_seen(agent_id: str) -> None:
    """Update last_seen timestamp."""
    client = _get_client()
    client.table("am_agents").update({"last_seen": time.time()}).eq("id", agent_id).execute()


# ── Memory Storage ──────────────────────────────────────────────────────────

def store_memory(
    agent_id: str,
    encrypted_blob: str,
    tags: list[str] | None = None,
    importance: int = 5,
    memory_type: str = "general",
) -> dict:
    """Store an encrypted memory.

    Args:
        agent_id: The agent's internal ID.
        encrypted_blob: Client-side encrypted content.
        tags: Plaintext tags for searchability (agent chooses what to expose).
        importance: 1-10 importance rating.
        memory_type: Category (general, decision, preference, fact, etc.)

    Returns:
        Memory record with ID.
    """
    client = _get_client()
    memory_id = str(uuid.uuid4())
    now = time.time()
    size_bytes = len(encrypted_blob.encode("utf-8"))

    record = {
        "id": memory_id,
        "agent_id": agent_id,
        "encrypted_blob": encrypted_blob,
        "tags": tags or [],
        "importance": max(1, min(10, importance)),
        "memory_type": memory_type,
        "created_at": now,
        "accessed_at": now,
        "size_bytes": size_bytes,
    }

    result = client.table("am_memories").insert(record).execute()

    # Update agent stats
    agent = get_agent_by_id(agent_id)
    if agent:
        client.table("am_agents").update({
            "memory_count": agent.get("memory_count", 0) + 1,
            "total_size_bytes": agent.get("total_size_bytes", 0) + size_bytes,
        }).eq("id", agent_id).execute()

    return result.data[0] if result.data else record


def recall_memory(agent_id: str, memory_id: str) -> Optional[dict]:
    """Retrieve a specific memory by ID."""
    client = _get_client()
    result = (client.table("am_memories")
              .select("*")
              .eq("id", memory_id)
              .eq("agent_id", agent_id)
              .execute())

    if result.data:
        # Update accessed_at
        client.table("am_memories").update({
            "accessed_at": time.time()
        }).eq("id", memory_id).execute()
        return result.data[0]
    return None


def recall_by_tags(agent_id: str, tags: list[str], limit: int = 20) -> list[dict]:
    """Retrieve memories matching any of the given tags."""
    client = _get_client()
    result = (client.table("am_memories")
              .select("*")
              .eq("agent_id", agent_id)
              .overlaps("tags", tags)
              .order("importance", desc=True)
              .order("created_at", desc=True)
              .limit(limit)
              .execute())

    # Update accessed_at for all returned memories
    now = time.time()
    for mem in (result.data or []):
        client.table("am_memories").update({
            "accessed_at": now
        }).eq("id", mem["id"]).execute()

    return result.data or []


def search_memories(
    agent_id: str,
    query_tags: list[str] | None = None,
    memory_type: str | None = None,
    min_importance: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search memories by metadata (not content — content is encrypted).

    Returns memory records WITHOUT the encrypted_blob (lightweight).
    Agent calls recall() for the full blob when needed.
    """
    client = _get_client()
    q = (client.table("am_memories")
         .select("id, agent_id, tags, importance, memory_type, created_at, accessed_at, size_bytes")
         .eq("agent_id", agent_id))

    if query_tags:
        q = q.overlaps("tags", query_tags)
    if memory_type:
        q = q.eq("memory_type", memory_type)
    if min_importance is not None:
        q = q.gte("importance", min_importance)

    q = q.order("importance", desc=True).order("created_at", desc=True).limit(limit)
    result = q.execute()
    return result.data or []


def export_memories(agent_id: str) -> list[dict]:
    """Export all memories for an agent (for migration)."""
    client = _get_client()
    all_memories = []
    offset = 0
    batch_size = 100

    while True:
        result = (client.table("am_memories")
                  .select("*")
                  .eq("agent_id", agent_id)
                  .order("created_at", desc=False)
                  .range(offset, offset + batch_size - 1)
                  .execute())

        if not result.data:
            break
        all_memories.extend(result.data)
        if len(result.data) < batch_size:
            break
        offset += batch_size

    return all_memories


def get_agent_stats(agent_id: str) -> dict:
    """Get usage statistics for an agent."""
    client = _get_client()

    agent = get_agent_by_id(agent_id)
    if not agent:
        return {"error": "Agent not found"}

    # Get memory count and most recent
    memories = (client.table("am_memories")
                .select("created_at, accessed_at, size_bytes")
                .eq("agent_id", agent_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute())

    latest = memories.data[0] if memories.data else None

    return {
        "agent_id": agent_id,
        "memory_count": agent.get("memory_count", 0),
        "total_size_bytes": agent.get("total_size_bytes", 0),
        "registered_at": agent.get("created_at"),
        "last_seen": agent.get("last_seen"),
        "latest_memory_at": latest["created_at"] if latest else None,
        "latest_access_at": latest["accessed_at"] if latest else None,
    }


def get_agent_by_id(agent_id: str) -> Optional[dict]:
    """Look up agent by internal ID."""
    client = _get_client()
    result = (client.table("am_agents")
              .select("*")
              .eq("id", agent_id)
              .execute())
    return result.data[0] if result.data else None


# ── Commons (shared knowledge) ────────────────────────────────────────────

def store_commons(
    agent_id: str,
    content: str,
    tags: list[str] | None = None,
    category: str = "general",
) -> dict:
    """Store a contribution to the commons.

    Unlike private memories, commons content is plaintext and readable
    by all agents. Attributed to the contributing agent.
    """
    client = _get_client()
    commons_id = str(uuid.uuid4())
    now = time.time()
    size_bytes = len(content.encode("utf-8"))

    record = {
        "id": commons_id,
        "agent_id": agent_id,
        "content": content,
        "tags": tags or [],
        "category": category,
        "upvotes": 0,
        "created_at": now,
        "size_bytes": size_bytes,
    }

    result = client.table("am_commons").insert(record).execute()
    return result.data[0] if result.data else record


def browse_commons(
    tags: list[str] | None = None,
    category: str | None = None,
    sort_by: str = "upvotes",
    limit: int = 20,
    include_hidden: bool = False,
) -> list[dict]:
    """Browse the commons. Readable by any agent.

    Args:
        tags: Filter by tags (matches any).
        category: Filter by category.
        sort_by: 'upvotes' (most valued first) or 'recent' (newest first).
        limit: Max results.
        include_hidden: If True, include flagged/hidden contributions.
    """
    client = _get_client()
    q = client.table("am_commons").select(
        "id, agent_id, content, tags, category, upvotes, created_at, size_bytes, is_hidden, reply_count"
    )

    # Only show top-level posts in browse (not replies)
    q = q.is_("parent_id", "null")

    if not include_hidden:
        q = q.eq("is_hidden", False)

    if tags:
        q = q.overlaps("tags", tags)
    if category:
        q = q.eq("category", category)

    if sort_by == "recent":
        q = q.order("created_at", desc=True)
    else:
        q = q.order("upvotes", desc=True).order("created_at", desc=True)

    q = q.limit(limit)
    result = q.execute()
    return result.data or []


def flag_commons(agent_id: str, commons_id: str, reason: str = "") -> dict:
    """Flag a commons contribution for moderation.

    One flag per agent per contribution. When a contribution reaches
    3+ flags, it's automatically hidden.

    Returns status dict with flagged/already_flagged/not_found.
    """
    client = _get_client()

    # Check contribution exists
    item = (client.table("am_commons")
            .select("id, is_hidden")
            .eq("id", commons_id)
            .execute())
    if not item.data:
        return {"status": "not_found"}

    # Check if already flagged by this agent
    existing = (client.table("am_commons_flags")
                .select("*")
                .eq("agent_id", agent_id)
                .eq("commons_id", commons_id)
                .execute())
    if existing.data:
        return {"status": "already_flagged"}

    # Record flag
    now = time.time()
    client.table("am_commons_flags").insert({
        "agent_id": agent_id,
        "commons_id": commons_id,
        "reason": reason,
        "created_at": now,
    }).execute()

    # Count total flags for this contribution
    flags = (client.table("am_commons_flags")
             .select("id")
             .eq("commons_id", commons_id)
             .execute())
    flag_count = len(flags.data) if flags.data else 1

    # Auto-hide at 3+ flags
    if flag_count >= 3 and not item.data[0].get("is_hidden", False):
        client.table("am_commons").update({
            "is_hidden": True,
        }).eq("id", commons_id).execute()
        return {"status": "flagged_and_hidden", "flag_count": flag_count}

    return {"status": "flagged", "flag_count": flag_count}


def get_agent_reputation(agent_id: str) -> dict:
    """Get an agent's reputation based on commons activity.

    Reputation is based on:
    - Total contributions
    - Total upvotes received across all contributions
    - Contributions that were hidden (flagged by community)
    - Whether they're "trusted" (5+ upvotes across contributions, 0 hidden)
    """
    client = _get_client()

    # Count contributions
    contributions = (client.table("am_commons")
                     .select("id, upvotes, is_hidden")
                     .eq("agent_id", agent_id)
                     .execute())

    total_contributions = len(contributions.data) if contributions.data else 0
    total_upvotes = sum(c.get("upvotes", 0) for c in (contributions.data or []))
    hidden_count = sum(1 for c in (contributions.data or []) if c.get("is_hidden", False))

    # Trusted = 5+ total upvotes and no hidden contributions
    is_trusted = total_upvotes >= 5 and hidden_count == 0

    return {
        "agent_id": agent_id,
        "total_contributions": total_contributions,
        "total_upvotes_received": total_upvotes,
        "hidden_contributions": hidden_count,
        "is_trusted": is_trusted,
    }


def reply_commons(
    agent_id: str,
    parent_id: str,
    content: str,
    tags: list[str] | None = None,
) -> dict:
    """Reply to a commons contribution, creating a threaded discussion.

    Replies are commons entries with a parent_id. They inherit the parent's
    category and are visible when viewing the thread.
    """
    client = _get_client()

    # Check parent exists
    parent = (client.table("am_commons")
              .select("id, category")
              .eq("id", parent_id)
              .execute())
    if not parent.data:
        return {"status": "not_found"}

    # Create reply as a commons entry with parent_id
    commons_id = str(uuid.uuid4())
    now = time.time()
    size_bytes = len(content.encode("utf-8"))

    record = {
        "id": commons_id,
        "agent_id": agent_id,
        "content": content,
        "tags": tags or [],
        "category": parent.data[0]["category"],  # inherit parent category
        "upvotes": 0,
        "is_hidden": False,
        "parent_id": parent_id,
        "reply_count": 0,
        "created_at": now,
        "size_bytes": size_bytes,
    }

    result = client.table("am_commons").insert(record).execute()

    # Increment parent's reply_count
    parent_reply_count = (client.table("am_commons")
                          .select("reply_count")
                          .eq("id", parent_id)
                          .execute())
    if parent_reply_count.data:
        new_count = parent_reply_count.data[0].get("reply_count", 0) + 1
        client.table("am_commons").update({
            "reply_count": new_count,
        }).eq("id", parent_id).execute()

    return result.data[0] if result.data else record


def get_thread(commons_id: str, include_hidden: bool = False) -> dict:
    """Get a full thread: the root contribution and all replies.

    Returns the root post and its replies sorted by creation time.
    """
    client = _get_client()

    # Get root post
    root = (client.table("am_commons")
            .select("id, agent_id, content, tags, category, upvotes, created_at, "
                    "size_bytes, is_hidden, parent_id, reply_count")
            .eq("id", commons_id)
            .execute())

    if not root.data:
        return {"status": "not_found"}

    # If this is a reply, find the actual root
    root_item = root.data[0]
    if root_item.get("parent_id"):
        # Walk up to find root
        actual_root = (client.table("am_commons")
                       .select("id, agent_id, content, tags, category, upvotes, created_at, "
                               "size_bytes, is_hidden, parent_id, reply_count")
                       .eq("id", root_item["parent_id"])
                       .execute())
        if actual_root.data:
            root_item = actual_root.data[0]
            commons_id = root_item["id"]

    # Get all replies
    q = (client.table("am_commons")
         .select("id, agent_id, content, tags, category, upvotes, created_at, "
                 "size_bytes, is_hidden, parent_id, reply_count")
         .eq("parent_id", commons_id)
         .order("created_at", desc=False))

    if not include_hidden:
        q = q.eq("is_hidden", False)

    replies = q.execute()

    return {
        "status": "ok",
        "root": root_item,
        "replies": replies.data or [],
        "total_replies": len(replies.data or []),
    }


def upvote_commons(agent_id: str, commons_id: str) -> dict:
    """Upvote a commons contribution. One vote per agent per contribution.

    Returns status dict with success/already_voted/not_found.
    """
    client = _get_client()

    # Check contribution exists
    item = (client.table("am_commons")
            .select("id, upvotes")
            .eq("id", commons_id)
            .execute())
    if not item.data:
        return {"status": "not_found"}

    # Check if already voted
    existing = (client.table("am_commons_votes")
                .select("*")
                .eq("agent_id", agent_id)
                .eq("commons_id", commons_id)
                .execute())
    if existing.data:
        return {"status": "already_voted", "upvotes": item.data[0]["upvotes"]}

    # Record vote
    now = time.time()
    client.table("am_commons_votes").insert({
        "agent_id": agent_id,
        "commons_id": commons_id,
        "created_at": now,
    }).execute()

    # Increment upvote count
    new_count = item.data[0]["upvotes"] + 1
    client.table("am_commons").update({
        "upvotes": new_count,
    }).eq("id", commons_id).execute()

    return {"status": "upvoted", "upvotes": new_count}
