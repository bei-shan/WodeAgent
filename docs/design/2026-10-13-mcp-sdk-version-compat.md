# MCP transport SDK version compatibility

Complete Python regression collection exposed a real production issue:
requirements.txt previously admitted mcp 2.x while tools/mcp/client.py
imports a v1-only streamablehttp_client symbol removed in SDK v2.

Short-term: support mcp >=1.10 and <2, with a CI transport import contract
test and pip check. This is not a claim that MCP 2.x is supported.

Long-term: migrate to SDK v2 with an httpx2.AsyncClient, its two-stream
return contract and explicit timeout/header handling, then test session
reconnection and cancellation. Do not simply rename an import.

Official migration guide:
https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md
