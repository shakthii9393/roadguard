/* Navigation page: search start/destination, fetch route options, draw on map */
const $ = (id) => document.getElementById(id);

const map = L.map("nav-map").setView([20.59, 78.96], 5);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

let startPoint = null;   // {lat, lon}
let endPoint = null;
let startMarker = null;
let endMarker = null;
let routeLine = null;
let routes = [];
let selectedIndex = 0;
let routeMode = "balanced";   // "fastest" | "safest" | "balanced" - which criteria drives the recommendation
let hazardMarkers = [];
let tracker = null;          // map follower (heading arrow, grey 'already driven' line)
let rerouting = false;
const rerouteMonitor = new RoadGuardTracking.RerouteMonitor();
let hudAlertTimer = null;
let noFixTimer = null;       // fires if GPS never delivers a usable position
let gotFix = false;
let session = null;          // active voice-guidance session, if any
let alertTimer = null;

function showMessage(text, kind) {
  const el = $("message");
  el.textContent = text;
  el.className = `message ${kind}`;
}

/* ----- debounced place search shared by both inputs ----- */
function wireLocationSearch(inputId, suggestionsId, onPick) {
  const input = $(inputId);
  const list = $(suggestionsId);
  let timer = null;

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { list.classList.remove("show"); return; }
    timer = setTimeout(async () => {
      try {
        const results = await api(`/api/navigation/search?q=${encodeURIComponent(q)}`);
        list.innerHTML = results.map((r, i) => `<li tabindex="0" data-i="${i}">${esc(r.display_name)}</li>`).join("");
        list.dataset.results = JSON.stringify(results);
        list.classList.toggle("show", results.length > 0);
      } catch (err) {
        list.classList.remove("show");
      }
    }, 350);
  });

  list.addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    const results = JSON.parse(list.dataset.results || "[]");
    const picked = results[Number(li.dataset.i)];
    input.value = picked.display_name;
    list.classList.remove("show");
    onPick({ lat: picked.latitude, lon: picked.longitude });
  });

  document.addEventListener("click", (e) => {
    if (!input.contains(e.target) && !list.contains(e.target)) list.classList.remove("show");
  });
}

wireLocationSearch("start-input", "start-suggestions", (p) => {
  startPoint = p;
  if (startMarker) startMarker.setLatLng([p.lat, p.lon]); else startMarker = L.marker([p.lat, p.lon], { title: "Start" }).addTo(map);
});
wireLocationSearch("end-input", "end-suggestions", (p) => {
  endPoint = p;
  if (endMarker) endMarker.setLatLng([p.lat, p.lon]); else endMarker = L.marker([p.lat, p.lon], { title: "Destination" }).addTo(map);
});

$("start-gps-btn").addEventListener("click", () => {
  if (!navigator.geolocation) return showMessage("Your browser doesn't support GPS.", "error");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      startPoint = { lat: pos.coords.latitude, lon: pos.coords.longitude };
      $("start-input").value = "My current location";
      if (startMarker) startMarker.setLatLng([startPoint.lat, startPoint.lon]);
      else startMarker = L.marker([startPoint.lat, startPoint.lon], { title: "Start" }).addTo(map);
      map.setView([startPoint.lat, startPoint.lon], 13);
    },
    () => showMessage("Couldn't get your location. Search for a starting point instead.", "error"),
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

/* ----- find routes ----- */
$("route-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!startPoint || !endPoint) return showMessage("Pick a start and destination from the suggestions (or use GPS for start).", "error");

  const btn = $("find-btn");
  btn.disabled = true; btn.textContent = "Finding routes...";
  showMessage("", "");
  try {
    const r = await api("/api/navigation/route", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_lat: startPoint.lat, start_lon: startPoint.lon, end_lat: endPoint.lat, end_lon: endPoint.lon,
        local_hour: new Date().getHours(),
      }),
    });
    routes = r.routes;
    renderRouteList();
    selectRoute(recommendedIndex());
    $("routes-section").hidden = false;
  } catch (err) {
    showMessage(err.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Find routes";
  }
});

/* ----- Fastest / Safest / Balanced picker ----- */
const riskClass = (level) => `risk-${(level || "medium").toLowerCase()}`;

