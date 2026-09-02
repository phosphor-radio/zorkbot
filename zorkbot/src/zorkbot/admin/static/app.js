"use strict";

// ---------------------------------------------------------------------
// Token handling. Access token: memory only. Refresh token: sessionStorage
// (dropped when the tab closes). Never a query parameter, anywhere.
// ---------------------------------------------------------------------

const state = {
  accessToken: null,
  refreshToken: sessionStorage.getItem("zorkbot_refresh_token"),
  mustChangePassword: false,
  liveTimer: null,
  liveStreamAbort: null,
  playersSort: "last_active",
  playersOrder: "desc",
  historyCursor: null,
};

function setTokens(resp) {
  state.accessToken = resp.access_token;
  state.refreshToken = resp.refresh_token;
  state.mustChangePassword = !!resp.must_change_password;
  sessionStorage.setItem("zorkbot_refresh_token", resp.refresh_token);
}

function clearTokens() {
  state.accessToken = null;
  state.refreshToken = null;
  sessionStorage.removeItem("zorkbot_refresh_token");
}

async function apiRaw(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.accessToken) headers["Authorization"] = "Bearer " + state.accessToken;
  return fetch("/api" + path, Object.assign({}, opts, { headers }));
}

async function api(path, opts = {}) {
  let resp = await apiRaw(path, opts);
  if (resp.status === 401 && state.refreshToken) {
    const refreshed = await tryRefresh();
    if (refreshed) resp = await apiRaw(path, opts);
  }
  if (resp.status === 401) {
    showLogin();
    throw new Error("unauthorized");
  }
  return resp;
}

async function tryRefresh() {
  try {
    const body = new URLSearchParams();
    body.set("grant_type", "refresh_token");
    body.set("refresh_token", state.refreshToken);
    const resp = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!resp.ok) {
      clearTokens();
      return false;
    }
    setTokens(await resp.json());
    return true;
  } catch (e) {
    clearTokens();
    return false;
  }
}

// ---------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------

function show(id) {
  for (const el of document.querySelectorAll(".view")) el.hidden = el.id !== id;
}

function showLogin() {
  clearTokens();
  show("login-view");
}

function showPasswordChange() {
  show("password-view");
}

function showApp() {
  show("app-view");
  startLive();
}

// ---------------------------------------------------------------------
// Login
// ---------------------------------------------------------------------

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("login-username").value;
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.hidden = true;
  try {
    const body = new URLSearchParams();
    body.set("grant_type", "password");
    body.set("username", username);
    body.set("password", password);
    const resp = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    const data = await resp.json();
    if (!resp.ok) {
      errEl.textContent = data.error_description || "Sign-in failed.";
      errEl.hidden = false;
      return;
    }
    setTokens(data);
    if (state.mustChangePassword) {
      showPasswordChange();
    } else {
      showApp();
    }
  } catch (err) {
    errEl.textContent = "Network error.";
    errEl.hidden = false;
  }
});

// ---------------------------------------------------------------------
// Forced first-login password change
// ---------------------------------------------------------------------

document.getElementById("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const current = document.getElementById("pw-current").value;
  const next = document.getElementById("pw-new").value;
  const confirm = document.getElementById("pw-confirm").value;
  const errEl = document.getElementById("password-error");
  errEl.hidden = true;
  if (next !== confirm) {
    errEl.textContent = "New passwords do not match.";
    errEl.hidden = false;
    return;
  }
  try {
    const resp = await api("/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    if (resp.status !== 204) {
      const data = await resp.json().catch(() => ({}));
      errEl.textContent = (data.error && data.error.error_description) || data.error_description || "Could not change password.";
      errEl.hidden = false;
      return;
    }
    // Password change revokes all tokens server-side — log in again.
    clearTokens();
    document.getElementById("password-form").reset();
    showLogin();
  } catch (err) {
    errEl.textContent = "Network error.";
    errEl.hidden = false;
  }
});

