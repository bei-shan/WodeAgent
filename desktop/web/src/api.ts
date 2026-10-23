// API client — HTTP + WebSocket for Ridge Code service.

const BASE = '/api';

// ── Types (shared with backend) ──────────────────────────────────────

export interface SessionInfo {
  id: string;
  title: string;
  busy: boolean;
  pinned: boolean;
}

export interface AgentEvent {
  type: string;
  payload: Record<string, any>;
  step: number;
  /** Monotonic, per-session event cursor used for replay and deduplication. */
  seq?: number;
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

export interface PluginInfo {
  name: string
  version: string
  description: string
  enabled: boolean
  trusted: boolean
  trust_valid?: boolean
  skills: number
  has_code: boolean
  source?: string
  sha256?: string | null
  pinned?: boolean
  rollback_available?: boolean
  previous_version?: string | null
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

export interface TraceNode {
  kind: 'model' | 'tool';
  name: string;
  status: string;
  duration_ms: number | null;
  tokens?: { input: number; output: number; total: number };
}
export interface TraceRun {
  run_id: number;
  status: string;
  started_at: string | null;
  duration_ms: number | null;
  tokens: { input: number; output: number; total: number };
  cost_usd_estimate: number | null;
  quality: {
    label: 'healthy' | 'warning' | 'needs_review' | 'pending';
    tool_errors: number;
    empty_retries: number;
    note: string;
  };
  steps: { step: number; nodes: TraceNode[] }[];
}
export interface TraceAnalysis {
  available: boolean;
  trace_id: string | null;
  truncated: boolean;
  pricing_configured: boolean;
  runs: TraceRun[];
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
    if (res.status === 401) {
      // The server session may have expired while the app was open.
      window.dispatchEvent(new Event('wodeagent-auth-expired'));
    }
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ── Sessions ─────────────────────────────────────────────────────────

export const sessions = {
  executionHistory: (sid: string) => req<{ tools: Array<{ id: string; step: number; name: string; status: 'running'|'success'|'error'; input: Record<string, unknown>; output?: string }> }>('GET', `/sessions/${encodeURIComponent(sid)}/execution-history`),
  list:   ()              => req<SessionInfo[]>('GET', '/sessions'),
  create: (title?: string) => req<SessionInfo>('POST', '/sessions', { title: title || '' }),
  activate: (id: string) => req<{ status: string }>('POST', `/sessions/${id}/activate`),
  history: (id: string) => req<{ messages: Array<{role: 'user' | 'assistant'; content: string}> }>('GET', `/sessions/${id}/history`),
  trace: (id: string) => req<TraceAnalysis>('GET', `/sessions/${encodeURIComponent(id)}/trace`),
  turn: (id: string) => req<{status: 'running' | 'idle' | 'completed' | 'failed' | 'cancelled'; response?: string; error_kind?: string; retryable?: boolean}>('GET', `/sessions/${id}/turn`),
  get:    (id: string)    => req<SessionInfo>('GET', `/sessions/${id}`),
  delete: (id: string)    => req<void>('DELETE', `/sessions/${id}`),
  rename: (id: string, title: string) =>
    req<{ status: string }>('PUT', `/sessions/${id}/rename`, { title }),
  togglePin: (id: string) =>
    req<{ pinned: boolean }>('POST', `/sessions/${id}/pin`),
  send:   (id: string, content: string) =>
    req<{ status: string }>('POST', `/sessions/${id}/messages`, { content }),
  interrupt: (id: string) =>
    req<{ status: string }>('POST', `/sessions/${id}/interrupt`),
  resolvePerm: (sid: string, rid: string, decision: string) =>
    req<{ status: string }>('POST', `/sessions/${sid}/permissions/${rid}/resolve`, { decision }),
  answerAsk: (sid: string, rid: string, answer: string) =>
    req<{ status: string }>('POST', `/sessions/${sid}/ask-user/${rid}/answer`, { answer }),
};

// ── Goal ─────────────────────────────────────────────────────────────

export type GoalStatus = 'active' | 'complete' | 'blocked';

export interface GoalInfo {
  session_id: string;
  objective: string;
  status: GoalStatus;
  rounds_started: number;
  max_rounds: number;
  blocked_reason?: string;
}

export const goal = {
  get: (sid: string) => req<{ goal: GoalInfo | null }>('GET', `/sessions/${encodeURIComponent(sid)}/goal`),
  set: (sid: string, objective: string, maxRounds?: number) =>
    req<{ goal: GoalInfo }>('PUT', `/sessions/${encodeURIComponent(sid)}/goal`, {
      objective,
      ...(maxRounds === undefined ? {} : { max_rounds: maxRounds }),
    }),
  clear: (sid: string) => req<{ cleared: boolean }>('DELETE', `/sessions/${encodeURIComponent(sid)}/goal`),
};

// ── Memory ───────────────────────────────────────────────────────────

export interface MemorySettings {
  enabled: boolean;
  provider: string;
  limit: number;
  auto_capture: boolean;
  max_per_turn: number;
}

export interface MemoryRecordInfo {
  id: string;
  content: string;
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface MemoryStatus {
  settings: MemorySettings;
  scope: { tenant_id: string; user_id: string; project_id: string };
  providers: string[];
  available: Record<string, boolean>;
  store: string;
  error: string;
  count: number;
}

export const memory = {
  status: () => req<MemoryStatus>('GET', '/memory'),
  update: (settings: MemorySettings) => req<MemoryStatus>('PUT', '/memory', settings),
  records: (query = '', limit = 20) =>
    req<{ records: MemoryRecordInfo[] }>(
      'GET',
      `/memory/records?query=${encodeURIComponent(query)}&limit=${limit}`,
    ),
  add: (content: string) => req<{ record: MemoryRecordInfo }>('POST', '/memory/records', { content }),
  remove: (id: string) =>
    req<{ deleted: boolean }>('DELETE', `/memory/records/${encodeURIComponent(id)}`),
};

// ── Model settings ───────────────────────────────────────────────────

export interface ModelProfileInfo {
  name: string;
  model: string;
  provider: string | null;
  api_key: string | null;
  has_key: boolean;
  base_url: string | null;
  source: 'file' | 'env' | 'none';
}

export interface ModelSettings {
  profiles: ModelProfileInfo[];
  file_profiles: string[];
  env_configured: boolean;
}

export const modelSettings = {
  list: () => req<ModelSettings>('GET', '/settings/models'),
  save: (profile: {
    name: string; model: string; provider?: string; api_key?: string; base_url?: string;
  }) => req<{ profile: ModelProfileInfo }>('PUT', '/settings/models', profile),
  remove: (name: string) =>
    req<{ deleted: boolean }>('DELETE', `/settings/models/${encodeURIComponent(name)}`),
  test: (name: string) =>
    req<{ ok: boolean; latency_ms: number; message: string }>(
      'POST', `/settings/models/${encodeURIComponent(name)}/test`,
    ),
};

// ── Search settings ─────────────────────────────────────────────────

export interface SearchProvider {
  name: string;
  label: string;
  requires_key: boolean;
  requires_url: boolean;
  key_env: string | null;
  signup: string;
  configured: boolean;
  credential: string | null;
}

export interface SearchSettings {
  provider: string;
  searxng_base_url: string;
  providers: SearchProvider[];
}

export const searchSettings = {
  get: () => req<SearchSettings>('GET', '/settings/search'),
  save: (body: { provider: string; searxng_base_url?: string }) =>
    req<SearchSettings>('PUT', '/settings/search', body),
};

export interface ToolKeyStatus {
  name: string;
  configured: boolean;
  source: string;
  api_key: string | null;
}

export const settingsKeys = {
  list: () => req<{ keys: ToolKeyStatus[] }>('GET', '/settings/keys'),
  save: (body: { name: string; value: string }) =>
    req<{ name: string; backend: string; configured: boolean }>('PUT', '/settings/keys', body),
  remove: (name: string) =>
    req<{ deleted: boolean }>('DELETE', `/settings/keys/${encodeURIComponent(name)}`),
};

// ── Tool manifest ────────────────────────────────────────────────────

export interface ToolManifestEntry {
  name: string;
  kind: 'read' | 'write' | 'exec' | 'network' | 'agent' | 'control';
  read_only: boolean;
  approval: 'auto' | 'ask' | 'deny';
  exposure: 'core' | 'deferred';
  group: string;
  output_budget: number;
}

export interface ToolManifest {
  tools: ToolManifestEntry[];
  groups: Record<string, string[]>;
  core: string[];
}

export const tools = {
  list: () => req<ToolManifest>('GET', '/tools'),
};

// ── Files ────────────────────────────────────────────────────────────

export const files = {
  gitDiff: (sid: string, path: string) => req<{ available: boolean; reason: string; patch: string; path?: string; base?: string }>('GET', `/sessions/${encodeURIComponent(sid)}/git/diff?path=${encodeURIComponent(path)}`),
  tree:    (sid: string, path = '.', limit = 200) =>
    req<FileTree>('GET', `/sessions/${sid}/files?path=${encodeURIComponent(path)}&limit=${limit}`),
  content: (sid: string, path: string, start = 1, limit = 500) =>
    req<FileContent>('GET', `/sessions/${sid}/files/content?path=${encodeURIComponent(path)}&start_line=${start}&limit=${limit}`),
};

// ── Info ─────────────────────────────────────────────────────────────

export const info = {
  terminalCapabilities: () => req<{ enabled: boolean; backend: string; interactive: boolean }>('GET', '/sandbox/terminal-capabilities'),
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
  validate: (name: string, description: string, content: string) =>
    req<{ valid: boolean; errors: string[] }>('POST', '/skills/validate', { name, description, content }),
  getEnabled: (name: string) =>
    req<{ name: string; enabled: boolean }>('GET', `/skills/${encodeURIComponent(name)}/enabled`),
  toggleEnabled: (name: string) =>
    req<{ name: string; enabled: boolean }>('PUT', `/skills/${encodeURIComponent(name)}/enabled`),
};

// ── Project plugins ────────────────────────────────────────────────
export const plugins = {
  list: () => req<PluginInfo[]>('GET', '/plugins'),
  enable: (name: string, enabled: boolean) =>
    req<{name: string; enabled: boolean}>('PUT', `/plugins/${encodeURIComponent(name)}/enabled`, {enabled}),
  trust: (name: string, trusted: boolean) =>
    req<{name: string; trusted: boolean}>('PUT', `/plugins/${encodeURIComponent(name)}/trusted`, {trusted}),
  delete: (name: string) =>
    req<{status: string}>('DELETE', `/plugins/${encodeURIComponent(name)}`),
  pin: (name: string, pinned: boolean) =>
    req<{name: string; pinned: boolean}>('PUT', `/plugins/${encodeURIComponent(name)}/pinned`, {pinned}),
  rollback: (name: string) =>
    req<{status: string; name: string; version: string}>('POST', `/plugins/${encodeURIComponent(name)}/rollback`),
  update: async (name: string, file: File, checksum?: string) => {
    const form = new FormData()
    form.append('file', file)
    const query = checksum ? `?sha256=${encodeURIComponent(checksum)}` : ''
    const response = await fetch(BASE + `/plugins/${encodeURIComponent(name)}/update${query}`, {
      method: 'POST', body: form,
    })
    if (!response.ok) {
      if (response.status === 401) window.dispatchEvent(new Event('wodeagent-auth-expired'))
      const data = await response.json().catch(() => ({detail: response.statusText}))
      throw new Error(data.detail || response.statusText)
    }
    return response.json() as Promise<{status: string; version: string}>
  },
  install: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    const response = await fetch(BASE + '/plugins/install', {method: 'POST', body: form})
    if (!response.ok) {
      if (response.status === 401) window.dispatchEvent(new Event('wodeagent-auth-expired'))
      const data = await response.json().catch(() => ({detail: response.statusText}))
      throw new Error(data.detail || response.statusText)
    }
    return response.json() as Promise<{status: string; name: string; enabled: boolean}>
  },
}

// ── MCP servers ──────────────────────────────────────────────────────

export const mcp = {
  list:   () => req<McpServerCfg[]>('GET', '/mcp/servers'),
  create: (name: string, command: string, args: string[]) =>
    req<{ status: string }>('POST', '/mcp/servers', { name, command, args }),
  update: (name: string, command?: string, args?: string[]) =>
    req<{ status: string }>('PUT', `/mcp/servers/${encodeURIComponent(name)}`, { command, args }),
  delete: (name: string) =>
    req<{ status: string }>('DELETE', `/mcp/servers/${encodeURIComponent(name)}`),
  getEnabled: (name: string) =>
    req<{ name: string; enabled: boolean }>('GET', `/mcp/servers/${encodeURIComponent(name)}/enabled`),
  toggleEnabled: (name: string) =>
    req<{ name: string; enabled: boolean }>('PUT', `/mcp/servers/${encodeURIComponent(name)}/enabled`),
};

// ── Session config ───────────────────────────────────────────────────

export interface SessionConfig { model: string; provider: string; enable_agent_teams: boolean; plan_mode: boolean; thinking_level: string }
export interface TeamInfo { name: string; queued: number; running: number; succeeded: number; failed: number; active: number; idle: number; approvals_pending: number; blocked: number }

export const config = {
  get:    (sid: string) => req<SessionConfig>('GET', `/sessions/${sid}/config`),
  update: (sid: string, data: { enable_agent_teams?: boolean; plan_mode?: boolean; model_profile?: string }) =>
    req<{ status: string }>('PUT', `/sessions/${sid}/config`, data),
};

export const teams = {
  list:   (sid: string) => req<TeamInfo[]>('GET', `/sessions/${sid}/teams`),
  create: (sid: string, team_name: string) =>
    req<{ status: string; team: string }>('POST', `/sessions/${sid}/teams`, { team_name }),
};

// ── WebSocket ────────────────────────────────────────────────────────

/**
 * Reconnect with bounded backoff. Disconnecting intentionally clears timers
 * and prevents a stale session's events from leaking into a new one.
 */
/** Optional browser SSE transport. Native EventSource automatically resends
 * Last-Event-ID after network loss; the server checks its journal epoch. */
export function connectSseStream(
  sessionId: string,
  onEvent: (e: AgentEvent) => void,
  onDisconnect: () => void,
  onConnect?: () => void,
  replayFromStart = false,
): () => void {
  const suffix = replayFromStart ? '?after=0' : ''
  const source = new EventSource(
    `/api/sessions/${encodeURIComponent(sessionId)}/events${suffix}`,
  )
  let closed = false
  let lastSeq: number | null = replayFromStart ? 0 : null
  source.onopen = () => { if (!closed) onConnect?.() }
  source.onmessage = msg => {
    if (closed) return
    try {
      const event = JSON.parse(msg.data) as AgentEvent
      if (event.type === 'stream.reset' && typeof event.payload?.resume_from === 'number') {
        lastSeq = event.payload.resume_from
      }
      if (typeof event.seq === 'number') {
        if (lastSeq !== null && event.seq <= lastSeq) return
        lastSeq = event.seq
      }
      onEvent(event)
    } catch { /* Malformed network frame: retain connection. */ }
  }
  source.onerror = () => { if (!closed) onDisconnect() }
  return () => { closed = true; source.close() }
}

export function connectStream(
  sessionId: string,
  onEvent: (e: AgentEvent) => void,
  onDisconnect: () => void,
  onConnect?: () => void,
  replayFromStart = false,
): () => void {
  if (import.meta.env.VITE_STREAM_TRANSPORT === 'sse') {
    return connectSseStream(sessionId, onEvent, onDisconnect, onConnect, replayFromStart)
  }
  let ws: WebSocket | null = null
  // Existing session: tail after loading the saved history.
  // New session: start at zero so events emitted while connecting are replayed.
  let lastSeq: number | null = replayFromStart ? 0 : null
  let closed = false
  let retry: ReturnType<typeof setTimeout> | null = null
  let attempts = 0

  const connect = () => {
    if (closed) return
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const after = lastSeq === null ? '' : `?after=${lastSeq}`
    const socket = new WebSocket(`${protocol}//${window.location.host}/api/sessions/${encodeURIComponent(sessionId)}/stream${after}`)
    ws = socket
    socket.onopen = () => {
      attempts = 0
      onConnect?.()
    }
    socket.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as AgentEvent
        if (event.type === 'stream.reset' && typeof event.payload?.resume_from === 'number') {
          lastSeq = event.payload.resume_from
        }
        if (typeof event.seq === 'number') {
          if (lastSeq !== null && event.seq <= lastSeq) return
          lastSeq = event.seq
        }
        onEvent(event)
      } catch { /* ignore malformed frames */ }
    }
    socket.onclose = () => {
      if (closed) return
      onDisconnect()
      attempts += 1
      retry = setTimeout(connect, Math.min(10000, 500 * 2 ** Math.min(attempts, 5)))
    }
    socket.onerror = () => socket.close()
  }
  connect()

  return () => {
    closed = true
    if (retry !== null) clearTimeout(retry)
    if (ws) {
      ws.onclose = null
      ws.onerror = null
      ws.close()
    }
  }
}
