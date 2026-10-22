"""ACP (Agent Client Protocol) server over stdio.

Implements the subset an editor needs to drive this agent:

``initialize``, ``session/new``, ``session/load``, ``session/prompt`` and the
``session/cancel`` notification. Agent text is streamed back as
``session/update`` notifications carrying ``agent_message_chunk``.

The transport is newline-delimited JSON-RPC 2.0 on stdin/stdout. Prompts run on
a worker thread so a ``session/cancel`` arriving mid-turn is still read and
applied; every write is serialized behind one lock.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from typing import Any, Callable, Optional, TextIO

PROTOCOL_VERSION = 1
STOP_END_TURN = "end_turn"
STOP_CANCELLED = "cancelled"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class AcpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class AcpSession:
    """One live agent bound to an ACP session id."""

    def __init__(self, session_id: str, cwd: str, agent: Any, on_event: Callable[[Any], None]):
        self.id = session_id
        self.cwd = cwd
        self.agent = agent
        self._cancelled = threading.Event()
        self._attach_sink(on_event)

    def _attach_sink(self, on_event: Callable[[Any], None]) -> None:
        from core.events import EventSink

        class _Sink(EventSink):
            def emit(self, event) -> None:
                on_event(event)

        sink = _Sink()
        for attribute in ("event_sink", "_event_sink"):
            if hasattr(self.agent, attribute):
                try:
                    setattr(self.agent, attribute, sink)
                except Exception:
                    pass
                return

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def prompt(self, text: str) -> str:
        return str(self.agent.run(text))

    def cancel(self) -> None:
        self._cancelled.set()
        for name in ("interrupt", "cancel", "request_interrupt"):
            method = getattr(self.agent, name, None)
            if callable(method):
                try:
                    method()
                    return
                except Exception:
                    continue
        flag = getattr(self.agent, "_interrupt_requested", None)
        if flag is not None and hasattr(flag, "set"):
            flag.set()


class AcpServer:
    def __init__(
        self,
        factory: Callable[[str], Any],
        *,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
        permission_timeout_s: float = 300.0,
    ):
        self._factory = factory
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._write_lock = threading.Lock()
        self._sessions: dict[str, AcpSession] = {}
        self._sessions_lock = threading.Lock()
        self._permission_timeout_s = max(0.01, float(permission_timeout_s))
        self._pending: dict[Any, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._next_request_id = 1000

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _respond(self, msg_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------------------
    # Client-bound requests (permission approval)
    # ------------------------------------------------------------------
    def _resolve_pending(self, request_id: Any, message: dict[str, Any]) -> None:
        with self._pending_lock:
            entry = self._pending.get(request_id)
        if entry is None:
            return
        entry["result"] = message.get("result")
        entry["error"] = message.get("error")
        entry["event"].set()

    def _request_client(
        self, method: str, params: dict[str, Any], timeout_s: float,
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        with self._pending_lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            entry: dict[str, Any] = {
                "event": threading.Event(), "result": None, "error": None,
            }
            self._pending[request_id] = entry
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        completed = entry["event"].wait(timeout_s)
        with self._pending_lock:
            self._pending.pop(request_id, None)
        return (True, entry) if completed else (False, None)

    def _ask_permission(
        self, session_id: str, description: str, tool_name: str, action: str,
    ) -> str:
        """Bridge one tool permission prompt to the editor; deny by default."""
        params = {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": f"perm-{uuid.uuid4().hex[:8]}",
                "title": f"{tool_name}: {action}",
                "kind": "other",
                "status": "pending",
                "content": [{"type": "text", "text": str(description)[:2000]}],
            },
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        }
        completed, entry = self._request_client(
            "session/request_permission", params, self._permission_timeout_s,
        )
        if not completed or entry is None or entry.get("error"):
            return "denied"
        outcome = ((entry.get("result") or {}).get("outcome") or {})
        if outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow":
            return "granted"
        return "denied"

    def _install_permission_broker(self, session_id: str, agent: Any) -> None:
        """Route gate and host-command approvals to the editor client."""

        def broker(description: str, tool_name: str, action: str) -> str:
            return self._ask_permission(session_id, description, tool_name, action)

        gate = getattr(agent, "_permission_gate", None)
        if gate is not None:
            try:
                gate._broker = broker
            except Exception:
                pass
        registry = getattr(agent, "tool_registry", None)
        get_tool = getattr(registry, "get_tool", None)
        bash = get_tool("Bash") if callable(get_tool) else None
        if bash is not None and hasattr(bash, "_command_approval_broker"):
            bash._command_approval_broker = broker

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    def serve(self) -> None:
        for raw in self._in:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._error(None, PARSE_ERROR, "invalid JSON")
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") == "session/prompt" and message.get("id") is not None:
                threading.Thread(
                    target=self.handle, args=(message,), name="acp-prompt", daemon=True,
                ).start()
            else:
                self.handle(message)

    def handle(self, message: dict[str, Any]) -> Any:
        method = message.get("method")
        msg_id = message.get("id")
        if not isinstance(method, str):
            # A response from the client (e.g. a permission decision).
            if msg_id is not None:
                self._resolve_pending(msg_id, message)
            return None
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            result = self._dispatch(method, params)
        except AcpError as exc:
            if msg_id is not None:
                self._error(msg_id, exc.code, str(exc))
            return None
        except Exception as exc:  # keep the server alive on adapter bugs
            if msg_id is not None:
                self._error(msg_id, INTERNAL_ERROR, f"internal error: {exc}")
            return None
        if msg_id is not None:
            self._respond(msg_id, result)
        return result

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "session/new":
            return self._session_new(params)
        if method == "session/load":
            return self._session_load(params)
        if method == "session/prompt":
            return self._session_prompt(params)
        if method == "session/cancel":
            self._session_cancel(params)
            return None
        if method in {"authenticate", "logout"}:
            return {}
        raise AcpError(METHOD_NOT_FOUND, f"unknown method: {method}")

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------
    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, int) else PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": True,
                },
            },
        }

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise AcpError(INVALID_PARAMS, "session/new requires a cwd")
        session_id = uuid.uuid4().hex
        agent = self._factory(cwd)
        session = AcpSession(
            session_id, cwd, agent,
            on_event=lambda event: self._forward_event(session_id, event),
        )
        self._install_permission_broker(session_id, agent)
        with self._sessions_lock:
            self._sessions[session_id] = session
        return {"sessionId": session_id}

    def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or session_id not in self._sessions:
            raise AcpError(INVALID_PARAMS, "unknown sessionId")
        return {}

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            raise AcpError(INVALID_PARAMS, "unknown sessionId")
        text = self._prompt_text(params.get("prompt"))
        if not text.strip():
            raise AcpError(INVALID_PARAMS, "prompt contains no text content")
        try:
            session.prompt(text)
        except Exception as exc:
            raise AcpError(INTERNAL_ERROR, f"agent failed: {exc}") from exc
        return {"stopReason": STOP_CANCELLED if session.cancelled else STOP_END_TURN}

    def _session_cancel(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is not None:
            session.cancel()

    @staticmethod
    def _prompt_text(prompt: Any) -> str:
        if isinstance(prompt, str):
            return prompt
        if not isinstance(prompt, list):
            return ""
        parts: list[str] = []
        for block in prompt:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    def _forward_event(self, session_id: str, event: Any) -> None:
        from core.events import EventType

        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", {}) or {}
        if event_type == EventType.ASSISTANT_FINAL:
            content = payload.get("content")
            if isinstance(content, str) and content:
                self._notify("session/update", {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": content},
                    },
                })
        elif event_type == EventType.TOOL_STARTED:
            tool_name = str(payload.get("tool_name") or payload.get("name") or "tool")
            self._notify("session/update", {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": str(payload.get("tool_call_id") or payload.get("id") or tool_name),
                    "title": tool_name,
                    "status": "in_progress",
                },
            })
        elif event_type == EventType.TOOL_COMPLETED:
            tool_name = str(payload.get("tool_name") or payload.get("name") or "tool")
            self._notify("session/update", {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": str(payload.get("tool_call_id") or payload.get("id") or tool_name),
                    "status": "completed",
                },
            })

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            close = getattr(session.agent, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def default_agent_factory(cwd: str) -> Any:
    """Build a local CodeAgent for the given workspace."""
    from agents.codeAgent import CodeAgent
    from core.llm import HelloAgentsLLM
    from tools.registry import ToolRegistry

    return CodeAgent(
        name="acp",
        llm=HelloAgentsLLM(),
        tool_registry=ToolRegistry(),
        project_root=cwd,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ridgecode acp", description="ACP server over stdio")
    parser.add_argument("--cwd", default=".", help="Default workspace root for new sessions")
    args = parser.parse_args(argv)

    server = AcpServer(default_agent_factory)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