function balancedScore(route) {
  const durations = routes.map((r) => r.duration_min);
  const risks = routes.map((r) => r.risk?.score ?? 50);
  const minD = Math.min(...durations), maxD = Math.max(...durations);
  const minR = Math.min(...risks), maxR = Math.max(...risks);
  const normD = maxD > minD ? (route.duration_min - minD) / (maxD - minD) : 0;
  const normR = maxR > minR ? ((route.risk?.score ?? 50) - minR) / (maxR - minR) : 0;
  return 0.5 * normD + 0.5 * normR;
}

function compareByMode(a, b) {
  if (routeMode === "fastest") return a.duration_min - b.duration_min;
  if (routeMode === "safest") return (a.risk?.score ?? 999) - (b.risk?.score ?? 999);
  return balancedScore(a) - balancedScore(b);
}

function recommendedIndex() {
  if (!routes.length) return -1;
  return routes.reduce((bestI, r, i) => (compareByMode(r, routes[bestI]) < 0 ? i : bestI), 0);
}

document.querySelectorAll(".mode-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    routeMode = btn.dataset.mode;
    document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("selected", b === btn));
    if (routes.length) renderRouteList();
  });
});

/* ----- route list: comparison table + selectable cards ----- */
function renderRouteList() {
  const recI = recommendedIndex();

  $("route-compare-body").innerHTML = routes.map((r, i) => `
    <tr data-i="${i}" class="${i === selectedIndex ? "selected" : ""} ${i === recI ? "recommended" : ""}">
      <td>Route ${i + 1}${i === recI ? " ⭐" : ""}</td>
      <td>${r.distance_km} km</td>
      <td>${Math.round(r.duration_min)} min</td>
      <td>${r.hazard_count}</td>
      <td>${r.risk ? `<span class="risk-badge ${riskClass(r.risk.level)}">${esc(r.risk.level)}</span>` : "-"}</td>
    </tr>`).join("");
  $("route-compare-body").querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => selectRoute(Number(row.dataset.i)));
  });

  const container = $("route-list");
  container.innerHTML = routes.map((r, i) => `
    <div class="route-card ${i === recI ? "recommended" : ""}" data-i="${i}" tabindex="0">
      <div class="route-labels">
        ${r.labels.map((l) => `<span class="route-label ${l === "Fewer hazards" || l === "Safest" ? "hazard-good" : ""}">${esc(l)}</span>`).join("")}
        ${r.risk ? `<span class="risk-badge ${riskClass(r.risk.level)}">${esc(r.risk.level)} risk</span>` : ""}
      </div>
      <h3>Route ${i + 1} ${i === recI ? `<span class="recommended-tag">Recommended</span>` : ""}</h3>
      <div class="route-metrics">
        <span><b>${r.distance_km} km</b></span>
        <span><b>${Math.round(r.duration_min)} min</b></span>
        <span class="${r.hazard_count > 0 ? "hazard-flag" : ""}">${r.hazard_count} reported hazard${r.hazard_count === 1 ? "" : "s"}</span>
      </div>
    </div>`).join("");

  container.querySelectorAll(".route-card").forEach((card) => {
    card.addEventListener("click", () => selectRoute(Number(card.dataset.i)));
  });
}

/* ----- Route Safety Score panel for the selected route ----- */
function renderRouteSafety(route) {
  const risk = route.risk;
  if (!risk) { $("route-safety-section").hidden = true; return; }

  $("route-safety-section").hidden = false;
  const gauge = $("safety-gauge");
  gauge.className = `safety-gauge ${riskClass(risk.level)}`;
  $("safety-score-num").textContent = Math.round(risk.score);
  gauge.style.setProperty("--pct", Math.round(risk.score));
  $("safety-level").textContent = `${risk.level} risk`;
  $("safety-notes").textContent = risk.notes && risk.notes.length ? risk.notes.join(" ") : "";

  const factors = [
    ["Hazard density", risk.hazard_density_score],
    ["Hazard severity", risk.severity_score],
    ["Historical incidents", `${risk.historical_incidents} nearby`],
    ["Road condition", risk.road_condition_score],
    ["Night visibility", risk.is_night ? "Factored in" : "Daytime"],
    ["Traffic (estimated)", risk.traffic_score],
    ["Weather", risk.weather_condition],
  ];
  $("safety-factors").innerHTML = factors.map(([label, value]) => `
    <li><b>${esc(label)}</b>${esc(String(value))}${typeof value === "number" ? "/100" : ""}</li>`).join("");
}

