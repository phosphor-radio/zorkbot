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
  logStreamAbort: null,
  logReconnect: null,
  logRecords: [],
  logLastSeq: 0,
  uptimeTimer: null,
  radioTimer: null,
  contactsShown: false,
  radioWrites: false,
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

// Streams are opened with `apiRaw` so the caller keeps the ReadableStream —
// `api`'s retry would consume the body. They still need the same one-shot
// refresh, and more than most requests: a log tail is meant to stay open for
// hours, well past the access token's 30-minute TTL, and without this its
// reconnect loop would spin on 401 until someone reloaded the page.
async function openStream(path, signal) {
  let resp = await apiRaw(path, { signal });
  if (resp.status === 401 && state.refreshToken) {
    if (await tryRefresh()) resp = await apiRaw(path, { signal });
  }
  if (resp.status === 401) showLogin();
  return resp;
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
  stopLogStream();
  stopUptimePolling();
  stopRadioPolling();
  resetRadioView();
  resetLogView();
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
    if (btn.dataset.tab === "radio") startRadioPolling();
    else stopRadioPolling();
    // Log streams are capped server-side (max_log_streams, default 2), so the
    // stream is dropped the moment the tab loses focus rather than held open
    // for a view nobody is looking at. The uptime poll goes with it.
    if (btn.dataset.tab === "logs") {
      startLogStream();
      startUptimePolling();
    } else {
      stopLogStream();
      stopUptimePolling();
    }
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
    const resp = await openStream(`/sessions/${num}/stream`, controller.signal);
    if (!resp.ok || !resp.body) {
      log.textContent = "Could not open stream.";
      return;
    }
    await readSse(resp, (event, data) => handleSessionEvent(event, data, log, num));
  } catch (e) {
    if (e.name !== "AbortError") {
      log.textContent += "\n[stream closed]";
    }
  }
}

// EventSource can't send an Authorization header and a token must never go in
// a query string, so the SPA reads the text/event-stream body itself. Shared
// by the session transcript and the log tail.
async function readSse(resp, onEvent) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const parsed = parseSseFrame(frame);
      if (parsed) onEvent(parsed.event, parsed.data);
    }
  }
}

function parseSseFrame(raw) {
  let event = "message";
  let dataLine = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLine += line.slice(5).trim();
    else if (line.startsWith(":")) return null; // comment/ping
  }
  if (!dataLine) return null;
  try {
    return { event, data: JSON.parse(dataLine) };
  } catch (e) {
    return null;
  }
}

