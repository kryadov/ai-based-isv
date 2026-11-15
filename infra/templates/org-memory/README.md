# org-memory

Organization semantic memory (spec §7.4, ADR-0003), canonical in Gitea.

- `lessons/` — lessons promoted from project episodic memory
  (`org-bootstrap memory promote --episode N` opens a PR here). Every lesson
  carries its source (project, run, vertex, episode) in the front matter.
- `reuse-catalog.yaml` — reuse catalog v1: components a new project should
  reuse instead of re-building.

Changes arrive only through PRs reviewed by a human (agents never write here
in v0). "Published is not the same as improves results": a lesson becomes a
rule only after it is verified on historical tasks (spec self-evals, later).
