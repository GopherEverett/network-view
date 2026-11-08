"use strict";

const $ = (id) => document.getElementById(id);
const DAY = 86400;
const TYPE_ICONS = {
  "This computer": "💻", "Router": "📡", "Computer": "🖥️", "Phone": "📱", "Tablet": "📱",
  "Phone / Tablet": "📱", "Apple device": "🍏", "TV / Streaming": "📺", "Game console": "🎮",
  "Printer": "🖨️", "Camera": "📷", "Smart home": "💡", "Network gear": "🔀",
  "NAS / Server": "🗄️", "Speaker": "🔊", "Smart watch": "⌚", "Unknown": "❔",
};

const state = {
  data: null,
  show: "online",
  sort: { key: "ip", desc: false },
  selected: null,
  pollTimer: null,
};

// ---------- helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ipNum = (ip) => ip.split(".").reduce((n, o) => n * 256 + +o, 0);
const now = () => Date.now() / 1000;

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, now() - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < DAY) return `${Math.floor(s / 3600)} h ago`;
  if (s < 30 * DAY) return `${Math.floor(s / DAY)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
const fullDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");
// Custom name, then what the device calls itself, then DNS, then the maker.
const displayName = (d) => d.name || d.friendly_name || d.hostname || d.model || d.vendor || "Unknown device";
const portsOf = (d) => {
  const full = d.ports_scanned_at || 0, quick = d.quick_ports_at || 0;
  if (d.open_ports != null && full >= quick) return { ports: d.open_ports, at: full, quick: false };
  if (d.quick_ports != null) return { ports: d.quick_ports, at: quick, quick: true };
  return null;
};
const isNew = (d) => {
  const baseline = state.data?.baseline_until;
  return !!(baseline && d.first_seen > baseline && now() - d.first_seen < DAY);
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok && res.status !== 202) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ---------- data ----------
async function refresh() {
  try {
    state.data = await api("/api/devices");
    render();
  } catch (e) {
    $("netInfo").textContent = `Can't reach the server (${e.message})`;
  }
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  const scanning = state.data?.status.state === "scanning";
  const downloading = state.data?.vendor_db?.download?.state === "downloading";
  state.pollTimer = setTimeout(refresh, scanning ? 1000 : downloading ? 3000 : 15000);
}

