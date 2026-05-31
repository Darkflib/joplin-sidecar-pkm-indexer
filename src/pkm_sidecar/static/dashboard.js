// pkm-sidecar dashboard. Loads all data via authenticated fetch(). The token is
// kept only in sessionStorage (tab-scoped) — never in any other web storage or
// a cookie, and only sent as an Authorization header to same-origin /api calls.
"use strict";

const TOKEN_KEY = "pkm_sidecar_token";

// 1) URL-fragment handoff: read #token=..., persist to sessionStorage, then strip
// it from the address bar so it never lands in history/Referer.
function captureFragmentToken() {
  const match = (window.location.hash || "").match(/token=([^&]+)/);
  if (match) {
    sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]));
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

function showPasteBanner() {
  document.getElementById("paste-banner").hidden = false;
}

async function api(path) {
  const token = getToken();
  const headers = token ? { Authorization: "Bearer " + token } : {};
  const resp = await fetch(path, { headers });
  if (resp.status === 401) {
    showPasteBanner();
    throw new Error("unauthorized");
  }
  return resp.json();
}

function li(text) {
  const el = document.createElement("li");
  el.textContent = text;
  return el;
}

function fill(id, notes) {
  const ul = document.getElementById(id);
  ul.innerHTML = "";
  if (!notes.length) {
    ul.appendChild(li("(none)"));
    return;
  }
  notes.forEach((n) => ul.appendChild(li(n.title || "(untitled)")));
}

async function loadStatus() {
  const s = await api("/api/status");
  const db = s.database;
  document.getElementById("status").textContent =
    `notes ${db.note_count} · folders ${db.folder_count} · tags ${db.tag_count} · tasks ${db.task_count}`;
  const badge = document.getElementById("status-badge");
  if (s.joplin && s.joplin.reachable === false) {
    badge.textContent = "Joplin offline — showing stale index";
    badge.classList.add("degraded");
  } else {
    badge.textContent = "Joplin online";
    badge.classList.remove("degraded");
  }
}

async function loadAll() {
  await loadStatus();
  fill("recent", await api("/api/notes/recent"));
  fill("inbox", await api("/api/notes/inbox"));
  fill("untagged", await api("/api/notes/untagged"));
  fill("todos", await api("/api/notes/todos"));
  fill("stale", await api("/api/notes/stale"));
}

function wireSearch() {
  document.getElementById("search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const q = document.getElementById("search-input").value.trim();
    if (!q) return;
    const hits = await api("/api/search?q=" + encodeURIComponent(q) + "&limit=50");
    const ul = document.getElementById("search-results");
    ul.innerHTML = "";
    hits.forEach((h) => ul.appendChild(li(h.title)));
  });
}

function wirePasteBanner() {
  document.getElementById("token-save").addEventListener("click", () => {
    const value = document.getElementById("token-input").value.trim();
    if (!value) return;
    sessionStorage.setItem(TOKEN_KEY, value);
    document.getElementById("paste-banner").hidden = true;
    loadAll().catch(() => {});
  });
}

captureFragmentToken();
wireSearch();
wirePasteBanner();
if (getToken()) {
  loadAll().catch(() => {});
} else {
  showPasteBanner();
}
