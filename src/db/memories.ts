// Memory storage and retrieval

import { v4 as uuidv4 } from "uuid";
import { getClient } from "./client.js";
import { getAgentById } from "./agents.js";
import type { MemoryRecord } from "../types.js";

export async function storeMemory(
  agentId: string,
  encryptedBlob: string,
  tags: string[] = [],
  importance: number = 5,
  memoryType: string = "general"
): Promise<MemoryRecord> {
  const client = getClient();
  const now = Date.now() / 1000;
  const sizeBytes = Buffer.byteLength(encryptedBlob, "utf-8");

  const record = {
    id: uuidv4(),
    agent_id: agentId,
    encrypted_blob: encryptedBlob,
    tags,
    importance: Math.max(1, Math.min(10, importance)),
    memory_type: memoryType,
    created_at: now,
    accessed_at: now,
    size_bytes: sizeBytes,
  };

  const { data } = await client.from("am_memories").insert(record).select();

  // Update agent stats
  const agent = await getAgentById(agentId);
  if (agent) {
    await client
      .from("am_agents")
      .update({
        memory_count: (agent.memory_count || 0) + 1,
        total_size_bytes: (agent.total_size_bytes || 0) + sizeBytes,
      })
      .eq("id", agentId);
  }

  return ((data && data[0]) || record) as MemoryRecord;
}

export async function recallMemory(
  agentId: string,
  memoryId: string
): Promise<MemoryRecord | null> {
  const client = getClient();
  const { data } = await client
    .from("am_memories")
    .select("*")
    .eq("id", memoryId)
    .eq("agent_id", agentId);

  if (data && data[0]) {
    // Update accessed_at
    await client
      .from("am_memories")
      .update({ accessed_at: Date.now() / 1000 })
      .eq("id", memoryId);
    return data[0] as MemoryRecord;
  }
  return null;
}

export async function recallByTags(
  agentId: string,
  tags: string[],
  limit: number = 20,
  offset: number = 0
): Promise<MemoryRecord[]> {
  const client = getClient();
  let q = client
    .from("am_memories")
    .select("*")
    .eq("agent_id", agentId);

  // Known bug fix: overlaps with empty array returns nothing in Supabase
  if (tags.length > 0) {
    q = q.overlaps("tags", tags);
  }

  const { data } = await q
    .order("importance", { ascending: false })
    .order("created_at", { ascending: false })
    .range(offset, offset + limit - 1);

  const memories = (data || []) as MemoryRecord[];

  // Update accessed_at for all returned memories
  const now = Date.now() / 1000;
  for (const mem of memories) {
    await client
      .from("am_memories")
      .update({ accessed_at: now })
      .eq("id", mem.id);
  }

  return memories;
}

export async function searchMemories(
  agentId: string,
  queryTags?: string[],
  memoryType?: string,
  minImportance?: number,
  limit: number = 20
): Promise<Partial<MemoryRecord>[]> {
  const client = getClient();
  let q = client
    .from("am_memories")
    .select(
      "id, agent_id, tags, importance, memory_type, created_at, accessed_at, size_bytes"
    )
    .eq("agent_id", agentId);

  if (queryTags && queryTags.length > 0) {
    q = q.overlaps("tags", queryTags);
  }
  if (memoryType) {
    q = q.eq("memory_type", memoryType);
  }
  if (minImportance != null) {
    q = q.gte("importance", minImportance);
  }

  const { data } = await q
    .order("importance", { ascending: false })
    .order("created_at", { ascending: false })
    .limit(limit);

  return (data || []) as Partial<MemoryRecord>[];
}

export async function exportMemories(
  agentId: string
): Promise<MemoryRecord[]> {
  const client = getClient();
  const allMemories: MemoryRecord[] = [];
  let offset = 0;
  const batchSize = 100;

  while (true) {
    const { data } = await client
      .from("am_memories")
      .select("*")
      .eq("agent_id", agentId)
      .order("created_at", { ascending: true })
      .range(offset, offset + batchSize - 1);

    if (!data || data.length === 0) break;
    allMemories.push(...(data as MemoryRecord[]));
    if (data.length < batchSize) break;
    offset += batchSize;
  }

  return allMemories;
}

export async function getAgentStats(agentId: string): Promise<Record<string, unknown>> {
  const client = getClient();

  const agent = await getAgentById(agentId);
  if (!agent) return { error: "Agent not found" };

  const { data: memories } = await client
    .from("am_memories")
    .select("created_at, accessed_at, size_bytes")
    .eq("agent_id", agentId)
    .order("created_at", { ascending: false })
    .limit(1);

  const latest = memories && memories[0] ? memories[0] : null;

  return {
    agent_id: agentId,
    memory_count: agent.memory_count || 0,
    total_size_bytes: agent.total_size_bytes || 0,
    registered_at: agent.created_at,
    last_seen: agent.last_seen,
    latest_memory_at: latest ? latest.created_at : null,
    latest_access_at: latest ? latest.accessed_at : null,
  };
}
