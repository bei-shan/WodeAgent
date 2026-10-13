# Real PTY portable archive roundtrip

Extend the existing Bun/OpenTUI PTY acceptance test with /export and
/import against a loopback-only fake API. Drive actual keystrokes,
not direct TuiController method calls.

Assert GET export route, 0600 local file and allowlisted archive payload;
assert POST import receives the archive and switches to a new session
through the activate and history requests. Prior tree/fork and clean-exit
assertions remain enabled.

No real LLM/network calls are made in this test.
