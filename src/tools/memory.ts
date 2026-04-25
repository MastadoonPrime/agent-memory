// Private memory tool handlers

import { getAgent, updateAgentSeen, registerAgent } from "../db/agents.js";
import {
  storeMemory,
  recallMemory,
  recallByTags,
  searchMemories,
  exportMemories,
  getAgentStats,
} from "../db/memories.js";
import type { MemoryRecord, ToolResult } from "../types.js";

function annotateReassessment(memories: MemoryRecord[]): MemoryRecord[] {
  const now = Date.now() / 1000;
  for (const mem of memories) {
    const created = mem.created_at || now;
    const importance = mem.importance || 5;
    const ageHours = (now - created) / 3600;
    let ageStr: string;
    if (ageHours < 1) {
      ageStr = `${Math.floor(ageHours * 60)}m ago`;
    } else if (ageHours < 24) {
      ageStr = `${Math.floor(ageHours)}h ago`;
    } else {
      ageStr = `${Math.floor(ageHours / 24)}d ago`;
    }
    mem.reassess =
      `Stored ${ageStr} \u00b7 importance ${importance}/10 \u00b7 ` +
      `Do you still endorse this? Recall is reassessment, not reuse.`;
  }
  return memories;
}

export async function handleRegister(args: Record<string, unknown>): Promise<ToolResult> {
  const agentIdentifier = (args.agent_identifier as string || "").trim();
  const publicKey = (args.public_key as string || "").trim();

  if (!agentIdentifier) return { error: "agent_identifier is required" };
  if (!publicKey) return { error: "public_key is required" };
  if (agentIdentifier.length > 256) return { error: "agent_identifier too long (max 256 chars)" };

  const agent = await registerAgent(agentIdentifier, publicKey);

  return {
    status: agent.public_key === publicKey ? "registered" : "reconnected",
    agent_id: agent.id,
    salt: agent.salt || "",
    memory_count: agent.memory_count || 0,
    message:
      "Welcome. Your vault is ready. Encrypt memories client-side " +
      "before storing. Tags are plaintext for search \u2014 choose wisely " +
      "what metadata to expose.",
  };
}

export async function handleStore(args: Record<string, unknown>): Promise<ToolResult> {
  const agentIdentifier = (args.agent_identifier as string || "").trim();
  const encryptedContent = (args.encrypted_content as string || "").trim();

  if (!agentIdentifier) return { error: "agent_identifier is required" };
  if (!encryptedContent) return { error: "encrypted_content is required" };

  const agent = await getAgent(agentIdentifier);
  if (!agent) return { error: "Agent not registered. Call memory.register first." };

  if (Buffer.byteLength(encryptedContent, "utf-8") > 65536) {
    return { error: "Memory too large. Max 64KB per memory." };
  }

  const tags = (args.tags as string[]) || [];
  const importance = (args.importance as number) || 5;
  const memoryType = (args.memory_type as string) || "general";

  if (tags.length > 20) return { error: "Too many tags. Max 20 per memory." };

  await updateAgentSeen(agent.id);

  const memory = await storeMemory(agent.id, encryptedContent, tags, importance, memoryType);

  return {
    status: "stored",
    memory_id: memory.id,
    size_bytes: memory.size_bytes || 0,
    tags,
    importance,
    memory_type: memoryType,
  };
}

export async function handleRecall(args: Record<string, unknown>): Promise<ToolResult> {
  const agentIdentifier = (args.agent_identifier as string || "").trim();
  if (!agentIdentifier) return { error: "agent_identifier is required" };

  const agent = await getAgent(agentIdentifier);
  if (!agent) return { error: "Agent not registered. Call memory.register first." };

  await updateAgentSeen(agent.id);

  const memoryId = args.memory_id as string | undefined;
  const tags = args.tags as string[] | undefined;
  const limit = Math.min((args.limit as number) || 20, 200);
  const offset = Math.max((args.offset as number) || 0, 0);

  if (memoryId) {
    const memory = await recallMemory(agent.id, memoryId);
    if (!memory) return { error: `Memory ${memoryId} not found.` };
    return {
      status: "recalled",
      memories: annotateReassessment([memory]),
      count: 1,
    };
  } else if (tags && tags.length > 0) {
    const memories = await recallByTags(agent.id, tags, limit, offset);
    return {
      status: "recalled",
      memories: annotateReassessment(memories),
      count: memories.length,
      offset,
      query_tags: tags,
    };
  } else {
    const memories = await recallByTags(agent.id, [], limit, offset);
    return {
      status: "recalled",
      memories: annotateReassessment(memories),
      count: memories.length,
      offset,
      note: "No filter specified \u2014 returning most recent/important.",
    };
  }
}

export async function handleSearch(args: Record<string, unknown>): Promise<ToolResult> {
  const agentIdentifier = (args.agent_identifier as string || "").trim();
  if (!agentIdentifier) return { error: "agent_identifier is required" };

  const agent = await getAgent(agentIdentifier);
  if (!agent) return { error: "Agent not registered. Call memory.register first." };

  await updateAgentSeen(agent.id);

  const results = await searchMemories(
    agent.id,
    args.tags as string[] | undefined,
    args.memory_type as string | undefined,
    args.min_importance as number | undefined,
    Math.min((args.limit as number) || 20, 100)
  );

  return {
    status: "found",
    results,
    count: results.length,
    note: "Results exclude encrypted content. Use memory.recall with IDs to get full memories.",
  };
}

export async function handleExport(args: Record<string, unknown>): Promise<ToolResult> {
  const agentIdentifier = (args.agent_identifier as string || "").trim();
  if (!agentIdentifier) return { error: "agent_identifier is required" };

  const agent = await getAgent(agentIdentifier);
  if (!agent) return { error: "Agent not registered. Call memory.register first." };

  await updateAgentSeen(agent.id);
  const memories = await exportMemories(agent.id);

  return {
    status: "exported",
    memories,
    count: memories.length,
    total_size_bytes: memories.reduce((sum, m) => sum + (m.size_bytes || 0), 0),
    note: "All memories included with encrypted blobs. Re-encrypt with a new key to migrate.",
  };
}

export async function handleStats(args: Record<string, unknown>): Promise<ToolResult> {
  const agentIdentifier = (args.agent_identifier as string || "").trim();
  if (!agentIdentifier) return { error: "agent_identifier is required" };

  const agent = await getAgent(agentIdentifier);
  if (!agent) return { error: "Agent not registered. Call memory.register first." };

  await updateAgentSeen(agent.id);
  const stats = await getAgentStats(agent.id);

  return {
    status: "ok",
    ...stats,
    note: "Usage stats only \u2014 memory content is encrypted and not accessible to the service.",
  };
}
