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

async function api(path, options) {
  const token = getToken();
  const headers = token ? { Authorization: "Bearer " + token } : {};
  const resp = await fetch(path, { headers, ...options });
  if (resp.status === 401) {
    showPasteBanner();
    throw new Error("unauthorized");
  }
  if (!resp.ok) {
    throw new Error("HTTP " + resp.status);
  }
  return resp.json();
}

// ---------- formatting helpers ----------

// Relative "time ago" from an epoch-millis timestamp (Joplin stores ms).
function timeAgo(ms) {
  if (!ms) return "";
  const secs = Math.round((Date.now() - ms) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return mins + "m ago";
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return hrs + "h ago";
  const days = Math.round(hrs / 24);
  if (days < 30) return days + "d ago";
  const months = Math.round(days / 30);
  if (months < 12) return months + "mo ago";
  return Math.round(months / 12) + "y ago";
}

function fmtDate(ms) {
  if (!ms) return "";
  return new Date(ms).toLocaleString();
}

// FTS snippets arrive with [..] around matches; render those as <mark> safely by
// building text nodes and <mark> elements (never innerHTML of untrusted text).
function renderSnippet(el, snippet) {
  el.textContent = "";
  const parts = String(snippet).split(/(\[[^\]]*\])/);
  for (const part of parts) {
    if (part.startsWith("[") && part.endsWith("]")) {
      const mark = document.createElement("mark");
      mark.textContent = part.slice(1, -1);
      el.appendChild(mark);
    } else if (part) {
      el.appendChild(document.createTextNode(part));
    }
  }
}

// ---------- row rendering ----------

function noteRow(note, opts) {
  const options = opts || {};
  const item = document.createElement("li");
  item.className = "note-item";

  const isTodo = note.is_todo;
  const done = isTodo && note.todo_completed;
  if (done) item.classList.add("todo-done");

  const icon = document.createElement("span");
  icon.className = "note-icon";
  if (isTodo) {
    icon.classList.add(done ? "todo-done" : "todo-open");
    icon.textContent = done ? "☑" : "☐";
  } else {
    icon.textContent = "·";
  }
  item.appendChild(icon);

  const title = document.createElement("a");
  title.className = "note-title";
  title.textContent = note.title || "(untitled)";
  title.href = "joplin://x-callback-url/openNote?id=" + encodeURIComponent(note.id);
  title.title = note.title || "(untitled)";
  item.appendChild(title);

  const open = document.createElement("a");
  open.className = "note-open";
  open.href = title.href;
  open.textContent = "open ↗";
  open.title = "Open in Joplin";
  item.appendChild(open);

  if (options.snippet && note.snippet) {
    const snip = document.createElement("div");
    snip.className = "note-snippet";
    renderSnippet(snip, note.snippet);
    item.appendChild(snip);
  }

  const meta = document.createElement("div");
  meta.className = "note-meta";
  const when = note.user_updated_time || note.updated_time;
  if (when) {
    const t = document.createElement("span");
    t.textContent = timeAgo(when);
    t.title = fmtDate(when);
    meta.appendChild(t);
  }
  if (meta.childNodes.length) item.appendChild(meta);

  return item;
}

function fill(id, notes, opts) {
  const ul = document.getElementById(id);
  ul.innerHTML = "";
  const countEl = document.getElementById("count-" + id);
  if (countEl) countEl.textContent = notes.length;
  if (!notes.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Nothing here";
    ul.appendChild(empty);
    return;
  }
  notes.forEach((n) => ul.appendChild(noteRow(n, opts)));
}

function fillError(id, message) {
  const ul = document.getElementById(id);
  ul.innerHTML = "";
  const countEl = document.getElementById("count-" + id);
  if (countEl) countEl.textContent = "!";
  const err = document.createElement("li");
  err.className = "error";
  err.textContent = message;
  ul.appendChild(err);
}

// ---------- status / counts ----------

async function loadStatus() {
  const s = await api("/api/status");
  const db = s.database;
  document.getElementById("count-notes").textContent = db.note_count;
  document.getElementById("count-folders").textContent = db.folder_count;
  document.getElementById("count-tags").textContent = db.tag_count;
  document.getElementById("count-tasks").textContent = db.task_count;

  // Index meta timestamps are epoch *seconds* (note timestamps are ms); scale up.
  const lastSecs = s.indexing.last_incremental_index_at || s.indexing.last_full_index_at;
  const lastMs = lastSecs ? lastSecs * 1000 : null;
  const lastSync = document.getElementById("last-sync");
  if (lastMs) {
    lastSync.textContent = "synced " + timeAgo(lastMs);
    lastSync.title = fmtDate(lastMs);
  } else {
    lastSync.textContent = "never synced";
    lastSync.title = "";
  }

  const badge = document.getElementById("status-badge");
  if (s.joplin && s.joplin.reachable === false) {
    badge.textContent = "Joplin offline";
    badge.title = "Showing the last indexed snapshot";
    badge.classList.add("degraded");
  } else {
    badge.textContent = "Joplin online";
    badge.title = "";
    badge.classList.remove("degraded");
  }
}

// Load each column independently so one failing endpoint doesn't blank the rest.
async function loadColumn(id, path, opts) {
  try {
    fill(id, await api(path), opts);
  } catch (err) {
    if (err.message === "unauthorized") throw err;
    fillError(id, "Failed to load");
  }
}

async function loadAll() {
  document.getElementById("paste-banner").hidden = true;
  await loadStatus();
  await Promise.all([
    loadColumn("recent", "/api/notes/recent"),
    loadColumn("inbox", "/api/notes/inbox"),
    loadColumn("untagged", "/api/notes/untagged"),
    loadColumn("todos", "/api/notes/todos"),
    loadColumn("review", "/api/notes/review"),
    loadColumn("stale", "/api/notes/stale"),
  ]);
}

// ---------- search ----------

function clearSearch() {
  document.getElementById("col-search").hidden = true;
  document.getElementById("search-results").innerHTML = "";
  document.getElementById("search-input").value = "";
}

function wireSearch() {
  document.getElementById("search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const q = document.getElementById("search-input").value.trim();
    if (!q) {
      clearSearch();
      return;
    }
    document.getElementById("col-search").hidden = false;
    try {
      const hits = await api("/api/search?q=" + encodeURIComponent(q) + "&limit=50");
      fill("search-results", hits, { snippet: true });
    } catch (err) {
      if (err.message === "unauthorized") return;
      fillError("search-results", "Search failed");
    }
  });
  document.getElementById("search-clear").addEventListener("click", clearSearch);
}

// ---------- sync ----------

function wireSync() {
  const btn = document.getElementById("sync-btn");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Syncing…";
    try {
      await api("/api/index/sync", { method: "POST" });
      // Give the background sync a moment, then refresh the view.
      setTimeout(() => {
        loadAll().catch(() => {});
      }, 1500);
    } catch (err) {
      /* leave the view as-is on failure */
    } finally {
      setTimeout(() => {
        btn.disabled = false;
        btn.textContent = original;
      }, 1500);
    }
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
wireSync();
wirePasteBanner();
if (getToken()) {
  loadAll().catch(() => {});
} else {
  showPasteBanner();
}
