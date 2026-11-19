"use strict";
// Speed tests: internet (runs on the server) and Wi-Fi (runs here, in the
// browser, against the Network View server). Uses $ and esc from app.js.

const sp = {
  status: null,      // last /api/speedtest response
  wifi: null,        // in-progress Wi-Fi test state
  kind: "internet",  // history chart selection
  timer: null,
};

const LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]"];
const isLocalView = () => LOCAL_HOSTS.includes(location.hostname);

const fmtMbps = (v) => (v == null ? "—" : v >= 100 ? Math.round(v).toString() : v.toFixed(1));
const fmtMs = (v) => (v == null ? "—" : v >= 10 ? Math.round(v).toString() : v.toFixed(1));

// ---------- rendering the two cards ----------
function metricsHtml(r, live) {
  const m = (label, value, unit, swatch, isLive) =>
    `<div class="metric${isLive ? " live" : ""}"><div class="label">${swatch ? `<span class="swatch" style="background:${swatch}"></span>` : ""}${label}</div>
     <div class="value">${value}<small>${unit}</small></div></div>`;
  return [
    m("Download", fmtMbps(r.download_mbps), "Mbps", "var(--series-down)", live === "download"),
    m("Upload", fmtMbps(r.upload_mbps), "Mbps", "var(--series-up)", live === "upload"),
    m("Ping", fmtMs(r.latency_ms), "ms", null, live === "latency"),
    m("Jitter", fmtMs(r.jitter_ms), "ms", null, false),
  ].join("");
}

function setProgress(card, fraction) {
  const box = card.querySelector(".sp-progress");
  box.hidden = fraction == null;
  if (fraction != null) box.querySelector(".bar").style.width = `${Math.round(fraction * 100)}%`;
}

const PHASES = { latency: "Measuring ping…", download: "Measuring download…", upload: "Measuring upload…" };
const lastOf = (kind) => (sp.status?.history || []).filter((r) => r.kind === kind).at(-1);

function renderInternet() {
  const s = sp.status;
  if (!s) return;
  const card = $("spInternet");
  const running = s.state === "running";
  const coolingDown = s.cooldown_until && s.cooldown_until > now();
  $("spInternetBtn").disabled = running || coolingDown;
  if (coolingDown) {  // re-enable the button when the wait is over
    clearTimeout(sp.cooldownTimer);
    sp.cooldownTimer = setTimeout(renderInternet, (s.cooldown_until - now()) * 1000 + 500);
  }
  $("spInternetBtn").textContent = running ? "Testing…" : "Run test";
  const last = lastOf("internet");
  if (running) {
    const shown = { ...s.partial };
    if (s.phase === "download" || s.phase === "upload") shown[`${s.phase}_mbps`] = s.live_mbps;
    $("spInternetMetrics").innerHTML = metricsHtml(shown, s.phase);
    const phases = ["latency", "download", "upload"];
    setProgress(card, (phases.indexOf(s.phase) + (s.progress || 0)) / phases.length);
    $("spInternetStatus").className = "sp-status muted";
    $("spInternetStatus").textContent = PHASES[s.phase] || "Starting…";
    return;
  }
  setProgress(card, null);
  $("spInternetMetrics").innerHTML = metricsHtml(last || {});
  const st = $("spInternetStatus");
  if (s.state === "error") {
    st.className = "sp-status error";
    st.textContent = s.error;
  } else {
    st.className = "sp-status muted";
    st.textContent = last
      ? `Tested ${ago(last.at)}${last.server ? ` via Cloudflare ${last.server}` : ""} · used ${last.data_mb} MB of data` +
        (last.limited ? " · Cloudflare limited part of this test, so speeds may read a little low" : "")
      : "Not tested yet. A test takes up to 25 seconds and uses roughly 100–500 MB of data.";
  }
}

function renderWifi() {
  const card = $("spWifi");
  const w = sp.wifi;
  const btn = $("spWifiBtn");
  const st = $("spWifiStatus");
  if (w) {
    btn.disabled = true;
    btn.textContent = "Testing…";
    $("spWifiMetrics").innerHTML = metricsHtml(w.shown, w.phase);
    setProgress(card, w.progress);
    st.className = "sp-status muted";
    st.textContent = PHASES[w.phase] || "Starting…";
    return;
  }
  setProgress(card, null);
  const last = lastOf("wifi");
  $("spWifiMetrics").innerHTML = metricsHtml(last || {});
  if (isLocalView()) {
    // Same computer: there's no Wi-Fi between the browser and the server.
    btn.disabled = true;
    btn.textContent = "Run test";
    const lan = state.data?.listen?.lan_url;
    st.className = "sp-status muted";
    st.innerHTML = lan
      ? `Open <a href="${esc(lan)}">${esc(lan)}</a> on your phone or another device to test its Wi-Fi.`
      : `To test Wi-Fi, restart with <code>python3 -m netview --host 0.0.0.0</code>, then open this page from your phone using this computer's IP address.`;
    return;
  }
  btn.disabled = false;
  btn.textContent = "Run test";
  if (sp.wifiError) {
    st.className = "sp-status error";
    st.textContent = sp.wifiError;
  } else {
    st.className = "sp-status muted";
    st.textContent = last ? `Tested ${ago(last.at)}${last.client ? ` from ${last.client}` : ""}` : "Not tested yet. Takes about 15 seconds.";
  }
}

