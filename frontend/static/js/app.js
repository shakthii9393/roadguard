/* Public page: report forms (AI detect + manual) + hazard map */
const map = L.map("map").setView([20.59, 78.96], 5);   // India; replaced when GPS is used
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const markerLayer = L.layerGroup().addTo(map);
let pin = null;
let chosen = null;      // {lat, lng}

const $ = (id) => document.getElementById(id);

/* ----- tabs ----- */
function selectTab(which) {
  const isAi = which === "ai";
  $("tab-ai").classList.toggle("active", isAi);
  $("tab-manual").classList.toggle("active", !isAi);
  $("tab-ai").setAttribute("aria-selected", isAi);
  $("tab-manual").setAttribute("aria-selected", !isAi);
  $("panel-ai").classList.toggle("active", isAi);
  $("panel-manual").classList.toggle("active", !isAi);
}
$("tab-ai").addEventListener("click", () => selectTab("ai"));
$("tab-manual").addEventListener("click", () => selectTab("manual"));

/* ----- choose location (shared by both forms) ----- */
function setLocation(lat, lng, zoom) {
  chosen = { lat, lng };
  if (pin) pin.setLatLng([lat, lng]); else pin = L.marker([lat, lng]).addTo(map);
  $("coords").textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  if (zoom) map.setView([lat, lng], zoom);
}
map.on("click", (e) => setLocation(e.latlng.lat, e.latlng.lng));

$("gps-btn").addEventListener("click", () => {
  if (!navigator.geolocation) return showMessage("Your browser doesn't support GPS. Tap the map instead.", "error");
  navigator.geolocation.getCurrentPosition(
    (p) => setLocation(p.coords.latitude, p.coords.longitude, 17),
    () => showMessage("Couldn't get your location. Tap the map to place a pin instead.", "error"),
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

/* ----- image preview (AI form) ----- */
$("image").addEventListener("change", (e) => {
  const file = e.target.files[0];
  const img = $("preview");
  if (!file) { img.style.display = "none"; return; }
  img.src = URL.createObjectURL(file);
  img.style.display = "block";
});

function showMessage(text, kind) {
  const el = $("message");
  el.textContent = text;
  el.className = `message ${kind}`;
}

/* ----- submit: AI detection ----- */
$("ai-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = new FormData();
  body.append("image", $("image").files[0]);
  if (chosen) { body.append("latitude", chosen.lat); body.append("longitude", chosen.lng); }
  body.append("description", $("ai-description").value);

  const btn = $("ai-submit-btn");
  btn.disabled = true; btn.textContent = "Analysing photo...";
  try {
    const r = await api("/api/hazards/detect", { method: "POST", body });
    showMessage(`${hazardLabel(r.hazard_type)} detected (${Math.round(r.confidence * 100)}% confidence, ${r.severity} severity). Report #${r.id} submitted for verification.`, "ok");
    $("ai-form").reset(); $("preview").style.display = "none";
    loadReports();
  } catch (err) {
    showMessage(err.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Detect and submit report";
  }
});

/* ----- submit: manual report ----- */
$("manual-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = new FormData();
  body.append("hazard_type", $("hazard-type").value);
  if (chosen) { body.append("latitude", chosen.lat); body.append("longitude", chosen.lng); }
  body.append("description", $("manual-description").value);
  const file = $("manual-image").files[0];
  if (file) body.append("image", file);

  const btn = $("manual-submit-btn");
  btn.disabled = true; btn.textContent = "Submitting...";
  try {
    const r = await api("/api/hazards/report", { method: "POST", body });
    showMessage(`Thanks - your ${hazardLabel(r.hazard_type).toLowerCase()} report (#${r.id}) was submitted and is awaiting admin verification.`, "ok");
    $("manual-form").reset();
    loadReports();
  } catch (err) {
    showMessage(err.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Submit report";
  }
});

/* ----- list + map markers ----- */
function severityBadge(r) {
  return r.severity ? `<span class="badge ${esc(r.severity)}">${esc(r.severity)}</span>` : `<span class="badge unrated">Unrated</span>`;
}

function popupHtml(r) {
  const img = r.annotated_url || r.image_url;
  return `<div class="popup">
    ${img ? `<img src="${esc(img)}" alt="${esc(hazardLabel(r.hazard_type))}">` : ""}
    <h3>${esc(hazardLabel(r.hazard_type))}</h3>
    <p>${severityBadge(r)} <span class="source-tag ${esc(r.source)}">${r.source === "ai_detection" ? "AI detected" : "Community report"}</span></p>
    <p class="status">${esc(r.status)} - <span class="verify-tag ${esc(r.verification_status)}">${esc(r.verification_status)}</span> - ${fmtDate(r.created_at)}</p>
    ${r.description ? `<p>${esc(r.description)}</p>` : ""}
  </div>`;
}

async function loadReports() {
  const params = new URLSearchParams();
  if ($("f-type").value) params.set("hazard_type", $("f-type").value);
  if ($("f-verify").value) params.set("verification_status", $("f-verify").value);
  const reports = await api(`/api/hazards?${params}`);

  markerLayer.clearLayers();
  const list = $("report-list");
  list.innerHTML = reports.length ? "" : `<li class="empty">No reports yet. Be the first to add one.</li>`;

  reports.forEach((r) => {
    const color = r.severity ? SEVERITY_COLORS[r.severity] : "#8a94a0";
    const marker = L.circleMarker([r.latitude, r.longitude], {
      radius: 9, weight: 2, color: r.verification_status === "Verified" ? "#1f2328" : "#fff",
      fillColor: color, fillOpacity: r.verification_status === "Pending" ? 0.6 : 0.95,
    }).bindPopup(popupHtml(r)).addTo(markerLayer);

    const li = document.createElement("li");
    const thumb = r.annotated_url || r.image_url;
    li.innerHTML = `${thumb ? `<img src="${esc(thumb)}" alt="">` : `<div></div>`}
      <div><h3>${esc(hazardLabel(r.hazard_type))} ${severityBadge(r)}</h3>
      <p><span class="source-tag ${esc(r.source)}">${r.source === "ai_detection" ? "AI" : "Community"}</span>
      <span class="verify-tag ${esc(r.verification_status)}">${esc(r.verification_status)}</span></p>
      <p>${esc(r.status)} - ${fmtDate(r.created_at)}</p></div>`;
    if (r.latitude != null) {
      li.addEventListener("click", () => { map.setView([r.latitude, r.longitude], 17); marker.openPopup(); });
    }
    list.appendChild(li);
  });

  const withLocation = reports.filter((r) => r.latitude != null);
  if (withLocation.length && !chosen) map.fitBounds(markerLayer.getBounds().pad(0.2), { maxZoom: 15 });
}

$("f-type").addEventListener("change", loadReports);
$("f-verify").addEventListener("change", loadReports);
loadReports();
