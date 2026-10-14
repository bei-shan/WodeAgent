"""Minimal, real newline-framed JSON-RPC MCP stdio server.

A deliberately independent protocol fixture: catches lifecycle/loop issues
that mock ClientSession tests cannot, for both Python SDK v1 and v2.
"""
import json
import os
import sys

COUNT = 0
for line in sys.stdin.buffer:
    try:
        req = json.loads(line)
        name = req.get("method", "")
        if "id" not in req:
            continue
        if name == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ridge-stdio-fixture", "version": "1.0.0"},
            }
        elif name == "tools/list":
            result = {"tools": [{
                "name": "ping", "description": "Real stdio echo",
                "inputSchema": {
                    "type": "object", "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }]}
        elif name == "tools/call":
            COUNT += 1
            args = req.get("params", {}).get("arguments", {})
            result = {
                "content": [{
                    "type": "text", "text":
                    "ping:" + str(args.get("value", "")) + ":count:" + str(COUNT)
                    + ":pid:" + str(os.getpid()),
                }],
                "isError": False,
            }
        else:
            result = {}
        response = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception as exc:
        print("MCP fixture error: " + repr(exc), file=sys.stderr, flush=True)