document.getElementById("settings-password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const current = document.getElementById("set-pw-current").value;
  const next = document.getElementById("set-pw-new").value;
  const confirm = document.getElementById("set-pw-confirm").value;
  const errEl = document.getElementById("settings-password-error");
  const okEl = document.getElementById("settings-password-success");
  errEl.hidden = true;
  okEl.hidden = true;
  if (next !== confirm) {
    errEl.textContent = "New passwords do not match.";
    errEl.hidden = false;
    return;
  }
  try {
    const resp = await api("/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    if (resp.status !== 204) {
      const data = await resp.json().catch(() => ({}));
      errEl.textContent = (data.error && data.error.error_description) || data.error_description || "Could not change password.";
      errEl.hidden = false;
      return;
    }
    okEl.hidden = false;
    document.getElementById("settings-password-form").reset();
    clearTokens();
    setTimeout(showLogin, 1200);
  } catch (err) {
    errEl.textContent = "Network error.";
    errEl.hidden = false;
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  stopLive();
  if (state.refreshToken) {
    try {
      const body = new URLSearchParams();
      body.set("refresh_token", state.refreshToken);
      await api("/token/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
    } catch (e) {
      /* best effort */
    }
  }
  showLogin();
});

// ---------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------

for (const btn of document.querySelectorAll(".tab")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", b === btn);
    for (const panel of document.querySelectorAll(".tab-panel")) {
      panel.hidden = panel.id !== "tab-" + btn.dataset.tab;
    }
    if (btn.dataset.tab === "history") loadHistory(true);
    if (btn.dataset.tab === "charts") loadCharts();
    if (btn.dataset.tab === "players") loadPlayers();
  });
}
document.querySelector(".tab[data-tab='live']").classList.add("active");

// ---------------------------------------------------------------------
// Live sessions
// ---------------------------------------------------------------------

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function fmtTime(unixSeconds) {
  if (unixSeconds == null) return "—";
  return new Date(unixSeconds * 1000).toLocaleString();
}

async function refreshLive() {
  try {
    const resp = await api("/sessions");
    const data = await resp.json();
    renderLiveTable(data.sessions || []);
  } catch (e) {
    /* transient; next poll will retry */
  }
}

function renderLiveTable(sessions) {
  const tbody = document.querySelector("#live-table tbody");
  tbody.innerHTML = "";
  if (sessions.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="5" class="mono">No active sessions.</td>`;
    tbody.appendChild(tr);
    return;
  }
  for (const s of sessions) {
    const tr = document.createElement("tr");
    const watchers = (s.watchers || [])
      .map((w) => `<span class="watcher-chip">${escapeHtml(w.name || w.pubkey_prefix)}</span>`)
      .join("") || "—";
    tr.innerHTML = `
      <td>#${s.num}</td>
      <td>${escapeHtml(s.player.name || s.player.pubkey_prefix)}</td>
      <td>${fmtDuration(s.duration_seconds)}</td>
      <td>${watchers}</td>
      <td><button class="link" data-watch="${s.num}">Watch</button></td>
    `;
    tbody.appendChild(tr);
  }
  for (const btn of tbody.querySelectorAll("[data-watch]")) {
    btn.addEventListener("click", () => openLiveStream(parseInt(btn.dataset.watch, 10)));
  }
}

function startLive() {
  refreshLive();
  stopLiveTimer();
  state.liveTimer = setInterval(refreshLive, 5000);
}

function stopLiveTimer() {
  if (state.liveTimer) {
    clearInterval(state.liveTimer);
    state.liveTimer = null;
  }
}

function stopLive() {
  stopLiveTimer();
  closeLiveStream();
}

function closeLiveStream() {
  if (state.liveStreamAbort) {
    state.liveStreamAbort.abort();
    state.liveStreamAbort = null;
  }
  document.getElementById("live-stream").hidden = true;
}

document.getElementById("live-stream-close").addEventListener("click", closeLiveStream);

async function openLiveStream(num) {
  closeLiveStream();
  const panel = document.getElementById("live-stream");
  const log = document.getElementById("live-stream-log");
  const title = document.getElementById("live-stream-title");
  title.textContent = `Session #${num}`;
  log.textContent = "";
  panel.hidden = false;

  const controller = new AbortController();
  state.liveStreamAbort = controller;

  try {
    const resp = await apiRaw(`/sessions/${num}/stream`, { signal: controller.signal });
    if (!resp.ok || !resp.body) {
      log.textContent = "Could not open stream.";
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const rawEvent = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        handleSseEvent(rawEvent, log, num);
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      log.textContent += "\n[stream closed]";
    }
  }
}

function handleSseEvent(raw, log, num) {
  let event = "message";
  let dataLine = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLine += line.slice(5).trim();
    else if (line.startsWith(":")) return; // comment/ping
  }
  if (!dataLine) return;
  let data;
  try {
    data = JSON.parse(dataLine);
  } catch (e) {
    return;
  }
  if (event === "command") {
    appendLog(log, `[${data.player}] > ${data.text}`);
  } else if (event === "output") {
    appendLog(log, data.text);
  } else if (event === "watchers") {
    appendLog(log, `[watchers: ${(data.watchers || []).map((w) => w.pubkey_prefix).join(", ") || "none"}]`);
  } else if (event === "session_end") {
    appendLog(log, `[session #${num} ended: ${data.reason}]`);
    refreshLive();
  }
}