// ---------- history chart ----------
const SERIES = [
  { key: "download_mbps", name: "Download", color: "var(--series-down)" },
  { key: "upload_mbps", name: "Upload", color: "var(--series-up)" },
];

// Round tick steps (1, 2, 2.5, 5 × 10^n), about four of them.
function niceTicks(v) {
  const target = Math.max(v, 1) / 4;
  const p = 10 ** Math.floor(Math.log10(target));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((n) => n >= target);
  const top = Math.ceil((v * 1.08) / step) * step || step * 4;
  return Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
}

function renderHistory() {
  const rows = (sp.status?.history || []).filter((r) => r.kind === sp.kind).slice(-40);
  $("spLegend").innerHTML = SERIES.map((s) =>
    `<span><span class="swatch" style="background:${s.color}"></span>${s.name} (Mbps)</span>`).join("");

  const box = $("spChart");
  const W = Math.max(280, box.clientWidth || 600), H = 220;
  const m = { top: 12, right: 92, bottom: 26, left: 44 };
  const iw = W - m.left - m.right, ih = H - m.top - m.bottom;
  if (!rows.length) {
    box.innerHTML = `<svg viewBox="0 0 ${W} 90" style="height:90px" role="img" aria-label="No results yet">
      <text class="empty" x="${W / 2}" y="50" text-anchor="middle">No ${sp.kind === "wifi" ? "Wi-Fi" : "internet"} tests yet. Results will appear here.</text></svg>`;
    renderTable(rows);
    return;
  }
  const t0 = rows[0].at, t1 = rows.at(-1).at;
  const x = (t) => (t1 === t0 ? m.left + iw / 2 : m.left + ((t - t0) / (t1 - t0)) * iw);
  const ticks = niceTicks(Math.max(...rows.flatMap((r) => SERIES.map((s) => r[s.key] || 0))));
  const ymax = ticks.at(-1);
  const y = (v) => m.top + ih - (v / ymax) * ih;

  const grid = ticks.map((v) => `<line x1="${m.left}" x2="${m.left + iw}" y1="${y(v)}" y2="${y(v)}"/>`).join("");
  const ylabels = ticks.map((v) => `<text x="${m.left - 8}" y="${y(v) + 4}" text-anchor="end">${Number.isInteger(v) ? v : v.toFixed(2).replace(/0$/, "")}</text>`).join("");
  // Same-day history: show times; longer: show dates.
  const dateFmt = t1 - t0 < 2 * 86400
    ? (t) => new Date(t * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : (t) => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const xlabels = (t1 === t0 ? [t0] : [t0, t1]).map((t, i, a) =>
    `<text x="${x(t)}" y="${H - 6}" text-anchor="${a.length === 1 ? "middle" : i ? "end" : "start"}">${dateFmt(t)}</text>`).join("");

  // Direct labels at the line ends; nudge apart if they'd overlap.
  const ends = SERIES.map((s) => ({ s, v: rows.at(-1)[s.key] })).filter((e) => e.v != null)
    .map((e) => ({ ...e, ly: y(e.v) + 4 }));
  if (ends.length === 2 && Math.abs(ends[0].ly - ends[1].ly) < 14) {
    const [hi, lo] = ends[0].ly < ends[1].ly ? ends : [ends[1], ends[0]];
    const mid = (hi.ly + lo.ly) / 2;
    hi.ly = mid - 7;
    lo.ly = mid + 7;
  }

  const lines = SERIES.map((s) => {
    const pts = rows.filter((r) => r[s.key] != null);
    const d = pts.map((r, i) => `${i ? "L" : "M"}${x(r.at).toFixed(1)},${y(r[s.key]).toFixed(1)}`).join("");
    const dots = pts.map((r) => `<circle class="dot" cx="${x(r.at)}" cy="${y(r[s.key])}" r="4" fill="${s.color}"/>`).join("");
    return `<path class="line" d="${d}" stroke="${s.color}"/>${dots}`;
  }).join("");
  const labels = ends.map((e) =>
    `<text class="end-label" x="${x(rows.at(-1).at) + 10}" y="${e.ly}">${e.s.name} ${fmtMbps(e.v)}</text>`).join("");

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${sp.kind} speed history, ${rows.length} tests">
    <g class="grid">${grid}</g>
    <line class="baseline" x1="${m.left}" x2="${m.left + iw}" y1="${y(0)}" y2="${y(0)}"/>
    <g class="axis">${ylabels}${xlabels}</g>
    ${lines}${labels}
    <line class="crosshair" y1="${m.top}" y2="${m.top + ih}" visibility="hidden"/>
    <rect x="${m.left - 10}" y="0" width="${iw + 20}" height="${H}" fill="transparent" class="hit"/>
  </svg><div class="tooltip" hidden></div>`;

  // Hover: snap to the nearest test, crosshair + tooltip.
  const svg = box.querySelector("svg"), cross = svg.querySelector(".crosshair"), tip = box.querySelector(".tooltip");
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    const r = rows.reduce((a, b) => (Math.abs(x(b.at) - px) < Math.abs(x(a.at) - px) ? b : a));
    cross.setAttribute("x1", x(r.at));
    cross.setAttribute("x2", x(r.at));
    cross.setAttribute("visibility", "visible");
    tip.innerHTML = `<div class="t-head">${esc(new Date(r.at * 1000).toLocaleString())}</div>` +
      SERIES.map((s) => `<div class="t-row"><span class="swatch" style="background:${s.color}"></span>${s.name}<b>${fmtMbps(r[s.key])} Mbps</b></div>`).join("") +
      `<div class="t-row">Ping<b>${fmtMs(r.latency_ms)} ms</b></div>`;
    tip.hidden = false;
    const scale = rect.width / W;
    const left = x(r.at) * scale + 12;
    tip.style.left = `${left + tip.offsetWidth > rect.width ? x(r.at) * scale - tip.offsetWidth - 12 : left}px`;
    tip.style.top = `${m.top * scale}px`;
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; };
  const hit = svg.querySelector(".hit");
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerdown", move);
  hit.addEventListener("pointerleave", hide);
  renderTable(rows);
}

function renderTable(rows) {
  const extra = sp.kind === "wifi" ? "Device" : "Server";
  $("spTable").innerHTML = `<thead><tr><th>When</th><th class="num">Download</th><th class="num">Upload</th>
    <th class="num">Ping</th><th class="num">Jitter</th><th>${extra}</th></tr></thead><tbody>` +
    rows.slice().reverse().map((r) => `<tr><td>${esc(new Date(r.at * 1000).toLocaleString())}</td>
      <td class="num">${fmtMbps(r.download_mbps)} Mbps</td><td class="num">${fmtMbps(r.upload_mbps)} Mbps</td>
      <td class="num">${fmtMs(r.latency_ms)} ms</td><td class="num">${fmtMs(r.jitter_ms)} ms</td>
      <td>${esc((sp.kind === "wifi" ? r.client : r.server) || "—")}</td></tr>`).join("") + "</tbody>";
}

function renderSpeed() {
  renderInternet();
  renderWifi();
  renderHistory();
}

// ---------- internet test (server side) ----------
async function loadSpeed() {
  clearTimeout(sp.timer);
  try {
    sp.status = await api("/api/speedtest");
    renderSpeed();
  } catch { /* server unreachable; the device list shows that */ }
  if (sp.status?.state === "running") sp.timer = setTimeout(loadSpeed, 400);
}

$("spInternetBtn").addEventListener("click", async () => {
  $("spInternetBtn").disabled = true;
  await api("/api/speedtest/start", { method: "POST" }).catch(() => {});
  loadSpeed();
});

// ---------- Wi-Fi test (browser side) ----------
const WIFI = { streams: 4, maxS: 8, minS: 3, warmS: 1, cap: 300e6, downChunk: 25e6, upChunk: 8e6 };

function clientLabel() {
  const ua = navigator.userAgent;
  const dev = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android"
    : /Macintosh/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows PC" : /CrOS/.test(ua) ? "Chromebook"
    : /Linux/.test(ua) ? "Linux" : "Unknown device";
  const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "";
  return br ? `${dev} · ${br}` : dev;
}

async function measureLatency(count = 10) {
  const rtts = [];
  for (let i = 0; i <= count; i++) {
    const t0 = performance.now();
    const r = await fetch(`/api/speedtest/lan/ping?i=${i}&r=${Math.random()}`, { cache: "no-store" });
    await r.text();
    if (i) rtts.push(performance.now() - t0); // first one may include connection setup
    sp.wifi.progress = (i / count) / 3;
    renderWifi();
  }
  rtts.sort((a, b) => a - b);
  const median = rtts[Math.floor(rtts.length / 2)];
  let jitter = 0;
  for (let i = 1; i < rtts.length; i++) jitter += Math.abs(rtts[i] - rtts[i - 1]);
  return { latency: median, jitter: jitter / Math.max(1, rtts.length - 1) };
}

// Run `worker` on several streams until time/data limits; return Mbps.
async function measureThroughput(phaseIndex, worker) {
  const ctrl = new AbortController();
  let total = 0;
  const add = (n) => { total += n; };
  const start = performance.now();
  const samples = [[0, 0]];
  const workers = Array.from({ length: WIFI.streams }, () => worker(ctrl.signal, add).catch(() => {}));
  await new Promise((resolve) => {
    const tick = setInterval(() => {
      const t = (performance.now() - start) / 1000;
      samples.push([t, total]);
      const recent = samples.filter((s) => s[0] >= t - 1);
      const [ta, ba] = recent[0], [tb, bb] = recent.at(-1);
      sp.wifi.shown[phaseIndex === 1 ? "download_mbps" : "upload_mbps"] = ((bb - ba) * 8) / 1e6 / Math.max(0.2, tb - ta);
      sp.wifi.progress = (phaseIndex + Math.min(1, t / WIFI.maxS)) / 3;
      renderWifi();
      if (t >= WIFI.maxS || (t >= WIFI.minS && total >= WIFI.cap)) {
        clearInterval(tick);
        resolve();
      }
    }, 200);
  });
  ctrl.abort();
  await Promise.all(workers);
  const [tEnd, bEnd] = samples.at(-1);
  let warm = samples.find((s) => s[0] >= WIFI.warmS) || samples[0];
  if (tEnd - warm[0] < 1) warm = samples[0];
  if (!bEnd) throw new Error("no data moved");
  return ((bEnd - warm[1]) * 8) / 1e6 / (tEnd - warm[0]);
}

async function downloadWorker(signal, add) {
  while (!signal.aborted) {
    const r = await fetch(`/api/speedtest/lan/down?bytes=${WIFI.downChunk}&r=${Math.random()}`, { signal, cache: "no-store" });
    const reader = r.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      add(value.length);
    }
  }
}

let uploadBlob = null;
function makeUploadBlob() {
  if (uploadBlob) return uploadBlob;
  const block = new Uint8Array(65536);
  crypto.getRandomValues(block); // incompressible
  uploadBlob = new Blob(Array.from({ length: WIFI.upChunk / 65536 }, () => block), { type: "application/octet-stream" });
  return uploadBlob;
}

function uploadWorker(signal, add) {
  const once = () => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let sent = 0;
    xhr.upload.onprogress = (e) => { add(e.loaded - sent); sent = e.loaded; };
    xhr.onload = () => (xhr.status < 400 ? resolve() : reject(new Error(`HTTP ${xhr.status}`)));
    xhr.onerror = xhr.onabort = () => reject(new Error("stopped"));
    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.open("POST", "/api/speedtest/lan/up");
    xhr.send(makeUploadBlob());
  });
  return (async () => { while (!signal.aborted) await once(); })();
}

$("spWifiBtn").addEventListener("click", async () => {
  sp.wifi = { phase: "latency", progress: 0, shown: {} };
  sp.wifiError = null;
  renderWifi();
  try {
    const { latency, jitter } = await measureLatency();
    Object.assign(sp.wifi.shown, { latency_ms: latency, jitter_ms: jitter });
    sp.wifi.phase = "download";
    const down = await measureThroughput(1, downloadWorker);
    sp.wifi.shown.download_mbps = down;
    sp.wifi.phase = "upload";
    const up = await measureThroughput(2, uploadWorker);
    await api("/api/speedtest/results", {
      method: "POST",
      body: JSON.stringify({ download_mbps: down, upload_mbps: up, latency_ms: latency, jitter_ms: jitter, client: clientLabel() }),
    });
  } catch (e) {
    sp.wifiError = `The Wi-Fi test failed (${e.message}). Check that this device can still reach Network View, then try again.`;
  }
  sp.wifi = null;
  await loadSpeed();
});

document.querySelectorAll(".speed-history .seg button").forEach((b) => b.addEventListener("click", () => {
  sp.kind = b.dataset.kind;
  document.querySelectorAll(".speed-history .seg button").forEach((x) => x.classList.toggle("on", x === b));
  renderHistory();
}));

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderHistory, 150);
});

loadSpeed();
