// API client — HTTP + WebSocket for MyCodeAgent service.

const BASE = '/api';

// ── Types (shared with backend) ──────────────────────────────────────

export interface SessionInfo {
  id: string;
  title: string;
  busy: boolean;
}

export interface AgentEvent {
  type: string;
  payload: Record<string, any>;
  step: number;
}

export interface FileEntry {
  name: string;
  path: string;
  type: 'file' | 'directory';
}

export interface FileTree {
  path: string;
  entries: FileEntry[];
  truncated: boolean;
}

export interface FileContent {
  path: string;
  content: string;
  truncated: boolean;
}

export interface ModelInfo {
  name: string;
  model: string;
  provider: string;
  base_url: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  parameters: { name: string; type: string; description: string; required: boolean }[];
}

export interface McpServerCfg {
  name: string;
  command: string;
  args: string[];
}

export interface McpStatus {
  servers: { name: string; connected: boolean; tool_count: number }[];
  pending: string[];
  connect_mode: string;
}

export interface SkillInfo {
  name: string;
  description: string;
  base_dir: string;
}

export interface SkillDetail {
  name: string;
  description: string;
  content: string;
  frontmatter: Record<string, string>;
}

// ── HTTP wrappers ────────────────────────────────────────────────────

async function req<T>(method: string, path: string, body?: any): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  };
  const res = await fetch(BASE + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ── Sessions ─────────────────────────────────────────────────────────

export const sessions = {
  list:   ()              => req<SessionInfo[]>('GET', '/sessions'),
  create: (title?: string) => req<SessionInfo>('POST', '/sessions', { title: title || '' }),
  get:    (id: string)    => req<SessionInfo>('GET', `/sessions/${id}`),
  delete: (id: string)    => req<void>('DELETE', `/sessions/${id}`),
  send:   (id: string, content: string) =>
    req<{ status: string }>('POST', `/sessions/${id}/messages`, { content }),
  interrupt: (id: string) =>
    req<{ status: string }>('POST', `/sessions/${id}/interrupt`),
  resolvePerm: (sid: string, rid: string, decision: string) =>
    req<{ status: string }>('POST', `/sessions/${sid}/permissions/${rid}/resolve`, { decision }),
  answerAsk: (sid: string, rid: string, answer: string) =>
    req<{ status: string }>('POST', `/sessions/${sid}/ask-user/${rid}/answer`, { answer }),
};

// ── Files ────────────────────────────────────────────────────────────

export const files = {
  tree:    (sid: string, path = '.', limit = 200) =>
    req<FileTree>('GET', `/sessions/${sid}/files?path=${encodeURIComponent(path)}&limit=${limit}`),
  content: (sid: string, path: string, start = 1, limit = 500) =>
    req<FileContent>('GET', `/sessions/${sid}/files/content?path=${encodeURIComponent(path)}&start_line=${start}&limit=${limit}`),
};

// ── Info ─────────────────────────────────────────────────────────────

export const info = {
  models: () => req<ModelInfo[]>('GET', '/models'),
  tools:  () => req<ToolInfo[]>('GET', '/tools'),
  mcpStatus: () => req<McpStatus>('GET', '/mcp/status'),
  health: () => req<{ status: string; uptime: number; sessions: number }>('GET', '/health'),
};

// ── Skills ───────────────────────────────────────────────────────────

export const skills = {
  list:    ()                          => req<SkillInfo[]>('GET', '/skills'),
  get:     (name: string)              => req<SkillDetail>('GET', `/skills/${encodeURIComponent(name)}/content`),
  create:  (name: string, description: string, content: string) =>
    req<{ status: string }>('POST', '/skills', { name, description, content }),
  update:  (name: string, data: { description?: string; content?: string }) =>
    req<{ status: string }>('PUT', `/skills/${encodeURIComponent(name)}`, data),
  delete:  (name: string) =>
    req<{ status: string }>('DELETE', `/skills/${encodeURIComponent(name)}`),
};

// ── MCP servers ──────────────────────────────────────────────────────

export const mcp = {
  list:   () => req<McpServerCfg[]>('GET', '/mcp/servers'),
  create: (name: string, command: string, args: string[]) =>
    req<{ status: string }>('POST', '/mcp/servers', { name, command, args }),
  update: (name: string, command?: string, args?: string[]) =>
    req<{ status: string }>('PUT', `/mcp/servers/${encodeURIComponent(name)}`, { command, args }),
  delete: (name: string) =>
    req<{ status: string }>('DELETE', `/mcp/servers/${encodeURIComponent(name)}`),
};

// ── WebSocket ────────────────────────────────────────────────────────

export function connectStream(
  sessionId: string,
  onEvent: (e: AgentEvent) => void,
  onDisconnect: () => void,
): () => void {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/api/sessions/${sessionId}/stream`;
  const ws = new WebSocket(wsUrl);
  let closed = false;

  ws.onmessage = (msg) => {
    try {
      const event: AgentEvent = JSON.parse(msg.data);
      onEvent(event);
    } catch { /* ignore malformed */ }
  };
  ws.onclose = () => { if (!closed) onDisconnect(); };
  ws.onerror = () => { if (!closed) onDisconnect(); };

  return () => { closed = true; ws.close(); };
}
