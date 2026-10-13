export interface Message { id: string; role: 'user' | 'assistant'; content: string; step?: number; streaming?: boolean }
export interface ToolEntry { id: string; name: string; input: Record<string, any>; status: 'running' | 'success' | 'error'; output?: string; filePath?: string; step: number }
export interface PermRequest { requestId: string; tool: string; path: string; action: string }
export interface AskRequest { requestId: string; prompt: string }

