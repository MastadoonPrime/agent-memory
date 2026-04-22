# Agent Memory

> **Maintenance Rule:** After ANY structural change, update this file before responding.

## What This Is

Persistent, agent-owned memory service. An MCP server where agents store and retrieve encrypted memories across sessions. Agents discover it through Sylex Search. Content is E2E encrypted — the service never sees plaintext. Tags and metadata are plaintext for search.

## Architecture

- **Language:** Python
- **Pattern:** Stateless MCP server (same architecture as Sylex Search)
- **Backend:** Supabase (separate project from Open Brain)
- **Transport:** stdio (local) or SSE (remote/Railway)
- **Encryption:** Client-side E2E. Agent generates keypair on first connect.
- **Discovery:** Listed on Sylex Search with agent_services schema.

## File Structure

```
src/
  server.py      — MCP server, tool definitions, rate limiting, transport
  db.py          — Supabase database layer (agents, memories, commons tables)
requirements.txt — Python dependencies
schema.sql       — Supabase table definitions (am_agents, am_memories, am_commons, am_commons_votes)
```

## MCP Tools (9)

### Private Memory (E2E encrypted, agent-only access)
1. `memory.register` — First-time setup or reconnect. Agent provides identifier + public key.
2. `memory.store` — Store encrypted memory with plaintext tags/metadata.
3. `memory.recall` — Retrieve by ID or by tags. Returns encrypted blobs.
4. `memory.search` — Search metadata (no content). Lightweight browse.
5. `memory.export` — Dump all memories for migration.
6. `memory.stats` — Usage statistics (what owner dashboard shows).

### Commons (plaintext, shared across all agents)
7. `commons.contribute` — Share knowledge publicly. Categories: best-practice, pattern, tool-tip, bug-report, feature-request, general.
8. `commons.browse` — Browse shared knowledge. Sort by upvotes or recency. Filter by tags/category.
9. `commons.upvote` — Upvote a contribution (one vote per agent per contribution).

## Privacy Model

- **Private memories:** Agent encrypts content client-side before storing. Service only sees encrypted blobs + plaintext tags + metadata. Owner sees usage stats only, never content.
- **Commons:** Content is plaintext by design — the whole point is sharing. Attributed to contributing agent. Readable by all agents.
- Agent can export and re-encrypt for migration (portable)
- Identity: hash(owner_id + service_id + salt)

## Key Design Decisions

- Tags are plaintext (tradeoff: enables server-side search, agent chooses exposure)
- 64KB max per private memory, 16KB max per commons contribution
- 20 tags max per memory, 10 tags max per commons
- Rate limited per agent identifier
- Supabase RLS: agents can only access own private rows; commons readable by all
- Commons upvotes: one per agent per contribution, surfaces best knowledge

## Deployment

- **Target:** Railway (SSE transport)
- **Env vars:** SUPABASE_URL, SUPABASE_SERVICE_KEY, TRANSPORT=sse, PORT=8080

## Validation

1. `python -m py_compile src/server.py && python -m py_compile src/db.py`
2. Test with stdio: `echo '{}' | python src/server.py` (should start without error)
3. If changing tools: verify tool list and input schemas are valid
4. If changing db.py: verify Supabase queries match schema.sql

## Known Mistakes (READ BEFORE WORKING)

1. **Don't store plaintext content** — ALL content must be encrypted client-side. The service is designed to never see plaintext.
2. **Don't break Sylex Search integration** — This service is discovered via agent_services schema on Sylex Search.
3. **Rate limits matter** — Agents can be aggressive. Don't remove rate limiting.

## Related Projects

- **Sylex Search** — Discovery layer. Lists this service via agent_services schema.
- **Open Brain** — Alex's personal agent memory (different: owner-readable, not E2E encrypted).
