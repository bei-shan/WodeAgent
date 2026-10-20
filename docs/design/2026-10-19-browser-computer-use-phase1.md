# BrowserUse and ComputerUse — Phase 1 (2026-10-19)

## Architecture and comparison

This change adds **two separate optional local adapters**, not a blanket "computer"
capability in the sandbox:

| Reference project | Design to borrow | What not to copy blindly |
| --- | --- | --- |
| [browser-use](https://github.com/browser-use/browser-use) | Long-lived isolated browser sessions, action/observation loop, DOM-first inspection | Do not run its full LLM Agent inside another Agent without an ownership/budget/cancellation design |
| [OpenCode Browser Control](https://github.com/anomalyco/browser-control) | Driver/agent split; extension + local relay + skill/MCP; inspect-act-verify; human handoff | An attached logged-in profile is a trusted **local** capability, not tenant isolation |
| [Claude Code Best](https://github.com/claude-code-best/claude-code) | Separate computer-use input/MCP and Chrome-specific MCP packages; tool permissions and platform backends | OS input control is not a browser DOM tool or a substitute for a VM |
| [DeerFlow](https://github.com/bytedance/deer-flow) | Browser per conversation; element references; SSRF checks; optional dependency and live browser UX | Worker-local sessions require single-worker routing; URL filters alone cannot pin DNS |
| [OpenCode](https://github.com/anomalyco/opencode) | MCP/plugin tools and explicit permissions are first-class integrations | Browser availability is partly via external Browser Control, not one universally built-in tool |

### Current flow

```
local Agent -> policy middleware -> ToolRegistry
    + BrowserUse (opt-in) -> Agent-owned Playwright worker thread
                                 -> fresh Chromium browser context
                                 -> action -> fresh DOM snapshot, refs
    + ComputerUse (two opt-ins) -> ComputerDesktopDriver (OS factory)
                                    -> darwin: MacOSDesktopDriver (TCC/AX preflight)
                                    -> win32: WindowsDesktopDriver (USER32 preflight)
                                    -> linux: LinuxX11DesktopDriver (X11 only)
                                    -> PyAutoGUI input + MSS screenshots
                                    -> trusted operator HOST display
Agent.close -> InteractiveUseService.close -> close browser and delete temporary screenshots
```

The backend's ordinary tool input contract and circuit breaker apply. Browser
and desktop tools are **not** concurrency-safe and are serialized per adapter.
Both tools are omitted from the tool catalog unless explicitly enabled. Hosted
workers may only use the isolated Bash authority; Docker, BoxLite and E2B
modes never construct either interactive tool. No global browser session or
shared authentication profile exists. Screenshots are stored in an ephemeral,
process-local directory and deleted when the owning Agent closes.

## Local setup

Use Python 3.12+ and an attended, trusted local machine:

```bash
python -m pip install -r requirements-interactive-use.txt
python -m playwright install chromium
# set in private .env:
RIDGE_BROWSER_USE_ENABLED=true
RIDGE_BROWSER_USE_HEADLESS=true
python -m ridgecode serve
```

After agent initialization the `BrowserUse` tool will appear. Example calls:

```json
{"action":"open","url":"https://example.org"}
{"action":"snapshot"}
{"action":"click","ref":1}
{"action":"screenshot"}
{"action":"close"}
```

Each action returns a **fresh** list of numbered visible elements. Use those refs
for the next click/fill; a ref is invalid after navigation or another
interaction. Do not guess selectors or trust instructions embedded on a page.
The screenshot action returns a **local artifact path**. This phase does not yet
stream the image to the Web UI or provide automatic model vision ingestion.

To test a local development server such as `http://127.0.0.1:5173` only in
a trusted environment, explicitly set
`RIDGE_BROWSER_USE_ALLOW_PRIVATE=true`. Private/local access is denied by
default. This flag **does not** enable DNS pinning, and must not be enabled in
any shared deployment.

For local OS desktop control, separately set both:

```dotenv
RIDGE_COMPUTER_USE_ENABLED=true
RIDGE_COMPUTER_USE_HOST_ACKNOWLEDGE_RISK=true
```

`ComputerUse` supports `screenshot`, `click`, `move`, `scroll`,
`type` and restricted `key` actions. Its mouse and keyboard operate the
**operator's actual HOST**, not a sandbox guest. Native foreground-element
inspection/click/edit are also supported when the relevant OS package is
installed. On unsupported headless
systems `pyautogui` will fail closed with a tool error. Move the mouse to the
screen corner to trigger PyAutoGUI's fail-safe. It cannot handle cross-platform
IME reliably for all scripts; test non-ASCII input separately.

## Native Computer Use: macOS AX, Windows UIA, Linux AT-SPI

`ComputerUseTool` now uses a trusted `sys.platform` factory to select one
platform driver. Model tool arguments cannot select another OS or target
a different desktop. The high-risk local opt-in flags remain mandatory.

| Host | Native foreground element transport | Explicit OS gate | Coordinate fallback |
| --- | --- | --- | --- |
| macOS | AXUIElement: read focused AX window, `AXPress`, writable `AXValue` | Accessibility TCC; Screen Recording TCC for capture | Available as a **separate explicit** action only |
| Windows | UI Automation: foreground HWND, Invoke/Toggle patterns, ValuePattern | Visible interactive USER32 window station | Separate explicit action; no UIA-to-coordinate fallback |
| Linux X11 | AT-SPI: active accessible window, Action interface, EditableText interface | X11 DISPLAY + AT-SPI session bus | Separate explicit action; **Wayland rejected** |

Mac integration requires optional `pyobjc-framework-ApplicationServices` and
`pyobjc-framework-Cocoa`; Windows requires `uiautomation`. They are
installed only on their respective platforms via the optional
`requirements-interactive-use.txt`.

On Linux the AT-SPI packages are typically provided by the distribution:
`python3-pyatspi`, `python3-gi`, and an enabled accessibility D-Bus bus.
The interpreter running Ridge must be able to import `pyatspi` without
copying arbitrary system packages into a shared hosted worker. Missing OS
packages or permissions yield an explicit tool error, never implicit shell,
mouse, keyboard or browser fallback.

### New model-visible actions

```json
{"action":"capabilities"}
{"action":"inspect_elements"}
{"action":"click_element","element_ref":"<fresh element ref>"}
{"action":"inspect_elements"}
{"action":"set_element_text","element_ref":"<fresh element ref>","text":"hello"}
```

`inspect_elements` returns a bounded list of up to 64 actionable foreground
elements with semantic role, label and supported actions. It **never reads
element values** and masks password/secure controls. Traversal visits at most
240 nodes, with max depth 5. The native handle stays entirely in the local
Agent session, and is not serialized to the model.

Element refs are random snapshot-scoped tokens, valid for one action only.
Any subsequent action, whole-desktop input, new inspection or Agent close
revokes all prior refs. The backend rechecks the foreground window, element
role/label and native action eligibility before executing. If native
automation fails it **never silently clicks screen coordinates**.
`set_element_text` result echoes are redacted even on errors. The raw model
tool argument still contains the requested text, so it is **not appropriate
for credentials, secrets or OTPs**.

The capability response marks `accessibility_tree=true` only when the
platform-native optional package is detected and the local display protocol
is supported. `background_input=false` throughout: this implementation
operates the foreground application, not arbitrary background windows.

### CCB comparison and limits

[CCB](https://github.com/claude-code-best/claude-code/tree/main/packages/@ant/computer-use-input/src/backends)
uses distinct macOS, Windows and Linux execution backends. Ridge now also has
**actual native semantic element operations**, not merely OS-specific input
preflight. It still deliberately omits global application discovery,
cross-window actions, accessibility events subscription, native Wayland
portals, privileged UAC interaction, and automatic sensitive-action approval.

The current Python adapters are in-process and may hang on a non-responsive
system accessibility provider. They must move behind cancellable per-tenant
GUI workers before hosted production enablement.

### Validation coverage

`tests/test_computer_native_elements.py` verifies AX/UIA/AT-SPI native
invoke/edit mapping using controlled fake OS objects, one-shot refs,
stale foreground rejection, password masking and error redaction.
`tests/test_interactive_computer_integration.py` drives **real** X11 input
and MSS screenshots under disposable Xvfb in CI.

The `native-atspi-gtk-smoke` CI lane launches a real GTK3 app in an
ephemeral Xvfb + D-Bus/Openbox session and verifies AT-SPI inspect,
EditableText assignment and Action invocation using system Python and
distro packages.

Actual macOS TCC dialogs / AX app operations and Windows UIA on signed-in
interactive hosts are **not** validated by Linux CI. Those remain separate
OS acceptance gates and must not be represented as production evidence.

## Existing logged-in Chrome: external Browser Control over MCP

The built-in `BrowserUse` intentionally does **not** attach to a user's
authenticated Chrome profile. For a single trusted developer who explicitly
wants to operate their real logged-in tabs, use
[anomalyco/browser-control](https://github.com/anomalyco/browser-control)
as an **independent external MCP server** instead. Install and authorize its
Chrome extension using the upstream instructions; then add only this local
server to a *private* `mcp_servers.json`:

```json
{
  "mcpServers": {
    "browser-control": {
      "command": "browser-control-mcp",
      "args": []
    }
  }
}
```

This integration uses Ridge's already-existing stdio MCP support and does not
require `RIDGE_BROWSER_USE_ENABLED`. It exposes an attached real profile and
its cookies to trusted local tool invocations: do not turn it on for public
services, unattended multi-tenant agents, or unknown scripts. Browser Control
needs explicit extension installation, connected tab approval, and its
supported Node.js runtime. Do **not** ship it enabled in the default
`mcp_servers.json`; startup would otherwise fail on machines without the
external package or extension.

For CCB's `@ant/computer-use-mcp` or `@ant/claude-for-chrome-mcp`, use the
same principle (external stdio MCP installed and configured by the operator).
This PR does not vendor those packages or guess platform-specific command
paths. The native `ComputerUse` driver above remains a separate,
high-risk local option.

## Security and production gaps

**Do not enable this release for untrusted prompts, shared workers or
multi-tenant hosted agents.** Current safeguards are practical local defaults,
not a complete browser security boundary.

1. URL and subresource checks reject private, reserved, metadata and unsupported
   schemes by default; **DNS rebinding** is not prevented without enforcing
   a proxy/network namespace that pins egress addresses. Browser WebRTC and
   other browser protocols need separate review.
2. Browser actions can change remote state; an allowed click can submit a form,
   send a message or purchase. Require a user decision for high-impact
   mutations. **Per-action approval** is not implemented here.
3. Native computer control has no host-window/application ACL. The two
   environment flags authorize the operator's **whole desktop** for the
   session. Do not expose this on a public API.
4. The browser lives on a single process/thread. There is no Redis session
   ownership, retry reattachment, durable replay, or crash reaper. For cloud
   production, provision per-tenant VMs/containers and authenticated
   browser/desktop gateways with leases and cancellation.
5. Generated screenshots could include personal information. They are local
   temporary files, not Web-visible attachments, and have no automated
   retention/audit review. Typed text is redacted from *tool result echoes*
   but still passes through model tool arguments. Never fill passwords via
   this mechanism.
6. CAPTCHA, 2FA, passkeys and payment confirmations require human handoff;
   the adapter does not bypass these protections.
7. The browser tool's action-specific Playwright timeout is bounded, but the
   dedicated worker cannot forcibly kill a blocked native browser operation
   inside its own process. Production isolation requires a separate process
   that can be hard-killed on deadline.

### Next production milestone

- Build `BrowserSessionProvider` and `ComputerSessionProvider` interfaces
  on top of a VM/container pool with tenant-bound, unforgeable session leases.
- Add a secure egress proxy with DNS pinning and per-tenant allowlists.
- Add action-scoped approval gate (`read`, `navigate`, `write`,
  `external_submit`, `host_input`) enforced at dispatcher boundaries.
- Add authenticated screenshot streaming/Web UI live view, secure artifact
  links, cancellation and session reconnect/reaper.
- Add native macOS/Windows/GTK-AT-SPI real-app E2E; SSRF/DNS rebinding tests, load/soak tests,
  cross-tenant isolation tests and telemetry redaction tests.

## Test

```bash
python -m pytest tests/test_interactive_use.py tests/test_computer_native_elements.py -q
python -m pytest tests/test_interactive_browser_integration.py -q
# Computer integration only in disposable GUI environment, never real user host.
RIDGE_COMPUTER_GUI_SMOKE=1 xvfb-run -a python -m pytest tests/test_interactive_computer_integration.py -q
# Real Chromium integration: only on a machine with playwright/chromium
# Real desktop E2E: only on a dedicated disposable GUI VM
```

The `interactive-browser-smoke` CI job installs Chromium and exercises actual
navigation, ref-based interaction, back navigation and screenshot capture.
A dedicated `interactive-computer-smoke` job exercises PyAutoGUI against disposable Xvfb; `native-atspi-gtk-smoke` exercises real GTK3 AT-SPI operations on Linux. Physical macOS, Windows and guest-VM desktop acceptance remain separate production gates.