function appendLog(log, text) {
  log.textContent += (log.textContent ? "\n" : "") + text;
  log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------------
// History
// ---------------------------------------------------------------------

document.getElementById("history-filters").addEventListener("submit", (e) => {
  e.preventDefault();
  loadHistory(true);
});
document.getElementById("history-more").addEventListener("click", () => loadHistory(false));

function toUnix(datetimeLocalValue) {
  if (!datetimeLocalValue) return null;
  return Math.floor(new Date(datetimeLocalValue).getTime() / 1000);
}

async function loadHistory(reset) {
  const tbody = document.querySelector("#history-table tbody");
  if (reset) {
    tbody.innerHTML = "";
    state.historyCursor = null;
  }
  const params = new URLSearchParams();
  const from = toUnix(document.getElementById("hist-from").value);
  const to = toUnix(document.getElementById("hist-to").value);
  const player = document.getElementById("hist-player").value.trim();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (player) params.set("player", player);
  if (state.historyCursor) params.set("cursor", state.historyCursor);
  params.set("limit", "50");

  const resp = await api("/sessions/history?" + params.toString());
  const data = await resp.json();
  state.historyCursor = data.next_cursor;
  document.getElementById("history-more").hidden = !data.next_cursor;

  for (const s of data.sessions || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>#${s.session_num}</td>
      <td>${escapeHtml((s.player && (s.player.name || s.player.pubkey_prefix)) || "—")}</td>
      <td>${fmtTime(s.started_at)}</td>
      <td>${fmtTime(s.ended_at)}</td>
      <td>${fmtDuration(s.duration_seconds)}</td>
      <td>${escapeHtml(s.end_reason || "—")}</td>
      <td>${s.peak_watchers}</td>
    `;
    tbody.appendChild(tr);
  }
}

// ---------------------------------------------------------------------
// Charts (minimal self-contained SVG line/bar renderer — no external
// charting library, so the admin UI stays fully offline-capable).
// ---------------------------------------------------------------------

function currentRangeSeconds() {
  return parseInt(document.getElementById("chart-range-select").value, 10);
}

function bucketForRange(rangeSeconds) {
  if (rangeSeconds <= 3600) return "minute";
  if (rangeSeconds <= 86400 * 2) return "hour";
  return "day";
}

function renderLineChart(container, series, labels) {
  // series: [{name, color, points: [{t, v}]}]
  const width = 900;
  const height = 160;
  const padding = { top: 10, right: 10, bottom: 20, left: 32 };
  const allPoints = series.flatMap((s) => s.points);
  const maxV = Math.max(1, ...allPoints.map((p) => p.v));
  const minT = Math.min(...allPoints.map((p) => p.t));
  const maxT = Math.max(...allPoints.map((p) => p.t));
  const spanT = Math.max(1, maxT - minT);

  const x = (t) => padding.left + ((t - minT) / spanT) * (width - padding.left - padding.right);
  const y = (v) => height - padding.bottom - (v / maxV) * (height - padding.top - padding.bottom);

  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">`;
  svg += `<line x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}" stroke="var(--border)" />`;
  svg += `<text class="axis-label" x="2" y="${height - padding.bottom}">0</text>`;
  svg += `<text class="axis-label" x="2" y="${padding.top + 8}">${maxV}</text>`;

  series.forEach((s, i) => {
    const cls = i === 0 ? "series-a" : "series-b";
    const d = s.points.map((p, idx) => `${idx === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    svg += `<path class="${cls}" d="${d}" />`;
  });
  svg += `</svg>`;

  const legend = series
    .map((s, i) => `<span><span class="dot" style="background:${i === 0 ? "var(--accent)" : "var(--danger)"}"></span>${escapeHtml(s.name)}</span>`)
    .join("");

  container.innerHTML = `<div class="legend">${legend}</div>${svg}`;
}

async function loadCharts() {
  const rangeSeconds = currentRangeSeconds();
  const bucket = bucketForRange(rangeSeconds);
  const to = Math.floor(Date.now() / 1000);
  const from = to - rangeSeconds;
  const qs = `from=${from}&to=${to}&bucket=${bucket}`;

  try {
    const sessResp = await api(`/stats/sessions?${qs}`);
    const sessData = await sessResp.json();
    renderLineChart(document.getElementById("chart-sessions"), [
      { name: "Started", points: sessData.map((d) => ({ t: d.t, v: d.started })) },
      { name: "Ended", points: sessData.map((d) => ({ t: d.t, v: d.ended })) },
    ]);

    const rxTransport = document.querySelector("input[name='rx-transport']:checked").value;
    const rxResp = await api(`/stats/messages?${qs}&direction=rx&transport=${rxTransport}`);
    const rxData = await rxResp.json();
    renderLineChart(document.getElementById("chart-rx"), [
      { name: "Messages received", points: rxData.map((d) => ({ t: d.t, v: d.count })) },
    ]);

    const txTransport = document.querySelector("input[name='tx-transport']:checked").value;
    const txResp = await api(`/stats/messages?${qs}&direction=tx&transport=${txTransport}`);
    const txData = await txResp.json();
    renderLineChart(document.getElementById("chart-tx"), [
      { name: "Messages sent", points: txData.map((d) => ({ t: d.t, v: d.count })) },
    ]);
  } catch (e) {
    /* view not active or transient error */
  }
}

document.getElementById("chart-range").addEventListener("change", loadCharts);
for (const input of document.querySelectorAll("input[name='rx-transport'], input[name='tx-transport']")) {
  input.addEventListener("change", loadCharts);
}

// ---------------------------------------------------------------------
// Players
// ---------------------------------------------------------------------

for (const th of document.querySelectorAll("#players-table th[data-sort]")) {
  th.addEventListener("click", () => {
    if (state.playersSort === th.dataset.sort) {
      state.playersOrder = state.playersOrder === "asc" ? "desc" : "asc";
    } else {
      state.playersSort = th.dataset.sort;
      state.playersOrder = "desc";
    }
    loadPlayers();
  });
}

async function loadPlayers() {
  const params = new URLSearchParams({
    sort: state.playersSort,
    order: state.playersOrder,
    limit: "100",
  });
  const resp = await api("/players?" + params.toString());
  const data = await resp.json();
  const tbody = document.querySelector("#players-table tbody");
  tbody.innerHTML = "";
  for (const p of data.players || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p.name || "—")}</td>
      <td>${fmtTime(p.last_active_at)}</td>
      <td>${fmtTime(p.first_active_at)}</td>
      <td>${p.sessions_started}</td>
      <td>${p.messages_received_from}</td>
      <td>${p.messages_sent_to}</td>
      <td class="mono">${escapeHtml(p.pubkey_prefix)}</td>
    `;
    tbody.appendChild(tr);
  }
}

// ---------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

(async function init() {
  if (!state.refreshToken) {
    showLogin();
    return;
  }
  const ok = await tryRefresh();
  if (!ok) {
    showLogin();
    return;
  }
  if (state.mustChangePassword) {
    showPasswordChange();
  } else {
    showApp();
  }
})();
