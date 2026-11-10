# M4b spike — snapshot intermittency (and what actually broke in k3s)

Date: 2026-11-10 · Time-boxed investigation during the m4b_flow bring-up.

## The k3s "empty diff" was NOT the M4a snapshot intermittency

The M4a acceptance (T10) reported intermittent `snapshot: null` → empty diff on
re-created sandboxes. The first k3s flow runs reproduced an empty diff on EVERY
attempt, which looked like the same failure — it was not.

**Root cause chain (all fixed in this milestone):**

1. `KubeSandbox` defaulted the pod image to the pinned `ghcr.io/anomalyco/opencode`
   base image — which **ships no git**. The harness diff endpoint depends on git
   (M4a spike): deterministic `{"data": []}` with files edited and the session
   succeeded. Fix: the sandbox runs the derived `opencode-git:<tag>` image
   (`SANDBOX_IMAGE`), the same image M4a's docker path builds.
2. The serve container did not mount the PVC workspace (`volumeMounts` only on the
   initContainer) — the harness ran against an empty image-internal `/workspace`.
3. `OPENCODE_DB` pointed into the PVC workspace — it broke the harness
   snapshot/diff machinery even with git present (verified by a controlled
   probe: identical pod minus OPENCODE_DB → real diff). Containers are
   per-vertex, so the default ephemeral state already isolates sessions.
4. A `--depth 1` clone was ruled out: full and shallow clones behaved the same
   (the shallow-clone theory was tested before the git-image discovery; the full
   clone was kept — it is harmless and closer to what the M4a docker path gives
   the harness).

With all four fixed: `m4b_flow.sh` PASS — the harness multi-step session produces
a real diff (`2 files changed`), done_when passes, the branch is pushed.

## models.opencode.ai — the harness phones home (open question)

`opencode serve` logs `Failed to fetch models.dev` (GET
`https://models.opencode.ai/experimental/v2.0/api.json`) when the network policy
blocks internet egress. In the controlled probes this was **non-fatal** (the
session worked with litellm despite the logged error), but one flow run under
allow-all egress showed a session fall back to OpenCode's free gateway
(`provider.free-limit`, HTTP 429). Watch this in M4c: if sessions under the
default-deny policy ever select the free provider again, the harness config
mechanism needs a hard offline guarantee (e.g. pre-populated provider catalog or
an upstream flag) — the sandbox policy itself is correct and stays.

## Probe-pod networking warm-up (flow infra lesson)

A freshly created pod's first outbound connection can fail during CNI warm-up
(DNS/route not ready for the first second). One-shot probes must retry
(5×3 s) or the acceptance fails transiently; a completed one-shot pod's logs
also proved unreliable to read from the flow (the pod may be gone before the
read) — the stable pattern is a sleeping pod + per-check `kubectl exec`.

## M4a's original intermittency — status

The M4a flow's occasional empty diff on re-created sandboxes (docker, hardlink
clones) was NOT reproduced here and remains unpinned. Frequency there: ~1 in a
few runs; the worker's 3-attempt budget absorbs it. Re-open if it ever burns a
whole budget in M4c+.