function handleSessionEvent(event, data, log, num) {
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

// Only player DMs are ACK-waited, so an empty denominator is the normal
// state of a bot that has sent nothing but watcher fan-out and channel
// traffic — it is "nothing measured", never 0%.
function deliveryRate(delivered, failed, suffix) {
  const measured = delivered + failed;
  if (measured === 0) return "\u2014";
  const pct = ((delivered / measured) * 100).toFixed(1);
  return suffix ? `${pct}% (${suffix})` : `${pct}%`;
}

function currentRangeSeconds() {
  return parseInt(document.getElementById("chart-range-select").value, 10);
}

function bucketForRange(rangeSeconds) {
  if (rangeSeconds <= 3600) return "minute";
  if (rangeSeconds <= 86400 * 2) return "hour";
  return "day";
}

// Series colours live in app.css (.chart .series-*), not here: the admin CSP
// forbids inline styles, so a style="" swatch would render colourless.
const SERIES_CLASSES = ["series-a", "series-b", "series-c", "series-d"];

function seriesClass(i) {
  return SERIES_CLASSES[i % SERIES_CLASSES.length];
}

function renderLineChart(container, series) {
  // series: [{name, points: [{t, v}]}]
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
    const d = s.points.map((p, idx) => `${idx === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    svg += `<path class="${seriesClass(i)}" d="${d}" />`;
  });
  svg += `</svg>`;

  const legend = series
    .map((s, i) => `<span><span class="dot ${seriesClass(i)}"></span>${escapeHtml(s.name)}</span>`)
    .join("");

  container.innerHTML = `<div class="legend">${legend}</div>${svg}`;
}

// DM and channel as two series rather than a transport picker: the split is
// the interesting part of the shape, and one line at a time hides it. Order
// fixes the colours — DM is series-a (blue), channel series-b (red).
async function messageSeries(qs, direction) {
  const series = [];
  for (const [transport, name] of [["dm", "DM"], ["channel", "Channel"]]) {
    const resp = await api(`/stats/messages?${qs}&direction=${direction}&transport=${transport}`);
    const data = await resp.json();
    series.push({ name, points: data.map((d) => ({ t: d.t, v: d.count })) });
  }
  return series;
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

    renderLineChart(document.getElementById("chart-rx"), await messageSeries(qs, "rx"));
    renderLineChart(document.getElementById("chart-tx"), await messageSeries(qs, "tx"));

    const delResp = await api(`/stats/delivery?${qs}`);
    const delData = await delResp.json();
    renderLineChart(document.getElementById("chart-delivery"), [
      { name: "Delivered", points: delData.map((d) => ({ t: d.t, v: d.delivered })) },
      { name: "Not acknowledged", points: delData.map((d) => ({ t: d.t, v: d.failed })) },
    ]);
    const delivered = delData.reduce((n, d) => n + d.delivered, 0);
    const failed = delData.reduce((n, d) => n + d.failed, 0);
    document.getElementById("delivery-rate").textContent =
      deliveryRate(delivered, failed, `${delivered + failed} measured`);
  } catch (e) {
    /* view not active or transient error */
  }
}

document.getElementById("chart-range").addEventListener("change", loadCharts);

// ---------------------------------------------------------------------
// Radio. Node state, channels and contacts, plus the recent-message windows
// the bot keeps in memory for the channels it serves.
// ---------------------------------------------------------------------

// RF settings do not change on their own, and the server caches the device
// query behind radio_cache_seconds anyway — a faster poll returns identical
// bytes at the cost of serial traffic on a Pi.
const RADIO_POLL_MS = 30000;

const radioPanelEl = document.getElementById("radio-panel");
const contactsTableEl = document.getElementById("contacts-table");
const contactsCountEl = document.getElementById("contacts-count");
const radioMessagesEl = document.getElementById("radio-messages");
const channelAddEl = document.getElementById("channel-add");
const channelFormEl = document.getElementById("channel-add-form");
const channelSlotEl = document.getElementById("channel-add-slot");
const channelNameEl = document.getElementById("channel-add-name");
const channelKeyEl = document.getElementById("channel-add-key");
const channelKeyLabelEl = document.getElementById("channel-add-key-label");
const channelNoteEl = document.getElementById("channel-add-note");
const channelErrorEl = document.getElementById("channel-add-error");

function startRadioPolling() {
  refreshRadio();
  stopRadioPolling();
  state.radioTimer = setInterval(refreshRadio, RADIO_POLL_MS);
}

function stopRadioPolling() {
  if (state.radioTimer) {
    clearInterval(state.radioTimer);
    state.radioTimer = null;
  }
}

function resetRadioView() {
  state.contactsShown = false;
  contactsTableEl.hidden = true;
  closeRadioMessages();
  closeChannelForm();
}

async function refreshRadio() {
  try {
    // The panel first, not in parallel: it carries writes.enabled, and the
    // channel view needs to know whether to offer the Add form before it
    // renders. Two sequential requests on a 30 s poll cost nothing.
    await loadRadioPanel();
    await loadChannels();
    if (state.contactsShown) await loadContacts();
  } catch (e) {
    /* transient; the next poll retries */
  }
}

// Every value below is either a number the server computed or a string from
// the mesh, so the panel is built with textContent rather than innerHTML.
function loadRadioPanelInto(el, data) {
  el.replaceChildren();
  // Before the connected check: this reports config, not radio state.
  state.radioWrites = !!(data.writes && data.writes.enabled);
  if (!data.connected) {
    const p = document.createElement("p");
    p.className = "mono";
    p.textContent = "No radio connected.";
    el.appendChild(p);
    return;
  }

  const r = data.radio || {};
  const fw = data.firmware || {};
  const loc = data.location || {};
  const rows = [
    [data.name || "—", data.public_key || "", fwLabel(fw)],
    [
      `${num(r.freq_mhz)} MHz`,
      `BW ${num(r.bandwidth_khz)} kHz`,
      `SF${num(r.spreading_factor)}`,
      `CR${num(r.coding_rate)}`,
      `${num(r.tx_power_dbm)} dBm${r.max_tx_power_dbm != null ? ` / ${r.max_tx_power_dbm}` : ""}`,
      // null means the firmware did not report it, which is not the same
      // fact as a path hash size of zero.
      `path hash ${r.path_hash_size == null ? "unknown" : r.path_hash_size + " B"}`,
    ],
    [loc.set ? `${loc.lat}, ${loc.lon}` : "location not set"],
  ];

  for (const parts of rows) {
    const line = document.createElement("p");
    line.className = "radio-line";
    line.textContent = parts.filter((p) => p !== "").join(" \u00b7 ");
    el.appendChild(line);
  }

  if (data.stale_since) {
    const warn = document.createElement("p");
    warn.className = "radio-stale";
    warn.textContent = `Radio unreachable — showing values from ${fmtTime(data.stale_since)}.`;
    el.appendChild(warn);
  }

  contactsCountEl.textContent = countLabel(data.contacts);
}

function fwLabel(fw) {
  return [fw.model, fw.version].filter(Boolean).join(" \u00b7 ");
}

function num(v) {
  return v == null ? "?" : v;
}

function countLabel(c) {
  if (!c) return "";
  return c.max != null ? `${c.count} / ${c.max}` : String(c.count);
}

async function loadRadioPanel() {
  const resp = await api("/radio");
  loadRadioPanelInto(radioPanelEl, await resp.json());
}

async function loadChannels() {
  const resp = await api("/radio/channels");
  const data = await resp.json();
  const tbody = document.querySelector("#channels-table tbody");
  tbody.replaceChildren();

  for (const c of data.channels || []) {
    const tr = document.createElement("tr");
    // n/a and — are different answers. An untracked channel is one the bot
    // does not watch at all; a tracked one with no traffic is simply quiet.
    // Rendering both as a dash would call an unmonitored channel silent.
    const count = c.tracked ? String(c.message_count) : "n/a";
    const last = c.tracked ? (c.last_message_at ? fmtTime(c.last_message_at) : "\u2014") : "n/a";
    appendCells(tr, [
      String(c.idx), c.name || "\u2014", c.hash || "\u2014",
      c.role || "\u2014", count, last,
    ]);
    const actions = document.createElement("td");
    actions.className = "row-actions";
    if (c.tracked) {
      const btn = document.createElement("button");
      btn.className = "link";
      btn.textContent = "Messages";
      btn.addEventListener("click", () => openChannelMessages(c.idx, c.name));
      actions.appendChild(btn);
    }
    if (c.editable) {
      const btn = document.createElement("button");
      btn.className = "link";
      btn.textContent = "Remove";
      btn.addEventListener("click", () => removeChannel(c.idx, c.name));
      actions.appendChild(btn);
    } else if (c.role) {
      // Not a disabled button: this row is not a thing the console edits.
      // The bot rewrites its own channels from config on every startup.
      const owned = document.createElement("span");
      owned.className = "mono";
      owned.textContent = "config";
      owned.title = `owned by ${c.role === "zork" ? "[channel]" : "[bots_channel]"} in zorkbot.toml`;
      actions.appendChild(owned);
    }
    tr.appendChild(actions);
    tbody.appendChild(tr);
  }

  refreshChannelForm(data.free_slots || []);
}

// ---------------------------------------------------------------------
// Channel writes. Add and remove are the same firmware command — there is
// no delete on the device, only assignment to a slot that always exists.
// See docs/specs/admin-radio-edit.md.
// ---------------------------------------------------------------------

function refreshChannelForm(freeSlots) {
  const offer = state.radioWrites && freeSlots.length > 0;
  channelAddEl.hidden = !offer;
  if (!offer) {
    closeChannelForm();
    return;
  }
  const previous = channelSlotEl.value;
  channelSlotEl.replaceChildren();
  for (const idx of freeSlots) {
    const option = document.createElement("option");
    option.value = String(idx);
    option.textContent = String(idx);
    channelSlotEl.appendChild(option);
  }
  if (freeSlots.includes(Number(previous))) channelSlotEl.value = previous;
}

function closeChannelForm() {
  channelFormEl.hidden = true;
  channelFormEl.reset();
  // The key is the only secret typed into this console; it should not
  // outlive the request that carries it.
  channelKeyEl.value = "";
  channelErrorEl.textContent = "";
  syncChannelKeyField();
}

// The server enforces these rules regardless — the form is not the only
// caller — but mirroring them here means the operator learns them by using
// it rather than by collecting 400s.
function syncChannelKeyField() {
  const isPublic = channelNameEl.value.trim().startsWith("#");
  channelKeyEl.disabled = isPublic;
  channelKeyEl.required = !isPublic;
  channelKeyLabelEl.hidden = isPublic;
  if (isPublic) channelKeyEl.value = "";
  channelNoteEl.textContent = isPublic
    ? "Names starting with # are public: the key is derived from the name, so anyone who knows it can join."
    : "This key is never shown again. Keep it wherever you keep the channel's other copies.";
}

channelNameEl.addEventListener("input", syncChannelKeyField);

document.getElementById("channel-add-show").addEventListener("click", () => {
  channelFormEl.hidden = !channelFormEl.hidden;
  if (channelFormEl.hidden) closeChannelForm();
  else syncChannelKeyField();
});

document.getElementById("channel-add-cancel").addEventListener("click", closeChannelForm);

channelFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  channelErrorEl.textContent = "";
  const name = channelNameEl.value.trim();
  const body = { name };
  if (!name.startsWith("#")) body.secret = channelKeyEl.value.trim();

  const resp = await api(`/radio/channels/${encodeURIComponent(channelSlotEl.value)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    channelErrorEl.textContent = await errorText(resp, "Could not add the channel.");
    return;
  }
  closeChannelForm();
  await loadChannels();
});

async function removeChannel(idx, name) {
  // The warning is only true of a keyed channel. A "#" channel's key is
  // derived from its name, so re-adding it costs nothing but typing.
  const warning = name.startsWith("#")
    ? ""
    : "\n\nThe console cannot show you the key again, so re-adding it means " +
      "having your own copy.";
  const ok = window.confirm(`Remove ${name} from slot ${idx}?${warning}`);
  if (!ok) return;

  const resp = await api(`/radio/channels/${encodeURIComponent(idx)}`, { method: "DELETE" });
  if (!resp.ok) {
    window.alert(await errorText(resp, "Could not remove the channel."));
    return;
  }
  await loadChannels();
}

// Error bodies from this API are {detail: {error, error_description}}; the
// description is written for the operator, so prefer it over a generic line.
async function errorText(resp, fallback) {
  try {
    const body = await resp.json();
    return (body.detail && body.detail.error_description) || fallback;
  } catch (e) {
    return fallback;
  }
}

async function loadContacts() {
  const resp = await api("/radio/contacts");
  const data = await resp.json();
  const tbody = document.querySelector("#contacts-table tbody");
  tbody.replaceChildren();

  const contacts = data.contacts || [];
  if (contacts.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 9;
    td.className = "mono";
    td.textContent = "No contacts.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const c of contacts) {
    const tr = document.createElement("tr");
    appendCells(tr, [
      c.name || "\u2014",
      c.pubkey_prefix,
      c.type,
      c.last_advert_at ? fmtTime(c.last_advert_at) : "\u2014",
      c.distance_km == null ? "\u2014" : `${c.distance_km} km`,
      // "flood" is a routing mode, not a missing hop count.
      c.routing === "flood" ? "flood" : `${c.hops} hop${c.hops === 1 ? "" : "s"}`,
      c.path || "\u2014",
      c.path_hash_size == null ? "\u2014" : `${c.path_hash_size} B`,
    ]);
    const actions = document.createElement("td");
    if (c.message_count > 0) {
      const btn = document.createElement("button");
      btn.className = "link";
      btn.textContent = `Messages (${c.message_count})`;
      btn.addEventListener("click", () => openContactMessages(c.pubkey_prefix, c.name));
      actions.appendChild(btn);
    }
    tr.appendChild(actions);
    tbody.appendChild(tr);
  }
}

function appendCells(tr, values) {
  for (const value of values) {
    const td = document.createElement("td");
    td.textContent = value;
    tr.appendChild(td);
  }
}

document.getElementById("contacts-show").addEventListener("click", async () => {
  state.contactsShown = !state.contactsShown;
  contactsTableEl.hidden = !state.contactsShown;
  document.getElementById("contacts-show").textContent = state.contactsShown
    ? "Hide contacts"
    : "Show contacts";
  if (state.contactsShown) await loadContacts();
});

function closeRadioMessages() {
  radioMessagesEl.hidden = true;
}

document.getElementById("radio-messages-close").addEventListener("click", closeRadioMessages);

async function openContactMessages(prefix, name) {
  await openRadioMessages(
    `/radio/contacts/${encodeURIComponent(prefix)}/messages`,
    `Messages with ${name || prefix}`
  );
}

async function openChannelMessages(idx, name) {
  await openRadioMessages(
    `/radio/channels/${encodeURIComponent(idx)}/messages`,
    `Messages on ${name || "channel " + idx}`
  );
}

async function openRadioMessages(path, title) {
  const list = document.getElementById("radio-messages-list");
  document.getElementById("radio-messages-title").textContent = title;
  list.replaceChildren();
  radioMessagesEl.hidden = false;

  const note = document.getElementById("radio-messages-note");
  const resp = await api(path);
  if (!resp.ok) {
    note.textContent = "Could not load messages.";
    return;
  }
  const data = await resp.json();
  note.textContent = data.since_process_start
    ? `Last ${data.window} messages seen since the bot started \u2014 not a full history.`
    : "";

  if (!data.messages || data.messages.length === 0) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = "Nothing seen yet.";
    list.appendChild(empty);
    return;
  }
  for (const m of data.messages) appendRadioMessage(list, m);
  list.scrollTop = list.scrollHeight;
}

