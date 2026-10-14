/* Admin dashboard: sign in, review reports, move status, verify/reject */
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("rg_token") || "";
let whoami = sessionStorage.getItem("rg_user") || "";
const authHeaders = () => ({ Authorization: `Bearer ${token}`, "Content-Type": "application/json" });
const STATUSES = ["Reported", "In Progress", "Resolved"];

async function login(username, password) {
  try {
    const r = await api("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    token = r.access_token; whoami = r.username;
    sessionStorage.setItem("rg_token", token); sessionStorage.setItem("rg_user", whoami);
    enterDashboard();
  } catch (err) {
    sessionStorage.removeItem("rg_token");
    const m = $("login-msg"); m.textContent = err.message; m.className = "message error";
  }
}

function enterDashboard() {
  $("login").hidden = true; $("dashboard").hidden = false;
  $("whoami").textContent = `Signed in as ${whoami}`;
  refresh();
}

$("logout-btn").addEventListener("click", () => {
  sessionStorage.removeItem("rg_token"); sessionStorage.removeItem("rg_user");
  token = ""; $("login").hidden = false; $("dashboard").hidden = true;
});

async function refresh() {
  const [stats, reports] = await Promise.all([
    api("/api/hazards/stats/summary"),
    api("/api/hazards?" + filterParams()),
  ]);
  renderStats(stats); renderRows(reports);
  loadRiskEvents();
}

/* Collision-risk events from Night Rider (admin-only endpoint). A failure here must not break the dashboard. */
async function loadRiskEvents() {
  try {
    const [rows, stats] = await Promise.all([
      api("/api/admin/risk-events?limit=25", { headers: authHeaders() }),
      api("/api/admin/risk-events/stats", { headers: authHeaders() }),
    ]);
    $("risk-summary").textContent = `${stats.total} logged · ${stats.by_level.CRITICAL || 0} critical · ${stats.by_level.HIGH || 0} high`;
    $("risk-rows").innerHTML = rows.length ? rows.map((e) => {
      const where = e.latitude != null && e.longitude != null
        ? `<a href="https://www.openstreetmap.org/?mlat=${e.latitude}&mlon=${e.longitude}#map=18/${e.latitude}/${e.longitude}" target="_blank" rel="noopener">${e.latitude.toFixed(4)}, ${e.longitude.toFixed(4)}</a>`
        : "-";
      return `<tr><td>${esc(new Date(e.created_at).toLocaleString())}</td><td>${esc(e.event.replace(/_/g, " ").toLowerCase())}</td>
        <td><span class="level-chip ${esc(e.risk_level)}">${esc(e.risk_level)}</span></td><td>${Math.round(e.risk_score)}</td>
        <td>${esc(e.action.replace(/_/g, " ").toLowerCase())}</td><td>${e.distance_m != null ? Math.round(e.distance_m) + " m" : "-"}</td><td>${where}</td></tr>`;
    }).join("") : `<tr><td colspan="7" class="hint">No risk events logged yet.</td></tr>`;
  } catch (_) {
    $("risk-rows").innerHTML = `<tr><td colspan="7" class="hint">Could not load risk events.</td></tr>`;
  }
}

function filterParams() {
  const p = new URLSearchParams();
  if ($("f-status").value) p.set("status", $("f-status").value);
  if ($("f-severity").value) p.set("severity", $("f-severity").value);
  if ($("f-verify").value) p.set("verification_status", $("f-verify").value);
  return p;
}

function renderStats(s) {
  const cards = [
    ["Total reports", s.total],
    ["Pending review", s.by_verification["Pending"] || 0],
    ["Verified", s.by_verification["Verified"] || 0],
    ["AI detections", s.by_source["ai_detection"] || 0],
    ["Community reports", s.by_source["user_report"] || 0],
    ["High severity", s.by_severity["High"] || 0],
  ];
  $("stats").innerHTML = cards.map(([label, n]) => `<div class="stat"><b>${n}</b><span>${label}</span></div>`).join("");
}

function renderRows(reports) {
  const tbody = $("rows");
  if (!reports.length) { tbody.innerHTML = `<tr><td colspan="9" class="empty">No reports match these filters.</td></tr>`; return; }
  tbody.innerHTML = reports.map((r) => {
    const thumb = r.annotated_url || r.image_url;
    const loc = r.latitude != null
      ? `<a target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat=${r.latitude}&mlon=${r.longitude}#map=18/${r.latitude}/${r.longitude}">${r.latitude.toFixed(4)}, ${r.longitude.toFixed(4)}</a>`
      : `<span class="hint">No GPS</span>`;
    return `<tr>
      <td>${thumb ? `<img src="${esc(thumb)}" alt="">` : ""}</td>
      <td class="type">${esc(hazardLabel(r.hazard_type))}${r.confidence != null ? `<br><small>${Math.round(r.confidence * 100)}% confidence</small>` : ""}</td>
      <td>${r.severity ? `<span class="badge ${esc(r.severity)}">${esc(r.severity)}</span>` : `<span class="badge unrated">Unrated</span>`}</td>
      <td><span class="source-tag ${esc(r.source)}">${r.source === "ai_detection" ? "AI" : "Community"}</span></td>
      <td>${loc}</td>
      <td>${fmtDate(r.created_at)}</td>
      <td><select data-id="${r.id}" class="status-select" aria-label="Status of report ${r.id}">
        ${STATUSES.map((s) => `<option ${s === r.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
      <td>
        ${r.verification_status === "Pending"
          ? `<button class="secondary" data-verify="${r.id}">Verify</button> <button class="secondary" data-reject="${r.id}">Reject</button>`
          : `<span class="verify-tag ${esc(r.verification_status)}">${esc(r.verification_status)}</span>`}
      </td>
      <td><button class="secondary" data-del="${r.id}">Delete</button></td>
    </tr>`;
  }).join("");
}

$("rows").addEventListener("change", async (e) => {
  if (!e.target.classList.contains("status-select")) return;
  await api(`/api/admin/hazards/${e.target.dataset.id}/status`, {
    method: "PATCH", headers: authHeaders(), body: JSON.stringify({ status: e.target.value }),
  });
  refresh();
});

$("rows").addEventListener("click", async (e) => {
  const verifyId = e.target.dataset.verify;
  const rejectId = e.target.dataset.reject;
  const delId = e.target.dataset.del;

  if (verifyId) {
    await api(`/api/admin/hazards/${verifyId}/verify`, { method: "PATCH", headers: authHeaders(), body: JSON.stringify({ verification_status: "Verified" }) });
    return refresh();
  }
  if (rejectId) {
    await api(`/api/admin/hazards/${rejectId}/verify`, { method: "PATCH", headers: authHeaders(), body: JSON.stringify({ verification_status: "Rejected" }) });
    return refresh();
  }
  if (delId) {
    if (!confirm(`Delete report #${delId}? This can't be undone.`)) return;
    await api(`/api/admin/hazards/${delId}`, { method: "DELETE", headers: authHeaders() });
    return refresh();
  }
});

$("login-btn").addEventListener("click", () => login($("username").value, $("password").value));
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") login($("username").value, $("password").value); });
$("f-status").addEventListener("change", refresh);
$("f-severity").addEventListener("change", refresh);
$("f-verify").addEventListener("change", refresh);

if (token) enterDashboard();
