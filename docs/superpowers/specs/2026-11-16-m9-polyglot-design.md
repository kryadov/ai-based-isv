# M9: polyglot dogfood — a TypeScript service through the same corridor

Spec §7.2 item 4, §8 M9: a dogfood project on a foreign stack (TS/React or a Java mini-service) checks polyglot support — TaskSource, CI adapters, agents in a foreign stack. The owner said to continue without stopping. Every "Ruling" here is my call, open to correction.

## Intent

Prove that nothing in the platform is Python-shaped except the platform itself. A TypeScript service is onboarded from a template and given a task. An agent implements it, and the PR passes a **typescript check profile** (tsc, eslint, node tests, gitleaks, semgrep), the bot review and HITL, then merges. This is the same corridor, with only the profile and the acceptance command swapped.

**Acceptance (`m9_flow.sh`):**
1. `onboard --stack typescript --template ts-service` creates `swarm-lab/ts-greeter`: template code, profile, protection.
2. The seed CI is green.
3. An agent adds `GET /api/time` with a `node:test` test.
4. `done_when` passes (node in docker).
5. The PR merges after CI, review and HITL.

## Rulings

- **TypeScript over Java.** Lighter: the CI job image (`gitea/runner-images:ubuntu-latest`) already has Node for actions, and a JVM toolchain would add minutes per run. Java stays in TODO (more check profiles).
- **No framework.** `node:http` plus `node:test`; dev dependencies are only `typescript`, `eslint` and `typescript-eslint`. Small, fast `npm ci`.
- **Tests run on compiled JS.** `npm test` = `tsc && node --test dist/test/`, so no loader and no Node-version-specific type stripping.
- **`done_when` runs node in docker:** `docker run --rm -v "$PWD":/w -w /w node:22-alpine sh -c "npm ci && npm test"`. The toolbox has the docker CLI and the daemon sees the same paths. It is the platform-supplied command of this stack; agents still never run commands.
- **Templates are data.** `infra/templates/projects/<name>/` holds the code, `infra/templates/check-profiles/<stack>/` the CI. `onboard --template` seeds the code before protection, reusing `bootstrap.seed.push_seed`. The reuse catalog gets the new profile and template.
- **The security job is shared**, copied from the python profile: gitleaks + semgrep via uv. It is stack-independent.

## Non-goals

- React/frontend: no browser tests in the corridor yet.
- Java.
- Deploying the TS service as a canary: the M6 deploy templates are service-agnostic, but the dogfood checks the corridor only.
- Per-kind tutorials (TODO).