// ---------- rendering ----------
function render() {
  const { network: net, status, devices } = state.data;

  // Header
  if (net) {
    $("netInfo").innerHTML =
      `Subnet <b>${esc(net.cidr)}</b> · this computer <b>${esc(net.local_ip)}</b>` +
      (net.gateway ? ` · router <b>${esc(net.gateway)}</b>` : "") +
      (net.interface ? ` · <span class="mono">${esc(net.interface)}</span>` : "");
  }
  const scanning = status.state === "scanning";
  $("scanBtn").disabled = scanning;
  $("scanBtn").textContent = scanning ? "Scanning…" : "Scan now";
  $("progress").hidden = !scanning;
  const pct = status.total ? (status.done / status.total) * 100 : 5;
  $("progressBar").style.width = `${status.phase === "probing" ? pct : 95}%`;

  // Stats
  const online = devices.filter((d) => d.online);
  const newToday = devices.filter(isNew);
  const lat = online.map((d) => d.latency_ms).filter((v) => v != null && v > 0);
  const avgLat = lat.length ? (lat.reduce((a, b) => a + b, 0) / lat.length).toFixed(1) : "—";
  const lastScan = status.state === "error" ? `Error: ${esc(status.error)}`
    : scanning ? `${esc(status.phase || "starting")}…` : ago(status.last_scan);
  $("stats").innerHTML = [
    stat("Online now", online.length, `of ${devices.length} ever seen`),
    stat("New today", newToday.length, newToday.length ? "first seen in last 24 h" : "nothing new"),
    stat("Avg latency", avgLat === "—" ? "—" : `${avgLat} ms`, "across online devices"),
    stat("Last scan", scanning ? "…" : ago(status.last_scan).replace(" ago", ""), scanning ? lastScan :
      status.duration_s != null ? `took ${status.duration_s}s` : lastScan),
  ].join("");

  // Type filter options
  const types = [...new Set(devices.map((d) => d.type).filter(Boolean))].sort();
  const sel = $("typeFilter");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All types</option>` +
    types.map((t) => `<option ${t === cur ? "selected" : ""}>${esc(t)}</option>`).join("");

  // Rows
  const q = $("search").value.trim().toLowerCase();
  let rows = devices.filter((d) =>
    (state.show === "all" || (state.show === "online") === !!d.online) &&
    (!sel.value || d.type === sel.value) &&
    (!q || [d.name, d.friendly_name, d.hostname, d.model, d.manufacturer, d.ip, d.mac, d.vendor, d.type, d.notes].some((v) => v && v.toLowerCase().includes(q))));
  rows = sortRows(rows);

  $("rows").innerHTML = rows.map(rowHtml).join("");
  const empty = $("empty");
  empty.hidden = rows.length > 0;
  empty.textContent = scanning && !devices.length ? "Scanning your network…"
    : devices.length ? "No devices match these filters." : "No devices found yet.";

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sort.key);
    th.classList.toggle("desc", th.dataset.sort === state.sort.key && state.sort.desc);
  });

  const vdb = state.data.vendor_db;
  const dl = vdb.download || {};
  let note = vdb.entries
    ? `Vendor names from the ${esc(vdb.source)} (${vdb.entries.toLocaleString()} prefixes${
      vdb.updated_at ? `, updated ${new Date(vdb.updated_at * 1000).toLocaleDateString()}` : ""}).`
    : "No vendor list loaded.";
  if (dl.state === "downloading") note += " Downloading the latest list from the IEEE…";
  if (dl.state === "failed") {
    note += ` <span class="warn">Couldn't update it: ${esc(dl.error)}${dl.hint ? ` ${esc(dl.hint)}` : ""}</span>
      <button class="link" id="vendorRetry">Retry</button>`;
  }
  $("footnote").innerHTML = note;

  if (state.selected) {
    const d = devices.find((x) => x.id === state.selected);
    if (d) fillDrawer(d, false);
  }
}

const stat = (label, value, sub) =>
  `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;

function rowHtml(d) {
  const tags = [
    d.is_self ? `<span class="tag you">you</span>` : "",
    d.is_gateway ? `<span class="tag gw">router</span>` : "",
    isNew(d) ? `<span class="tag new">new</span>` : "",
  ].join("");
  const shown = displayName(d);
  const sub = [d.model !== shown && d.model, d.hostname !== shown && d.hostname]
    .filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(" · ");
  const mac = d.mac ? `${esc(d.mac)}${d.randomized_mac ? ` <span class="muted" title="Private / randomized MAC address">⟳</span>` : ""}` : "—";
  return `<tr data-id="${esc(d.id)}" class="${d.online ? "" : "offline"}">
    <td class="col-status"><span class="dot ${d.online ? "on" : ""}" title="${d.online ? "Online" : "Offline"}"></span></td>
    <td><span class="dev-name">${esc(displayName(d))}</span>${tags}${sub ? `<div class="dev-sub">${esc(sub)}</div>` : ""}</td>
    <td class="mono">${esc(d.ip)}</td>
    <td class="mono hide-sm">${mac}</td>
    <td class="hide-sm">${esc(d.vendor || "—")}</td>
    <td><span class="type">${TYPE_ICONS[d.type] || "❔"} ${esc(d.type || "Unknown")}</span></td>
    <td class="num hide-sm">${d.latency_ms != null ? `${d.latency_ms} ms` : "—"}</td>
    <td class="hide-md" title="${esc(fullDate(d.first_seen))}">${ago(d.first_seen)}</td>
    <td class="hide-md" title="${esc(fullDate(d.last_seen))}">${d.online ? "now" : ago(d.last_seen)}</td>
  </tr>`;
}

function sortRows(rows) {
  const { key, desc } = state.sort;
  const val = (d) => {
    switch (key) {
      case "ip": return ipNum(d.ip);
      case "display": return displayName(d).toLowerCase();
      case "online": return d.online ? 0 : 1;
      case "latency_ms": return d.latency_ms ?? Infinity;
      default: return (d[key] ?? "").toString().toLowerCase() || (typeof d[key] === "number" ? d[key] : "￿");
    }
  };
  return rows.slice().sort((a, b) => {
    const va = key === "first_seen" || key === "last_seen" ? a[key] || 0 : val(a);
    const vb = key === "first_seen" || key === "last_seen" ? b[key] || 0 : val(b);
    return (va < vb ? -1 : va > vb ? 1 : ipNum(a.ip) - ipNum(b.ip)) * (desc ? -1 : 1);
  });
}

// ---------- drawer ----------
function openDrawer(id) {
  const d = state.data.devices.find((x) => x.id === id);
  if (!d) return;
  state.selected = id;
  fillDrawer(d, true);
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  $("scrim").hidden = false;
}

function closeDrawer() {
  state.selected = null;
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  $("scrim").hidden = true;
}

function fillDrawer(d, resetInputs) {
  $("dTitle").textContent = displayName(d);
  const facts = [
    ["Status", d.online ? `<span class="dot on"></span> Online` : `<span class="dot"></span> Offline`],
    ["IP address", `<span class="mono">${esc(d.ip)}</span>`],
    ["MAC address", d.mac ? `<span class="mono">${esc(d.mac)}</span>` : "—"],
    ["Vendor", esc(d.vendor || (d.randomized_mac ? "Private address (randomized)" : "Unknown"))],
    ["Announced as", esc(d.friendly_name || "—")],
    ["Model", esc(d.model || "—")],
    ...(d.manufacturer && d.manufacturer !== d.vendor ? [["Manufacturer", esc(d.manufacturer)]] : []),
    ["Hostname", esc(d.hostname || "—")],
    ["Type", `${TYPE_ICONS[d.type] || ""} ${esc(d.type)}`],
    ["Latency", d.latency_ms != null ? `${d.latency_ms} ms` : "—"],
    ["Found via", esc((d.discovered_by || []).join(", ") || "—")],
    ...(d.services?.length ? [["Services", esc(d.services.join(", "))]] : []),
    ["First seen", esc(fullDate(d.first_seen))],
    ["Last seen", esc(d.online ? "Now" : fullDate(d.last_seen))],
  ];
  $("dFacts").innerHTML = facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  if (resetInputs) {
    $("dName").value = d.name || "";
    $("dNotes").value = d.notes || "";
  }

  const names = state.data.port_names;
  const known = portsOf(d);
  if (!known) {
    $("dPortsInfo").textContent = "Not scanned yet.";
    $("dPorts").innerHTML = "";
  } else {
    const what = known.quick ? `quick check of ${state.data.quick_ports.length} ports` : "full scan";
    const when = ` (${what}${known.at ? `, ${ago(known.at)}` : ""})`;
    $("dPortsInfo").textContent = known.ports.length ? `Open${when}:` : `None open${when}.`;
    $("dPorts").innerHTML = known.ports.map((p) => {
      const web = [80, 8080, 8000, 3000, 5000, 8123, 32400].includes(p) ? "http" : [443, 8443].includes(p) ? "https" : null;
      const label = `<b>${p}</b> ${esc(names[p] || "")}`;
      return `<li>${web ? `<a href="${web}://${esc(d.ip)}:${p}/" target="_blank" rel="noopener">${label} ↗</a>` : label}</li>`;
    }).join("");
  }
  $("dScanPorts").disabled = !d.online;
}

// ---------- events ----------
$("scanBtn").addEventListener("click", async () => {
  $("scanBtn").disabled = true;
  await api("/api/scan", { method: "POST" }).catch(() => {});
  refresh();
});
$("search").addEventListener("input", render);
$("footnote").addEventListener("click", async (e) => {
  if (e.target.id !== "vendorRetry") return;
  e.target.disabled = true;
  await api("/api/vendors/refresh", { method: "POST" }).catch(() => {});
  refresh();
});
$("typeFilter").addEventListener("change", render);
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  state.show = b.dataset.show;
  document.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
  render();
}));
document.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const key = th.dataset.sort;
  state.sort = { key, desc: state.sort.key === key ? !state.sort.desc : ["first_seen", "last_seen"].includes(key) };
  render();
}));
$("rows").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDrawer(tr.dataset.id);
});
$("dClose").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());

const devPath = (id) => `/api/devices/${encodeURIComponent(id)}`;

$("dSave").addEventListener("click", async () => {
  const id = state.selected;
  await api(devPath(id), { method: "PATCH", body: JSON.stringify({ name: $("dName").value, notes: $("dNotes").value }) });
  $("dSave").textContent = "Saved ✓";
  setTimeout(() => ($("dSave").textContent = "Save"), 1500);
  refresh();
});
$("dScanPorts").addEventListener("click", async () => {
  const btn = $("dScanPorts");
  btn.disabled = true;
  btn.textContent = "Scanning ports…";
  try { await api(`${devPath(state.selected)}/ports`, { method: "POST" }); }
  finally { btn.textContent = "Scan common ports"; }
  refresh();
});
$("dForget").addEventListener("click", async () => {
  const d = state.data.devices.find((x) => x.id === state.selected);
  if (!d || !confirm(`Forget ${displayName(d)}? It will reappear as new if seen again.`)) return;
  await api(devPath(d.id), { method: "DELETE" });
  closeDrawer();
  refresh();
});

refresh();