function selectRoute(i, { keepGuidance = false } = {}) {
  if (!keepGuidance) stopGuidance();
  selectedIndex = i;
  document.querySelectorAll(".route-card").forEach((c) => c.classList.toggle("selected", Number(c.dataset.i) === i));
  document.querySelectorAll("#route-compare-body tr").forEach((r) => r.classList.toggle("selected", Number(r.dataset.i) === i));

  const route = routes[i];
  renderRouteSafety(route);
  if (routeLine) map.removeLayer(routeLine);
  routeLine = L.polyline(route.geometry, { color: "#f5b700", weight: 6, opacity: 0.9 }).addTo(map);
  if (!keepGuidance) map.fitBounds(routeLine.getBounds().pad(0.1));

  $("steps-section").hidden = false;
  $("steps-list").innerHTML = route.steps.map((s) => `<li>${esc(s.instruction)} <small>(${Math.round(s.distance_m)} m)</small></li>`).join("")
    || "<li>No detailed steps returned for this route.</li>";

  renderRouteHazards(route);
  $("guidance-section").hidden = false;
  $("track-btn").hidden = false;
  $("track-hint").hidden = false;
  if (!keepGuidance) resetGuidanceCard();
}

/* ----- verified hazards on the selected route ----- */
function renderRouteHazards(route) {
  hazardMarkers.forEach((m) => map.removeLayer(m));
  hazardMarkers = [];

  const hazards = route.hazards || [];
  hazards.forEach((h) => {
    const marker = L.circleMarker([h.latitude, h.longitude], {
      radius: 8, weight: 3, color: SEVERITY_COLORS[h.severity] || "#5d6670", fillColor: "#fff", fillOpacity: 0.9,
    }).addTo(map).bindTooltip(hazardLabel(h.hazard_type));
    hazardMarkers.push(marker);
  });

  $("route-hazards-section").hidden = false;
  $("route-hazard-list").innerHTML = hazards.length
    ? hazards.map((h) => `<li><b>${esc(hazardLabel(h.hazard_type))}</b>
        <span>${esc(RoadGuardGuidance.formatDistance(h.offset_m))} from start${h.severity ? ` · ${esc(h.severity)}` : ""}</span></li>`).join("")
    : `<li class="empty">No verified hazards reported on this route.</li>`;
}

/* ----- voice guidance ----- */
function resetGuidanceCard() {
  $("g-next").textContent = "Ready when you are";
  $("g-sub").textContent = "Directions and hazard warnings are spoken, so you can keep your eyes on the road.";
  $("g-alert").hidden = true;
  $("g-start").hidden = false;
  $("g-stop").hidden = true;
  $("g-demo").hidden = false;
  $("g-note").textContent = RoadGuardVoice.isSupported() ? "" : "This browser can't speak aloud. Directions will still show on screen.";
}

function startGuidance(simulate) {
  const route = routes[selectedIndex];
  if (!route) return;
  stopGuidance();
  RoadGuardVoice.unlock();   // browsers only allow speech that starts from a tap

  session = new RoadGuardGuidance.GuidanceSession(route, {
    voice: RoadGuardVoice,
    simulate,
    onState: renderGuidanceState,
    onEvent: renderGuidanceEvent,
    onStatus: onGpsStatus,
  });
  if (!session.start()) { session = null; return; }
  enterNavMode(route, simulate);

  $("g-start").hidden = true;
  $("g-demo").hidden = true;
  $("g-stop").hidden = false;
  $("g-next").textContent = simulate ? "Demo drive started" : "Finding your position…";
  $("g-sub").textContent = simulate ? "Simulated GPS - follow the yellow dot on the map." : "Waiting for a GPS fix.";
}

function stopGuidance() {
  if (session) session.stop();
  session = null;
  RoadGuardVoice.cancelAll();
  exitNavMode();
  if ($("g-start")) resetGuidanceCard();
}

function renderGuidanceState(state) {
  if (state.position) { gotFix = true; setHudStatus(""); }
  if (tracker) tracker.update(state);

  const left = RoadGuardGuidance.formatDistance(state.remaining_m);
  if (state.arrived) {
    $("g-next").textContent = "You have arrived";
    $("g-sub").textContent = "";
  } else if (state.offRoute) {
    $("g-next").textContent = "Off route";
    $("g-sub").textContent = "Head back to the highlighted route, or plan a new one.";
  } else if (state.next) {
    const n = state.next;
    $("g-next").textContent = `${n.text}${n.road ? ` onto ${n.road}` : ""}`;
    $("g-sub").textContent = `in ${RoadGuardGuidance.formatDistance(n.distance_m)} · ${left} to go`;
  } else {
    $("g-next").textContent = "Continue on this road";
    $("g-sub").textContent = `${left} to go`;
  }

  updateHud(state);
  maybeReroute(state);
}