// Message text and sender names come from the mesh, so both go in via
// textContent — the same rule the log tail follows.
function appendRadioMessage(list, m) {
  const row = document.createElement("div");
  row.className = `msg-line msg-${m.direction}`;

  const at = document.createElement("span");
  at.className = "msg-at";
  at.textContent = new Date(m.at * 1000).toLocaleTimeString();

  const who = document.createElement("span");
  who.className = "msg-who";
  who.textContent = m.sender_name || (m.pubkey_prefix || "?").slice(0, 8);

  const text = document.createElement("span");
  text.className = "msg-text";
  text.textContent = m.text;

  row.append(at, who);
  // A channel message carries no sender key material, so the name is
  // whatever the sender typed. Saying so is the point: rendering a claimed
  // name like a cryptographic one teaches the operator to trust it.
  if (!m.sender_verified) {
    const mark = document.createElement("span");
    mark.className = "msg-unverified";
    mark.textContent = "unverified";
    mark.title = "Channel messages carry no sender identity; this name is self-reported.";
    row.appendChild(mark);
  }
  row.appendChild(text);
  list.appendChild(row);
}

// ---------------------------------------------------------------------
// Uptime. The string comes formatted from /api/status so it reads exactly
// as the !uptime channel command answers; the SPA never reformats it.
// ---------------------------------------------------------------------

