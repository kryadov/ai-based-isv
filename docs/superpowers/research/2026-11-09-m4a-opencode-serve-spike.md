# M4a Spike: `opencode serve` in Docker via LiteLLM

> Throwaway research note. Goal: confirm the REST+SSE surface, provider config, and latency of OpenCode v2 running headlessly in Docker against the lab LiteLLM gateway.

## Pinned image

```text
ghcr.io/anomalyco/opencode:2.2.0
```

- `2.2.0` is the newest versioned tag that resolves (`2.3.0`/`3.0.0` do not exist); `latest` resolves to the same digest, so the versioned tag is pinned.
- Tested on Apple Silicon (arm64). The image is multi-arch (amd64 + arm64).

## Serve command that worked

```bash
docker run -d --name m4a-opencode-spike \
  -p 127.0.0.1:8080:8080 \
  -v "$SCRATCH/workspace:/workspace" \
  -v "$SCRATCH/opencode-data:/root/.opencode" \
  -e LITELLM_API_KEY="$LITELLM_MASTER_KEY" \
  -e OPENCODE_LOG_LEVEL=trace \
  -w /workspace \
  ghcr.io/anomalyco/opencode:2.2.0 \
  serve --hostname 0.0.0.0 --port 8080 --print-logs
```

- `--hostname 0.0.0.0` is required; the default binds to localhost inside the container and is unreachable from the host.
- `--port 8080` (arbitrary; pick one and map it).
- `--print-logs` sends server logs to `docker logs` (helpful for debugging).
- `-w /workspace` sets the default location; the location must be a git repository or OpenCode fails to boot the location services.

## Provider / model configuration

Configuration is done via a project-level `opencode.jsonc` file in the workspace root. Environment substitution works for the API key.

`$WORKSPACE/opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "litellm/qwen-coder",
  "permissions": [
    { "action": "*", "resource": "*", "effect": "allow" }
  ],
  "providers": {
    "litellm": {
      "name": "LiteLLM lab gateway",
      "env": ["LITELLM_API_KEY"],
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://192.168.5.1:30084/v1",
        "apiKey": "{env:LITELLM_API_KEY}"
      },
      "models": {
        "qwen-coder": {
          "name": "Qwen 2.5 Coder 7B",
          "modelID": "qwen-coder"
        }
      }
    }
  }
}
```

