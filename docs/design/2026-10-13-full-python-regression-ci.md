# Python full-suite CI gate

The Web MVP workflow now has two Python gates:

1. API smoke tests for fast feedback on session, stream, file, engine, and
   configuration changes.
2. python-full-regression running all test modules under tests/ with the
   regular requirements installed. This detects stale imports left by
   retiring the Python Rich/prompt_toolkit terminal and catches regressions
   outside the manually maintained smoke list.

The actual Docker engine integration is separately exercised through
docker-sandbox-smoke with AGENT_DOCKER_INTEGRATION=1. Full suite runs
without network API keys; tests requiring external services should skip
explicitly rather than silently use production credentials.

All PRs touching the Web workflow or Python application must pass both
Python gates, browser E2E, Web build and Docker sandbox before merge.