// Coarse on purpose: /status also health-checks the game service, and the
// format is minute-granular above an hour, so a tighter poll would buy
// nothing but load on the Pi.
const UPTIME_POLL_MS = 15000;

const uptimeEl = document.getElementById("log-uptime");

async function refreshUptime() {
  try {
    const resp = await api("/status");
    const data = await resp.json();
    uptimeEl.textContent = data.uptime || "\u2014";
  } catch (e) {
    // Transient failure — keep the last known value; the next poll retries.
  }
}

function startUptimePolling() {
  refreshUptime();
  stopUptimePolling();
  state.uptimeTimer = setInterval(refreshUptime, UPTIME_POLL_MS);
}

function stopUptimePolling() {
  if (state.uptimeTimer) {
    clearInterval(state.uptimeTimer);
    state.uptimeTimer = null;
  }
}

// ---------------------------------------------------------------------
// Log tail. A live view on the running process, streamed from the same
// in-memory ring the server replays on connect — no history, no persistence.
// ---------------------------------------------------------------------

// Independent of the server's ring: a tab left open for a day would otherwise
// accumulate unboundedly in the DOM.
const LOG_MAX_LINES = 2000;

const logLinesEl = document.getElementById("log-lines");
const logStreamEl = document.getElementById("log-stream");
const logStatusEl = document.getElementById("log-status");
const logFilterEl = document.getElementById("log-filter");
const logLevelEl = document.getElementById("log-level");
const logFollowEl = document.getElementById("log-follow");

