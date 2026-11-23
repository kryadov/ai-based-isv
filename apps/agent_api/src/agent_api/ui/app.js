// Organization panel (M10) — dependency-free ES module over the agent_api JSON API.
// Every DOM node is built with createElement/textContent: no data reaches innerHTML.

const LANG_KEY = "swarm.lang";
let lang = "en";
let dict = {};
let current = "inbox";

function t(key) {
  return dict[key] ?? key;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (res.status === 401) {
    window.location.href = "/ui";
    throw new Error("login required");
  }
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

async function loadLang(next) {
  try {
    dict = await api(`/ui/static/i18n/${next}.json`);
    lang = next;
    try { localStorage.setItem(LANG_KEY, next); } catch { /* storage may be blocked */ }
  } catch {
    dict = {};
  }
  document.documentElement.lang = lang;
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.getAttribute("data-i18n"));
  }
}

function status(text) {
  return el("span", { class: `status-${text}` }, text);
}

function link(url) {
  return url ? el("a", { href: url, target: "_blank", rel: "noopener" }, url) : "—";
}

// ------------------------------------------------------------------ tabs

async function viewInbox(view) {
  const { cards } = await api("/ui/api/inbox");
  if (!cards.length) {
    view.append(el("p", { class: "muted" }, t("inbox.empty")));
    return;
  }
  for (const card of cards) {
    const decide = async (action) => {
      await api(`/ui/api/hitl/${card.id}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      render();
    };
    const ctx = card.context || {};
    view.append(
      el(
        "div",
        { class: "card" },
        el("div", { class: "kind" }, `${card.kind} · #${card.id}`),
        el("h3", {}, card.subject),
        ctx.pr_url ? el("div", {}, "PR: ", link(ctx.pr_url)) : null,
        ctx.review_summary ? el("p", {}, ctx.review_summary) : null,
        ctx.summary ? el("p", {}, ctx.summary) : null,
        card.waiting_s !== null ? el("div", { class: "muted" }, `${t("inbox.waiting")}: ${Math.round(card.waiting_s / 60)} min`) : null,
        el("pre", {}, JSON.stringify(ctx, null, 2)),
        el(
          "div",
          { class: "actions" },
          el("button", { class: "approve", onclick: () => decide("approve") }, t("inbox.approve")),
          el("button", { class: "reject", onclick: () => decide("reject") }, t("inbox.reject")),
        ),
      ),
    );
  }
}

async function viewSwarm(view) {
  const { runs } = await api("/ui/api/runs");
  if (!runs.length) {
    view.append(el("p", { class: "muted" }, t("swarm.empty")));
    return;
  }
  const seen = new Set();  // runs come newest first: the first per task is its newest
  for (const run of runs) {
    const newest = !seen.has(run.task_ref);
    seen.add(run.task_ref);
    const retry = async () => {
      await api(`/ui/api/runs/${run.id}/retry`, { method: "POST" });
      render();
    };
    const canRetry = newest && run.status === "failed" && run.project.startsWith("intake:");
    const rows = run.vertices.map((v) =>
      el(
        "tr",
        {},
        el("td", {}, String(v.id)),
        el("td", {}, v.name),
        el("td", {}, status(v.status)),
        el("td", {}, v.deps.join(", ") || "—"),
        el("td", {}, v.leased_by || "—"),
        el("td", {}, link(v.pr_url)),
      ),
    );
    view.append(
      el(
        "div",
        { class: "card" },
        el("h3", {}, `${t("swarm.run")} ${run.id} · ${run.task_ref} `, status(run.status)),
        el("table", {}, el("tr", {}, ...["id", "vertex", "status", "deps", "agent", "PR"].map((h) => el("th", {}, h))), ...rows),
        canRetry ? el("div", { class: "actions" }, el("button", { class: "retry", onclick: retry }, t("swarm.retry"))) : null,
      ),
    );
  }
}

async function viewProjects(view) {
  const { registry, runs_by_repo: runs } = await api("/ui/api/projects");
  view.append(el("h3", {}, t("projects.registry")));
  view.append(
    el(
      "table",
      {},
      el("tr", {}, ...["name", "kind", "owner", "status", "reason"].map((h) => el("th", {}, h))),
      ...registry.map((p) => el("tr", {}, ...["name", "kind", "owner", "status", "reason"].map((k) => el("td", {}, p[k] ?? "")))),
    ),
  );
  view.append(el("h3", {}, t("projects.runs")));
  view.append(
    el("table", {}, ...Object.entries(runs).map(([repo, n]) => el("tr", {}, el("td", {}, repo), el("td", {}, String(n))))),
  );
}