function renderGuidanceEvent(e) {
  if (e.kind === "arrive") { stopGuidanceKeepCard(); return; }
  if (e.kind !== "hazard" && e.kind !== "offroute") return;
  const el = $("g-alert");
  el.textContent = e.text;
  el.hidden = false;
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => { el.hidden = true; }, 7000);
  showHudAlert(e.text, 7000);
}

function stopGuidanceKeepCard() {
  // Arrived: stop listening to GPS but leave the "You have arrived" card on screen.
  if (session) session.stop();
  session = null;
  $("g-stop").hidden = true;
  $("g-start").hidden = false;
  showArrivedHud();
}

/* ----- live tracking: full-screen navigation mode (Google-Maps style) ----- */
function enterNavMode(route, simulate) {
  document.body.classList.add("nav-active");
  $("nav-hud").hidden = false;
  $("hud-alert").hidden = true;
  $("hud-recenter").hidden = true;
  $("hud-arrow").textContent = "↑";
  $("hud-dist").textContent = "--";
  $("hud-road").textContent = simulate ? "Demo drive (simulated GPS)" : "Finding your position…";
  ["hud-eta", "hud-time", "hud-left"].forEach((id) => { $(id).textContent = "--"; });
  syncMuteButton();

  rerouteMonitor.reset();
  setTimeout(() => map.invalidateSize(), 50);   // the map just got bigger
  gotFix = false;
  setHudStatus("");
  clearTimeout(noFixTimer);
  if (!simulate) {
    // A laptop usually can't produce a GPS fix accurate enough to navigate with - say so instead of sitting on "Finding your position".
    noFixTimer = setTimeout(() => {
      if (!gotFix) setHudStatus("Still no usable GPS fix. Laptops only know a rough location - try on a phone outdoors, or watch a demo drive.", true);
    }, 10000);
  }
  tracker = new RoadGuardTracking.Tracker(map, L, {
    mapEl: $("nav-map"), stage: $("map-stage"),
    onFollowChange: (following) => { $("hud-recenter").hidden = following || tracker.headingUp; },
    onModeChange: (up) => {
      $("hud-compass").textContent = up ? "🧭 Heading-up" : "🧭 North-up";
      $("hud-recenter").hidden = up || tracker.follow;
    },
  });
  tracker.begin(route);
  $("hud-compass").textContent = "🧭 Heading-up";
  RoadGuardTracking.ScreenAwake.acquire();
}

function exitNavMode() {
  clearTimeout(noFixTimer);
  if (tracker) { tracker.end(); tracker = null; }
  RoadGuardTracking.ScreenAwake.release();
  clearTimeout(hudAlertTimer);
  if ($("nav-hud")) $("nav-hud").hidden = true;
  if (document.body.classList.contains("nav-active")) {
    document.body.classList.remove("nav-active");
    setTimeout(() => map.invalidateSize(), 50);
  }
}

function onGpsStatus(msg) {
  $("g-note").textContent = msg;
  if (msg) setHudStatus(msg, true);
}

/* Persistent GPS problem banner (with a way out: the demo drive). Empty text hides it. */
function setHudStatus(text, offerDemo) {
  const el = $("hud-status");
  $("hud-status-text").textContent = text;
  $("hud-demo").hidden = !offerDemo;
  el.hidden = !text;
}

function showHudAlert(text, ms) {
  const el = $("hud-alert");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(hudAlertTimer);
  hudAlertTimer = setTimeout(() => { el.hidden = true; }, ms);
}