function logStatus(text) {
  logStatusEl.textContent = text;
}

function stopLogStream() {
  if (state.logReconnect) {
    clearTimeout(state.logReconnect);
    state.logReconnect = null;
  }
  if (state.logStreamAbort) {
    state.logStreamAbort.abort();
    state.logStreamAbort = null;
  }
}

function logsTabActive() {
  return !document.getElementById("tab-logs").hidden;
}

async function startLogStream() {
  stopLogStream();
  if (logLinesEl.childElementCount === 0) renderLogLines();
  const controller = new AbortController();
  state.logStreamAbort = controller;
  const level = logLevelEl.value;
  logStatus("connecting\u2026");

  try {
    const resp = await openStream(`/logs/stream?level=${encodeURIComponent(level)}`, controller.signal);
    if (resp.status === 401) {
      logStatus("signed out");
      return;
    }
    if (resp.status === 429) {
      logStatus("too many log streams open \u2014 close another tab");
      return;
    }
    if (!resp.ok || !resp.body) {
      logStatus("could not open stream");
      return;
    }
    await readSse(resp, handleLogEvent);
    logStatus("stream closed");
  } catch (e) {
    if (e.name === "AbortError") return;
    logStatus("stream closed");
  }
  // The server closes on shutdown and any proxy in front may time the
  // connection out; a log tail that silently stops is worse than useless.
  if (state.logStreamAbort === controller && logsTabActive()) {
    state.logReconnect = setTimeout(startLogStream, 2000);
  }
}

