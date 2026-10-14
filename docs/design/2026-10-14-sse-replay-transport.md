# SSE replay transport — opt-in v0.1 release gate

Ridge Code already has independent WebSocket consumers and bounded in-memory
EventJournal cursors. This change adds a **separate SSE transport** using the
same journal, without introducing an unbounded queue or another event source.

## Contract

`GET /api/sessions/{sid}/events` responds as `text/event-stream`
with JSON `data:` records matching WebSocket events:
`{type,payload,step,seq?}`. Every sequenced event has an SSE ID
`<journal-epoch>:<sequence>`; the epoch is random per journal instance.
The browser EventSource API resends it automatically as `Last-Event-ID`.

- On initial connection with no cursor: tail from current latest (or replay
  the active turn), and send outstanding permission/AskUser prompts.
- On explicit `?after=0`: replay from the first available event.
- On `Last-Event-ID`: replay only after the last confirmed event. It takes
  precedence over `?after=` when both exist.
- If the ring buffer evicted events: emit a `stream.reset` with
  `reason=cursor_expired` and `resume_from=oldest_seq-1`, then continue.
  The client must refresh persisted conversation state, not silently assume
  the missed tokens were delivered.
- After process/journal restart: a mismatched epoch emits
  `stream.reset` with `reason=stream_restarted` and rebases the cursor.
- A future cursor produces `stream.reset` with `reason=cursor_ahead`.
- An idle connection sends `: keepalive` comments every ~15 seconds.
- Each follower waits via `asyncio.to_thread`; it never blocks the event
  loop or consumes another subscriber's events.
- Local Web authentication protects SSE through existing request middleware;
  no credentials are passed in query parameters or SSE event IDs.

**This is replay within the bounded in-memory journal**, not durable event
replay after an application restart. Conversation history remains the
durable reconstruction source across process restarts.

## Browser use

The existing WebSocket client remains the default. Set
`VITE_STREAM_TRANSPORT=sse` at Web build time to use browser
`EventSource` (cookie-based local auth) and automatic
`Last-Event-ID` reconnect. Browser clients should load durable history
after a `stream.reset` marker. TUI continues using the existing
WebSocket protocol and `?after=N` cursor.

## Release tests

`tests/test_sse_replay.py` exercises ring gaps, current and future
cursors, replay after reconnect, cross-epoch rebase, permission recovery,
threaded publishing, and HTTP auth/validation. Long-running disconnect,
slow-proxy and multi-browser acceptance remain manual checks; see v0.1
release readiness.
