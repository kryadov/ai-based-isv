# M4c spike — FastMCP in-cluster + opencode harness registration

Date: 2026-11-12 (plan Task 1, throwaway code; only this note is committed).
Stand: Windows/WSL2 k3d `swarm-lab`, opencode `ghcr.io/anomalyco/opencode:2.2.0`,
model `litellm/qwen3` (Ollama, 16k context), fastmcp **4.1.8** (the plan assumed 2.x).

## (a) FastMCP streamable HTTP — wire shape

- Server: `FastMCP("name")` + `@mcp.tool`, run with
  `mcp.run(transport="http", host="0.0.0.0", port=8000, stateless_http=True, json_response=True)`.
  Endpoint: `POST /mcp`.
- With `stateless_http=True, json_response=True` a **single POST** works — no
  `initialize` handshake, no `Mcp-Session-Id`, plain JSON back (not SSE):

  ```
  POST /mcp
  Content-Type: application/json
  Accept: application/json, text/event-stream
  {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{"text":"raw"}}}
  → 200 {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ECHO:raw"}],
         "isError":false,"structuredContent":{"result":"ECHO:raw"}}}
  ```

  The `Accept` header must list both types. This is the shape the worker's plain
  `TrackerClient` uses (no fastmcp dependency in agent_runtime).
- Tool errors are **HTTP 200** with `result.isError: true` and the message in
  `content[0].text` (e.g. unknown tool → `Unknown tool: 'nope'`). Clients must
  check `isError`, not the status code.
- `fastmcp.Client(url)` (and `fastmcp.Client(server)` in-memory) round-trips fine;
  `result.data` carries the tool's return value.

## (b) opencode 2.2.0 MCP config shape

`opencode mcp add echo --url <url>` writes (project `opencode.json`):

```json
{"mcp": {"servers": {"echo": {"type": "remote", "url": "http://…:8000/mcp"}}}}
```

The same block inside our generated `opencode.jsonc` is honoured. `GET /api/mcp`
returns `[]` before the first session and, after `POST /api/session`,
`[{"name":"echo","status":{"status":"connected"},"integrationID":"mcp_…"}]` —
servers connect lazily at session start. **`GET /api/mcp` → `connected` is the
machine-checkable proof that the harness registered and reached the server.**

## (c) NetworkPolicy

Sandbox pods and MCP pods share namespace `agents` under `default-deny-all`.
Without an explicit allow the in-namespace connection is refused; with

- ingress: TCP 8000 (the MCP container port), and
- egress: to pods labelled as MCP servers, TCP 8000

both the probe pod (`wget …mcp-echo.agents.svc:8000/mcp`) and the host via
NodePort reach the server. The existing allow policy only opens ingress 4096.
MCP pods themselves need egress to `platform` 3000 (Gitea) / 4000 (LiteLLM) —
already allowed namespace-wide.

## (d) Gotchas

- **Images:** `pip install` at pod boot cannot work in `agents` (default-deny
  egress). Build a small image on the host and `k3d image import` it (same
  pattern as the derived opencode image); `imagePullPolicy: Never`.
- **Local model does not invoke MCP tools.** The tool schema reaches the model
  (its reasoning quotes our tool description), but qwen3 ends the step without a
  tool call — 3 runs, with/without thinking, server names `echo` and `spike`.
  Built-in opencode tools are called normally (m4a). Direct LiteLLM probe: a tool
  named `echo` → `tool_calls` emitted; the same tool named `echo_echo` → empty
  `stop`. Prefixed tool names (opencode exposes MCP tools as `<server>_<tool>`)
  appear to break qwen3/Ollama tool calling. Consequence for M4c: the acceptance
  proves harness registration via `/api/mcp` = `connected` (Review Focus #3, the
  "config validated" branch), not a model-driven tool call; re-check with a
  cloud model when API keys arrive.
- Latency: in-memory and HTTP tool calls are millisecond-scale; the LLM step
  dominates (~1–2 min per qwen3 step on the RTX 4070 + CPU split).