function handleLogEvent(event, data) {
  if (event === "hello") {
    logStatus(`streaming \u00b7 process level ${data.root_level}`);
    return;
  }
  if (event !== "log") return;
  // A reconnect replays the server's whole ring buffer; the watermark keeps
  // the lines we already have from being appended a second time.
  if (data.seq <= state.logLastSeq) return;
  state.logLastSeq = data.seq;
  clearLogPlaceholder();
  if (data.dropped_before) appendLogGap(data.dropped_before);
  state.logRecords.push(data);
  if (state.logRecords.length > LOG_MAX_LINES) state.logRecords.shift();
  if (logMatchesFilter(data)) appendLogLine(data);
  trimLogLines();
  followLogTail();
}

function logMatchesFilter(record) {
  const needle = logFilterEl.value.trim().toLowerCase();
  if (!needle) return true;
  return (
    record.message.toLowerCase().includes(needle) ||
    record.logger.toLowerCase().includes(needle)
  );
}

// Records carry arbitrary text from anywhere in the process, so every field
// goes in via textContent — never innerHTML.
function appendLogLine(record) {
  const row = document.createElement("div");
  row.className = `log-line level-${String(record.level).toLowerCase()}`;

  const at = document.createElement("span");
  at.className = "log-at";
  at.textContent = new Date(record.at * 1000).toLocaleTimeString();

  const level = document.createElement("span");
  level.className = "log-level";
  level.textContent = record.level;

  const logger = document.createElement("span");
  logger.className = "log-logger";
  logger.textContent = record.logger;

  const message = document.createElement("span");
  message.className = "log-message";
  message.textContent = record.message;

  row.append(at, level, logger, message);
  logLinesEl.appendChild(row);
}

function appendLogGap(count) {
  const gap = document.createElement("div");
  gap.className = "log-gap";
  gap.textContent = `[${count} line${count === 1 ? "" : "s"} dropped \u2014 stream fell behind]`;
  logLinesEl.appendChild(gap);
}

function clearLogPlaceholder() {
  const placeholder = logLinesEl.querySelector(".log-empty");
  if (placeholder) placeholder.remove();
}

function trimLogLines() {
  while (logLinesEl.childElementCount > LOG_MAX_LINES) {
    logLinesEl.removeChild(logLinesEl.firstElementChild);
  }
}

function followLogTail() {
  if (logFollowEl.checked) logStreamEl.scrollTop = logStreamEl.scrollHeight;
}

function renderLogLines() {
  logLinesEl.replaceChildren();
  const matching = state.logRecords.filter(logMatchesFilter);
  if (matching.length === 0) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = state.logRecords.length ? "No lines match the filter." : "No log lines yet.";
    logLinesEl.appendChild(empty);
    return;
  }
  for (const record of matching) appendLogLine(record);
  followLogTail();
}

logFilterEl.addEventListener("input", renderLogLines);

// Level is a server-side filter, so changing it means a new stream — and a
// fresh backlog replay at the new level, which is what the operator wants.
logLevelEl.addEventListener("change", () => {
  state.logRecords = [];
  state.logLastSeq = 0;
  renderLogLines();
  startLogStream();
});

document.getElementById("log-clear").addEventListener("click", () => {
  // Clear hides what you have already read; the watermark stays put so the
  // stream keeps delivering only new lines.
  state.logRecords = [];
  renderLogLines();
});

// Signing out must not leave the previous session's log tail on screen.
function resetLogView() {
  state.logRecords = [];
  state.logLastSeq = 0;
  uptimeEl.textContent = "\u2014";
  renderLogLines();
}

// Scrolling up is how you say "let me read this" — stop yanking the view back.
logStreamEl.addEventListener("scroll", () => {
  const atBottom =
    logStreamEl.scrollHeight - logStreamEl.scrollTop - logStreamEl.clientHeight < 24;
  if (!atBottom && logFollowEl.checked) logFollowEl.checked = false;
});

logFollowEl.addEventListener("change", followLogTail);

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
      <td class="mono">${deliveryRate(p.dms_delivered, p.dms_undelivered)}</td>
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