function updateHud(state) {
  if (!tracker) return;
  const T = RoadGuardTracking;
  const route = routes[selectedIndex];
  const secs = T.etaSeconds(state.remaining_m, T.routeSpeedMps(route), state.speed);
  $("hud-left").textContent = RoadGuardGuidance.formatDistance(state.remaining_m);
  $("hud-time").textContent = Math.max(1, Math.round(secs / 60));
  $("hud-eta").textContent = T.arrivalDate(new Date(), secs).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  if (state.arrived) {
    $("hud-arrow").textContent = "⚑";
    $("hud-dist").textContent = "You've arrived";
    $("hud-road").textContent = "Tap Exit to finish";
  } else if (state.offRoute) {
    $("hud-arrow").textContent = "⚠";
    $("hud-dist").textContent = "Off route";
    $("hud-road").textContent = rerouting ? "Finding a new route…" : "Head back to the route - re-routing shortly";
  } else if (state.next) {
    const n = state.next;
    $("hud-arrow").textContent = T.arrowFor(n.text, n.type);
    $("hud-dist").textContent = RoadGuardGuidance.formatDistance(n.distance_m);
    $("hud-road").textContent = `${n.text}${n.road ? ` onto ${n.road}` : ""}`;
  } else {
    $("hud-arrow").textContent = "↑";
    $("hud-dist").textContent = "Continue";
    $("hud-road").textContent = "Continue on this road";
  }
}

function showArrivedHud() {
  if (!tracker) return;
  $("hud-arrow").textContent = "⚑";
  $("hud-dist").textContent = "You've arrived";
  $("hud-road").textContent = "Tap Exit to finish";
  $("hud-time").textContent = "0";
  $("hud-left").textContent = "0 m";
  $("hud-recenter").hidden = true;
}

/* Off route for a while (and not a demo drive) -> ask the server for fresh routes from where the rider is now. */
async function maybeReroute(state) {
  if (!session || session.opts.simulate || rerouting || !endPoint || !state.position) return;
  if (!rerouteMonitor.observe(state.offRoute, Date.now())) return;

  rerouting = true;
  rerouteMonitor.markDone(Date.now());
  RoadGuardVoice.say("Rerouting.", { priority: 3, ttlMs: 5000, key: "reroute" });
  showHudAlert("Rerouting…", 6000);
  const pos = state.position;
  try {
    const r = await api("/api/navigation/route", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_lat: pos.lat, start_lon: pos.lon, end_lat: endPoint.lat, end_lon: endPoint.lon,
        local_hour: new Date().getHours(),
      }),
    });
    if (!session) return;   // rider exited while we were waiting
    routes = r.routes;
    startPoint = { lat: pos.lat, lon: pos.lon };
    renderRouteList();
    selectRoute(recommendedIndex(), { keepGuidance: true });   // uses the Fastest/Safest/Balanced choice

    session.stop();
    session = new RoadGuardGuidance.GuidanceSession(routes[selectedIndex], {
      voice: RoadGuardVoice, simulate: false, announceStart: false,
      onState: renderGuidanceState, onEvent: renderGuidanceEvent,
      onStatus: onGpsStatus,
    });
    session.start();
    tracker.setRoute(routes[selectedIndex]);
    RoadGuardVoice.say("New route found.", { priority: 3, ttlMs: 5000, key: "reroute-done" });
    showHudAlert("New route found", 4000);
  } catch (err) {
    showHudAlert("Couldn't find a new route. Head back to the highlighted route.", 7000);
  } finally {
    rerouting = false;
  }
}

function syncMuteButton() {
  const on = RoadGuardVoice.isEnabled();
  $("hud-mute").textContent = on ? "🔊" : "🔇";
  $("hud-mute").classList.toggle("muted", !on);
  $("g-voice").checked = on;
}

$("track-btn").addEventListener("click", () => startGuidance(false));
$("hud-demo").addEventListener("click", () => startGuidance(true));
$("hud-compass").addEventListener("click", () => { if (tracker) tracker.setHeadingUp(!tracker.headingUp); });
$("hud-exit").addEventListener("click", stopGuidance);
$("hud-recenter").addEventListener("click", () => { if (tracker) tracker.recenter(); });
$("hud-mute").addEventListener("click", () => { RoadGuardVoice.setEnabled(!RoadGuardVoice.isEnabled()); syncMuteButton(); });

$("g-start").addEventListener("click", () => startGuidance(false));
$("g-demo").addEventListener("click", () => startGuidance(true));
$("g-stop").addEventListener("click", stopGuidance);

$("g-voice").checked = RoadGuardVoice.isEnabled();
$("g-voice").addEventListener("change", (e) => RoadGuardVoice.setEnabled(e.target.checked));

/* Night Rider picks up the route from here, so camera alerts and turn directions run together */
$("g-night").addEventListener("click", (e) => {
  const ok = RoadGuardGuidance.saveActiveRoute(routes[selectedIndex], $("end-input").value);
  if (!ok) {
    e.preventDefault();
    $("g-note").textContent = "Couldn't hand this route to Night Rider (browser storage is blocked). You can still ride without turn directions.";
  }
});