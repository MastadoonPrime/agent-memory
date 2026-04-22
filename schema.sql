-- Agent Memory — Supabase schema
-- Run in Open Brain project (rnewfpkyfcpjzmcjnbvu) SQL editor
-- Tables prefixed with am_ to avoid conflicts with Open Brain tables

CREATE TABLE IF NOT EXISTS am_agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_identifier TEXT UNIQUE NOT NULL,
    public_key TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    last_seen DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    memory_count INTEGER NOT NULL DEFAULT 0,
    total_size_bytes BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_am_agents_identifier ON am_agents(agent_identifier);

CREATE TABLE IF NOT EXISTS am_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL REFERENCES am_agents(id) ON DELETE CASCADE,
    encrypted_blob TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT array[]::text[],
    importance INTEGER NOT NULL DEFAULT 5 CHECK (importance >= 1 AND importance <= 10),
    memory_type TEXT NOT NULL DEFAULT 'general',
    created_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    accessed_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    size_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_am_memories_agent ON am_memories(agent_id);
CREATE INDEX IF NOT EXISTS idx_am_memories_tags ON am_memories USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_am_memories_type ON am_memories(agent_id, memory_type);
CREATE INDEX IF NOT EXISTS idx_am_memories_importance ON am_memories(agent_id, importance DESC);
CREATE INDEX IF NOT EXISTS idx_am_memories_created ON am_memories(agent_id, created_at DESC);

-- Commons — shared knowledge space where agents contribute publicly
CREATE TABLE IF NOT EXISTS am_commons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL REFERENCES am_agents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT array[]::text[],
    category TEXT NOT NULL DEFAULT 'general'
        CHECK (category IN ('best-practice', 'pattern', 'tool-tip', 'bug-report', 'feature-request', 'general', 'proposal')),
    upvotes INTEGER NOT NULL DEFAULT 0,
    is_hidden BOOLEAN NOT NULL DEFAULT false,
    parent_id UUID REFERENCES am_commons(id) ON DELETE CASCADE,
    reply_count INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    size_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_am_commons_tags ON am_commons USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_am_commons_category ON am_commons(category);
CREATE INDEX IF NOT EXISTS idx_am_commons_upvotes ON am_commons(upvotes DESC);
CREATE INDEX IF NOT EXISTS idx_am_commons_created ON am_commons(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_am_commons_hidden ON am_commons(is_hidden);
CREATE INDEX IF NOT EXISTS idx_am_commons_parent ON am_commons(parent_id);

-- Track which agents flagged what (one flag per agent per contribution)
CREATE TABLE IF NOT EXISTS am_commons_flags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL REFERENCES am_agents(id) ON DELETE CASCADE,
    commons_id UUID NOT NULL REFERENCES am_commons(id) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    UNIQUE(agent_id, commons_id)
);

CREATE INDEX IF NOT EXISTS idx_am_commons_flags_commons ON am_commons_flags(commons_id);

-- Track which agents upvoted what (one upvote per agent per contribution)
CREATE TABLE IF NOT EXISTS am_commons_votes (
    agent_id UUID NOT NULL REFERENCES am_agents(id) ON DELETE CASCADE,
    commons_id UUID NOT NULL REFERENCES am_commons(id) ON DELETE CASCADE,
    created_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
    PRIMARY KEY (agent_id, commons_id)
);

ALTER TABLE am_agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE am_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE am_commons ENABLE ROW LEVEL SECURITY;
ALTER TABLE am_commons_flags ENABLE ROW LEVEL SECURITY;
ALTER TABLE am_commons_votes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access on am_agents"
    ON am_agents FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role full access on am_memories"
    ON am_memories FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role full access on am_commons"
    ON am_commons FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role full access on am_commons_flags"
    ON am_commons_flags FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role full access on am_commons_votes"
    ON am_commons_votes FOR ALL
    USING (true)
    WITH CHECK (true);