- `LITELLM_API_KEY` is the LiteLLM master key (from OpenBao `secret/litellm/master`).
- The LiteLLM endpoint is reachable at `192.168.5.1:30084` from inside the Docker network (colima VM IP). `host.docker.internal:30084` also works.
- The broad `permissions` rule is **required for headless runs**; any unconfigured `ask` permission hangs the session because there is no interactive client to reply (OpenCode issue #14473).

## Authentication

All `/api/*` routes require HTTP Basic authentication:

- username: `opencode`
- password: the value printed in the container logs as `server password <token>`

Example:

```bash
curl -u "opencode:$PASS" http://127.0.0.1:8080/api/info
```

## Endpoint paths and JSON shapes

### `GET /api/info`

Response:

```json
{
  "version": "2.2.0",
  "pid": 1,
  "hostname": "d71e2f9f58f7",
  "urls": ["http://127.0.0.1:8080", "http://172.17.0.2:8080"],
  "paths": { "tmp": "/tmp/opencode" },
  "capabilities": { "persistentPty": true }
}
```

### `POST /api/session`

Request:

```json
{
  "model": { "id": "qwen-coder", "providerID": "litellm" },
  "location": { "directory": "/workspace" }
}
```

Response:

```json
{
  "data": {
    "id": "ses_e3f740986ffePoPeqwtbpQNdK6",
    "projectID": "bf309ff466f7bcd6b1ca4751d16d42627dbac2ee",
    "model": { "id": "qwen-coder", "providerID": "litellm", "variant": "default" },
    "cost": 0,
    "tokens": { "input": 0, "output": 0, "reasoning": 0, "cache": { "read": 0, "write": 0 } },
    "time": { "created": 1794231733458, "updated": 1794231733458 },
    "location": { "directory": "/workspace" }
  }
}
```

### `POST /api/session/{sessionID}/prompt`

Request:

```json
{ "text": "Reply with exactly: OPENCODE-OK" }
```

Response:

```json
{
  "data": {
    "id": "msg_1c08bf687001KmkbBMoRWZKSs0",
    "sessionID": "ses_e3f740986ffePoPeqwtbpQNdK6",
    "time": { "created": 1794231760520 },
    "type": "user",
    "payload": { "text": "Reply with exactly: OPENCODE-OK" },
    "delivery": "steer"
  }
}
```

### `GET /api/session/{sessionID}` (status / outcome)

Useful for polling when SSE is not available:

```json
{
  "data": {
    "id": "ses_e3f740986ffePoPeqwtbpQNdK6",
    "projectID": "bf309ff466f7bcd6b1ca4751d16d42627dbac2ee",
    "model": { "id": "qwen-coder", "providerID": "litellm", "variant": "default" },
    "cost": 0,
    "tokens": { "input": 2, "output": 9, "reasoning": 0, "cache": { "read": 4990, "write": 0 } },
    "outcome": "succeeded",
    "time": { "created": 1794231733458, "updated": 1794231760889, "idle": 1794231760890 },
    "title": "OpenCode OK",
    "location": { "directory": "/workspace" }
  }
}
```

### `GET /api/session/{sessionID}/diff`

Response when no files changed:

```json
{ "data": [] }
```

Schema when files changed:

```json
{
  "data": [
    {
      "file": "src/example.py",
      "patch": "@@ -1,3 +1,4 @@\n ...",
      "additions": 1,
      "deletions": 0,
      "status": "modified"
    }
  ]
}
```

## SSE event stream

- Endpoint: `GET /api/event`
- Headers: `Accept: text/event-stream`, `Authorization: Basic ...`
- The stream is global; filter client-side by `data.sessionID` or `location.directory`.
- Optional query parameter `types` filters event types, but the simplest robust approach is to subscribe to all events and filter in the client.

### Raw SSE wire format

```text
data: {"id":"evt_1c08a6332001r0H50XGVkluTxB","type":"server.connected","data":{}}

: heartbeat

data: {"id":"evt_1c08a675b001H400ZvuLseJ0YQ","created":1794231658331,"type":"session.created","location":{"directory":"/workspace"},"data":{"sessionID":"ses_e3f7598b3ffeCeYPkpusBgFfci","slug":"silent-panda","version":"2.2.0","projectID":"bf309ff466f7bcd6b1ca4751d16d42627dbac2ee","location":{"directory":"/workspace"},"subpath":"","model":{"id":"qwen-coder","providerID":"litellm"}},"durable":{"aggregateID":"ses_e3f7598b3ffeCeYPkpusBgFfci","seq":0,"version":1}}

```

- Events are delimited by blank lines.
- Each event carries `data: <json>` only; there is **no top-level `event:` field**. The event type is inside the JSON object at `type`.
- `: heartbeat` lines are comments and should be ignored.

### Example events (parsed JSON)

**Terminal completion event**

```json
{
  "id": "evt_1c08bf7f9002NuPk36F6ywhZ93",
  "created": 1794231760889,
  "type": "session.execution.succeeded",
  "data": { "sessionID": "ses_e3f740986ffePoPeqwtbpQNdK6" },
  "durable": {
    "aggregateID": "ses_e3f740986ffePoPeqwtbpQNdK6",
    "seq": 12,
    "version": 1
  }
}
```

**Assistant text delta**

```json
{
  "id": "evt_1c08bf7ed0010E0KpG56tkZeqe",
  "created": 1794231760877,
  "type": "session.text.delta",
  "location": { "directory": "/workspace" },
  "data": {
    "sessionID": "ses_e3f740986ffePoPeqwtbpQNdK6",
    "assistantMessageID": "msg_1c08bf69c001ma7GyScfs4AiHE",
    "ordinal": 0,
    "delta": "OPENCODE-OK"
  }
}
```

**Tool / step event**

```json
{
  "id": "evt_1c08bf7f6001yY5oqL4dMs7KGr",
  "created": 1794231760886,
  "type": "session.text.ended",
  "location": { "directory": "/workspace" },
  "data": {
    "sessionID": "ses_e3f740986ffePoPeqwtbpQNdK6",
    "assistantMessageID": "msg_1c08bf69c001ma7GyScfs4AiHE",
    "ordinal": 0,
    "text": "OPENCODE-OK"
  }
}
```

## Latency

- **Cold prompt (model load / first hit)**: ~20 seconds.
  - Session `ses_e3f7f8866ffebqJzXkDQbOOlcz`: created → updated delta ≈ 20.4 s.
- **Warm / cached prompt**: ~0.5–1.5 s.
  - Subsequent "Reply with exactly: ..." prompts returned in < 1.5 s with high `cache.read` token counts.

Use generous timeouts (≥ 120 s, ideally 600 s) when driving the slow local model.

## Gotchas / lessons

1. **Headless permissions hang**: without the broad `permissions: [{"action":"*","resource":"*","effect":"allow"}]` rule, any tool permission that resolves to `ask` blocks forever because no client replies.
2. **Auth is Basic, user `opencode`**: the password is emitted in the container logs. Bearer tokens or empty-user Basic auth do not work.
3. **Location must be a git repo**: `opencode serve` fails to boot location services if `/workspace` is not a git repository.
4. **Bind-mount single files on macOS/colima is brittle**: mounting `opencode.jsonc` as a single file caused Docker to expose it as a directory inside the container. Mount the parent workspace directory and place the config file inside it.
5. **Data / DB isolation**: mount a dedicated host directory to `/root/.opencode` so the SQLite state is isolated per run and does not leak between spikes.
6. **SSE is global**: subscribe once and filter by `sessionID`; there is no per-session SSE endpoint in the tested surface.
7. **SSE event format**: only `data:` lines, no `event:` field. The type is in the JSON payload.
8. **Model tool reliability**: `qwen2.5-coder:7b` sometimes emits raw tool-call JSON as assistant text instead of executing the tool; for the runtime client this is still a `session.execution.succeeded` event.