async function viewAutomations(view) {
  const { automations } = await api("/automations");
  const { runs } = await api("/automations/runs");
  for (const a of automations) {
    const run = async () => {
      await api(`/ui/api/automations/${encodeURIComponent(a.name)}/run`, { method: "POST" });
      alert(t("automations.requested"));
      render();
    };
    view.append(
      el(
        "div",
        { class: "card" },
        el("div", { class: "kind" }, `${a.trigger}${a.schedule ? " · " + a.schedule : ""} · ${a.role}`),
        el("h3", {}, a.name),
        el("p", {}, a.description),
        el("div", { class: "muted" }, `scope: ${(a.scope || []).join(", ")}`),
        el("div", { class: "actions" }, el("button", { class: "primary", onclick: run }, t("automations.run"))),
      ),
    );
  }
  view.append(el("h3", {}, t("automations.history")));
  view.append(
    el(
      "table",
      {},
      el("tr", {}, ...["id", "name", "status", "findings", "issues", "by"].map((h) => el("th", {}, h))),
      ...runs.map((r) =>
        el(
          "tr",
          {},
          el("td", {}, String(r.id)),
          el("td", {}, r.name),
          el("td", {}, status(r.status)),
          el("td", {}, String((r.report && r.report.findings ? r.report.findings.length : 0))),
          el("td", {}, (r.created_issues || []).length ? r.created_issues.map((u) => link(u)) : "—"),
          el("td", {}, r.requested_by || "—"),
        ),
      ),
    ),
  );
}

async function viewMetrics(view) {
  const text = await api("/metrics");
  view.append(el("h3", {}, t("metrics.title")));
  const rows = text
    .split("\n")
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const [series, value] = line.split(" ");
      return el("tr", {}, el("td", {}, series), el("td", {}, value));
    });
  view.append(el("table", {}, ...rows));
}

async function viewMemory(view) {
  const box = el("input", { type: "search", placeholder: t("memory.search") });
  const list = el("div");
  const search = async () => {
    const { episodes } = await api(`/ui/api/memory?q=${encodeURIComponent(box.value)}`);
    list.replaceChildren(
      ...episodes.map((e) =>
        el(
          "div",
          { class: "card" },
          el("div", { class: "kind" }, `${e.kind} · ${e.project} · #${e.id}`),
          el("h3", {}, e.title),
          el("pre", {}, e.body),
          el("div", { class: "muted" }, `${t("memory.promote")} ${e.id}`),
        ),
      ),
    );
  };
  box.addEventListener("change", search);
  view.append(box, list);
  await search();
}

function viewPlanned(view) {
  view.append(el("p", { class: "muted" }, t("planned")));
}

const VIEWS = {
  inbox: viewInbox,
  projects: viewProjects,
  swarm: viewSwarm,
  pipeline: viewPlanned,
  automations: viewAutomations,
  metrics: viewMetrics,
  memory: viewMemory,
  settings: viewPlanned,
};

async function renderHealth() {
  const h = await api("/ui/api/health");
  const items = [
    ["health.pending", h.pending_decisions],
    ["health.runs_done", h.runs_done],
    ["health.runs_failed", h.runs_failed],
    ["health.running", h.vertices_running],
    ["health.memory", h.memory_episodes],
  ];
  document.getElementById("health").replaceChildren(
    ...items.map(([k, v]) => el("span", {}, `${t(k)}: `, el("b", {}, String(v)))),
  );
  document.getElementById("badges").replaceChildren(
    el("span", { class: "badge" }, `${t("health.pending")}: ${h.pending_decisions}`),
    el("span", { class: "badge" }, `${t("health.running")}: ${h.vertices_running}`),
  );
}

async function render() {
  for (const b of document.querySelectorAll(".tabs button")) {
    b.classList.toggle("active", b.dataset.tab === current);
  }
  const view = document.getElementById("view");
  view.replaceChildren();
  try {
    await VIEWS[current](view);
  } catch (err) {
    view.append(el("p", { class: "error" }, `${t("error.load")}: ${err.message}`));
  }
  renderHealth().catch(() => {});
}

async function main() {
  let saved = "en";
  try { saved = localStorage.getItem(LANG_KEY) || "en"; } catch { /* default */ }
  const select = document.getElementById("lang");
  select.value = saved;
  select.addEventListener("change", async () => { await loadLang(select.value); render(); });
  await loadLang(saved);
  const me = await api("/ui/api/me");
  document.getElementById("who").textContent = `${me.user} (${me.role})`;
  for (const b of document.querySelectorAll(".tabs button")) {
    b.addEventListener("click", () => { current = b.dataset.tab; render(); });
  }
  await render();
}

main();
