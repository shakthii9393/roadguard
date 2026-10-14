/* Night Rider Mode: camera -> /api/detect/live -> HUD + overlay + context-aware voice alerts.
   Designed to be used hands-off - the important information is spoken, not read.

   Pipeline (server side, services/roadguard_context.py):
     detection -> tracking -> movement -> distance -> vehicle-path check -> collision risk
     -> road event -> recommended action -> voice sentence
   This file sends frames (plus session id, timestamp and GPS speed), then shows and speaks
   whatever `context` comes back. If a response has no `context`, the original per-object
   alerts still work, so an older backend never breaks the page. */
const $ = (id) => document.getElementById(id);

/* Names come from voice.js so the camera alerts and route warnings say the same words. */
const hazardName = (type) => RoadGuardVoice.label(type);
const HAZARD_EMOJI = {
  pothole: "🕳️", crack: "🪨", waterlogging: "🌊", debris: "🪨", construction_barrier: "🚧",
  parked_vehicle: "🚗", animal: "🐕", pedestrian: "🚶", speed_breaker: "⚠️",
  damaged_sign: "🛑", broken_streetlight: "💡", traffic_signal: "🚦", vehicle: "🚙",
};
/* Things worth knowing about but not "hazards": announced calmly, shown in blue, no "Caution". */
const INFO_ONLY = new Set(["traffic_signal", "damaged_sign", "broken_streetlight", "crack"]);
/* Drawn on the overlay but never announced on their own: a vehicle is only worth a word when the
   context layer decides it is a risk (approaching, in your path), not merely because it exists. */
const SILENT_LABELS = new Set(["vehicle"]);

// Voice priorities (see voice.js): a live camera hazard outranks turn directions
const PRIORITY_CAMERA_HAZARD = 4;

const CAPTURE_INTERVAL_MS = 700;      // how often a frame is sent: tracking needs frames close together
const ALERT_COOLDOWN_MS = 12000;      // don't re-announce the same informational label sooner than this
const CAPTURE_MAX_WIDTH = 640;        // frames are downscaled before sending - faster, smaller
const GPS_FRESH_MS = 5000;            // a GPS speed older than this is not trusted
const HORIZON_RATIO = 0.42;           // keep in step with HORIZON_Y_RATIO on the server (.env)

let stream = null;
let captureTimer = null;
let running = false;
let lastAlertAt = {};   // hazard_type -> timestamp (informational alerts)
let sawDemoModeNotice = false;
let guidance = null;    // route guidance session (turn directions + hazards on route), if a route was chosen
let lastGpsStatus = "";
let wakeLock = null;    // keeps the screen on so the camera loop and GPS don't get suspended mid-ride

let sessionId = null;   // one per run: lets the server keep this phone's object tracker separate
let geoWatchId = null;
let lastGps = null;     // { lat, lon }
let lastSpeed = null;   // m/s from GPS, or null
let lastSpeedAt = 0;
let lastLogged = {};    // "trackId:event" -> time a HIGH/CRITICAL moment was sent to the server
let lastLevel = "LOW";

const video = $("nr-video");
const overlay = $("nr-overlay");
const octx = overlay.getContext("2d");
const captureCanvas = document.createElement("canvas");   // off-screen, for downscaled frames

function setStatus(text, demo) {
  const el = $("nr-status");
  el.textContent = text;
  el.classList.toggle("demo", !!demo);
}

function showError(msg) {
  const el = $("nr-error");
  el.textContent = msg;
  el.style.display = "block";
}

/* ----- camera lifecycle ----- */
async function start() {
  $("nr-start-btn").disabled = true;
  RoadGuardVoice.unlock();   // must happen inside the tap, before the camera permission prompt
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
  } catch (err) {
    $("nr-start-btn").disabled = false;
    const secure = window.isSecureContext;
    showError(secure
      ? "Couldn't access the camera. Check camera permissions for this site and try again."
      : "Camera access needs HTTPS (or localhost). Open this page over https:// on your phone.");
    return;
  }

  video.srcObject = stream;
  await video.play();

  $("nr-idle").hidden = true;
  $("nr-stop-btn").hidden = false;
  running = true;
  lastAlertAt = {};
  lastLogged = {};
  lastLevel = "LOW";
  sawDemoModeNotice = false;
  sessionId = RoadGuardContext.newSessionId();
  resizeOverlay();
  const hasRoute = !!RoadGuardGuidance.loadActiveRoute();
  RoadGuardVoice.say(hasRoute ? "Night Rider Mode started. Voice directions on." : "Night Rider Mode started.", { priority: 1, key: "start" });
  keepScreenOn();
  startGps();
  startGuidance();
  renderHud(null);
  captureLoop();
}

function stop() {
  running = false;
  clearTimeout(captureTimer);
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  video.srcObject = null;
  octx.clearRect(0, 0, overlay.width, overlay.height);
  $("nr-idle").hidden = false;
  $("nr-stop-btn").hidden = true;
  $("nr-start-btn").disabled = false;
  $("nr-hud").hidden = true;
  setStatus("");
  stopGuidance();
  stopGps();
  RoadGuardVoice.cancelAll();
  releaseScreen();
}

$("nr-start-btn").addEventListener("click", start);
$("nr-stop-btn").addEventListener("click", stop);
$("nr-voice-toggle").checked = RoadGuardVoice.isEnabled();
$("nr-voice-toggle").addEventListener("change", (e) => RoadGuardVoice.setEnabled(e.target.checked));
$("nr-log-toggle").addEventListener("change", (e) => { $("nr-log-panel").hidden = !e.target.checked; });
window.addEventListener("beforeunload", stop);
window.addEventListener("resize", resizeOverlay);

function resizeOverlay() {
  overlay.width = video.clientWidth;
  overlay.height = video.clientHeight;
}

/* ----- GPS: the rider's speed makes "how soon would I reach it?" honest; position tags logged events ----- */
function startGps() {
  if (!("geolocation" in navigator)) return;
  try {
    geoWatchId = navigator.geolocation.watchPosition(
      (pos) => {
        lastGps = { lat: pos.coords.latitude, lon: pos.coords.longitude };
        if (Number.isFinite(pos.coords.speed) && pos.coords.speed >= 0) { lastSpeed = pos.coords.speed; lastSpeedAt = Date.now(); }
      },
      () => { /* denied or unavailable: the engine assumes a typical city speed instead */ },
      { enableHighAccuracy: true, maximumAge: 2000, timeout: 10000 },
    );
  } catch (_) { geoWatchId = null; }
}

function stopGps() {
  if (geoWatchId !== null) navigator.geolocation.clearWatch(geoWatchId);
  geoWatchId = null; lastGps = null; lastSpeed = null; lastSpeedAt = 0;
}

const currentSpeed = () => (lastSpeed !== null && Date.now() - lastSpeedAt < GPS_FRESH_MS ? lastSpeed : null);

/* ----- capture + detect loop ----- */
function captureLoop() {
  if (!running) return;
  captureFrameAndDetect().finally(() => {
    captureTimer = setTimeout(captureLoop, CAPTURE_INTERVAL_MS);
  });
}

async function captureFrameAndDetect() {
  if (!video.videoWidth) return;

  const scale = Math.min(1, CAPTURE_MAX_WIDTH / video.videoWidth);
  captureCanvas.width = Math.round(video.videoWidth * scale);
  captureCanvas.height = Math.round(video.videoHeight * scale);
  captureCanvas.getContext("2d").drawImage(video, 0, 0, captureCanvas.width, captureCanvas.height);
  const capturedAt = performance.now();

  const blob = await new Promise((resolve) => captureCanvas.toBlob(resolve, "image/jpeg", 0.7));
  if (!blob) return;

  const body = new FormData();
  body.append("image", blob, "frame.jpg");
  body.append("session_id", sessionId);
  body.append("t_ms", String(Math.round(capturedAt)));
  const speed = currentSpeed();
  if (speed !== null) body.append("speed_mps", String(speed));

  try {
    const res = await fetch("/api/detect/live", { method: "POST", body });
    if (!res.ok) { setStatus("Detection unavailable"); return; }
    const data = await res.json();
    handleDetections(data);
  } catch (err) {
    setStatus("Connection lost - retrying");
  }
}

/* ----- detections -> HUD + overlay + alerts ----- */
function handleDetections(data) {
  const base = data.demo_mode ? "DEMO MODE - showing sample detections" : "Live detection active";
  const speed = currentSpeed();
  const parts = [base];
  if (data.low_light) parts.push("low-light boost on");
  parts.push(speed !== null ? `${Math.round(speed * 3.6)} km/h` : "speed unknown");
  setStatus(parts.join(" · "), data.demo_mode);
  if (data.demo_mode && !sawDemoModeNotice) {
    sawDemoModeNotice = true;
    logEntry("Running in demo mode - train a model for real detection (see README).", null);
  }

  const ctx = data.context || null;
  if (ctx) {
    renderHud(ctx);
    drawContextOverlay(ctx, data.frame_width, data.frame_height);
    speakContext(ctx);
    reportRiskEvent(ctx);
    announceInfoOnly(data.detections);
  } else {
    // Older backend / analysis failed: original behaviour - box every detection, alert on the closest
    hideHud();
    drawOverlay(data.detections, data.frame_width, data.frame_height);
    const alertable = data.detections.filter((d) => !SILENT_LABELS.has(d.label) && canAlert(d.label));
    if (alertable.length === 0) return;
    const closest = alertable.reduce((a, b) => (a.estimated_distance_m <= b.estimated_distance_m ? a : b));
    triggerAlert(closest);
  }
}

/* Context-aware voice: the server decides WHEN (new event, escalation, cooldown); we only say it. */
function speakContext(ctx) {
  const plan = RoadGuardContext.voicePlan(ctx);
  if (!plan) return;
  RoadGuardVoice.say(plan.text, { priority: plan.priority, ttlMs: plan.ttlMs, key: plan.key });
  logEntry(plan.text, ctx.risk_level);
}

/* Traffic signals, worn signs etc. are awareness only - still announced calmly, as before. */
function announceInfoOnly(detections) {
  const info = detections.filter((d) => INFO_ONLY.has(d.label) && canAlert(d.label));
  if (info.length === 0) return;
  triggerAlert(info.reduce((a, b) => (a.estimated_distance_m <= b.estimated_distance_m ? a : b)));
}

/* HIGH / CRITICAL moments are sent to the server (fire and forget) so admins can see near-miss spots. */
function reportRiskEvent(ctx) {
  const payload = RoadGuardContext.riskEventPayload(ctx, {
    sessionId, gps: lastGps, speedMps: currentSpeed(), lastLogged, nowMs: Date.now(),
  });
  if (!payload) return;
  fetch("/api/risk-events", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  }).catch(() => { /* offline: not worth interrupting the ride */ });
}

/* ----- the HUD ----- */
function hideHud() { $("nr-hud").hidden = true; }

function renderHud(ctx) {
  const hud = $("nr-hud");
  const m = RoadGuardContext.hudModel(ctx);
  hud.hidden = false;
  hud.dataset.level = m.level;
  hud.classList.toggle("clear", m.clear);
  hud.style.setProperty("--risk-color", m.style.color);
  // Restart the pulse whenever danger steps up to CRITICAL
  if (m.level === "CRITICAL" && lastLevel !== "CRITICAL") { hud.classList.remove("pulse"); void hud.offsetWidth; hud.classList.add("pulse"); }
  if (m.level !== "CRITICAL") hud.classList.remove("pulse");
  lastLevel = m.level;

  $("hud-risk").textContent = `${m.style.emoji} ${m.riskText}`;
  $("hud-score").textContent = m.clear ? "" : `${m.score}/100`;
  $("hud-event").textContent = `${m.eventEmoji} ${m.title}`;
  $("hud-details").hidden = m.clear;
  $("hud-distance").textContent = m.distance;
  $("hud-direction").textContent = m.direction + (m.approach ? ` · ${m.approach}` : "");
  $("hud-path").textContent = m.path;
  $("hud-action-box").hidden = !m.action;
  $("hud-action").textContent = m.action;
  $("hud-voice").textContent = m.speech ? `“${m.speech}”` : "";
  $("hud-objects").innerHTML = m.objects.map((o) => `<li data-level="${esc(o.level)}">${esc(o.text)}</li>`).join("");
}

/* ----- informational / legacy alerts (unchanged behaviour) ----- */
function canAlert(label) {
  const last = lastAlertAt[label] || 0;
  return Date.now() - last > ALERT_COOLDOWN_MS;
}

/* Round to the nearest 5 m: the estimate isn't precise enough to justify "37 meters". */
const roundDistance = (m) => Math.max(5, Math.round(m / 5) * 5);

function alertPhrase(det, name, dist) {
  const informational = INFO_ONLY.has(det.label);
  const advice = det.urgency ? ` ${det.urgency}.` : "";
  if (informational) return `${name} ahead, approximately ${dist} meters.${advice}`;
  // "Warning" only when it's right on top of the rider; "Caution" otherwise
  const immediate = dist <= 15 || (det.severity === "High" && dist <= 25);
  return `${immediate ? "Warning" : "Caution"}. ${name} detected, approximately ${dist} meters ahead.${advice}`;
}

function triggerAlert(det) {
  lastAlertAt[det.label] = Date.now();
  const name = hazardName(det.label);
  const emoji = HAZARD_EMOJI[det.label] || "⚠️";
  const dist = roundDistance(det.estimated_distance_m);
  const informational = INFO_ONLY.has(det.label);

  showBanner(emoji, name, dist, det.urgency, informational);
  logEntry(`${name} - approximately ${dist} m ahead${det.urgency ? ` - ${det.urgency}` : ""}`, det.severity);

  RoadGuardVoice.say(alertPhrase(det, name, dist), {
    priority: PRIORITY_CAMERA_HAZARD, ttlMs: 4000, key: "camera-hazard",
  });
}

function showBanner(emoji, name, distanceM, urgency, informational) {
  const banner = $("nr-banner");
  banner.innerHTML = `${emoji} ${esc(name)} ${informational ? "ahead" : "detected"}
    <span class="distance">${distanceM} meters ahead <span class="estimate-tag">(estimate)</span></span>
    ${urgency ? `<span class="advice">${esc(urgency)}</span>` : ""}`;
  banner.classList.toggle("info", !!informational);
  banner.classList.add("show");
  clearTimeout(banner._hideTimer);
  banner._hideTimer = setTimeout(() => banner.classList.remove("show"), 4500);
}

function logEntry(text, severity) {
  const list = $("nr-log-list");
  const li = document.createElement("li");
  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  li.innerHTML = `<b>${time}</b> - ${esc(text)}`;
  list.prepend(li);
  while (list.children.length > 25) list.removeChild(list.lastChild);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ----- overlay boxes ----- */
/* Original overlay, used when the server sends no context: one box per detection, coloured by severity. */
function drawOverlay(detections, frameW, frameH) {
  resizeOverlay();
  octx.clearRect(0, 0, overlay.width, overlay.height);
  if (!frameW || !frameH) return;

  const sx = overlay.width / frameW;
  const sy = overlay.height / frameH;

  detections.forEach((d) => {
    const [x1, y1, x2, y2] = d.bbox;
    const rx = x1 * sx, ry = y1 * sy, rw = (x2 - x1) * sx, rh = (y2 - y1) * sy;
    const color = INFO_ONLY.has(d.label) ? "#2b7bb9"
      : d.severity === "High" ? "#c8402f" : d.severity === "Medium" ? "#d98e04" : "#2f8f5b";

    octx.strokeStyle = color;
    octx.lineWidth = 3;
    octx.strokeRect(rx, ry, rw, rh);

    const label = `${hazardName(d.label)} - ~${roundDistance(d.estimated_distance_m)}m`;
    octx.font = "600 14px Barlow, sans-serif";
    const textWidth = octx.measureText(label).width;
    octx.fillStyle = color;
    octx.fillRect(rx, Math.max(ry - 22, 0), textWidth + 10, 22);
    octx.fillStyle = "#fff";
    octx.fillText(label, rx + 5, Math.max(ry - 6, 15));
  });
}

/* Context overlay: the predicted vehicle path, plus every tracked object coloured by ITS risk level. */
function drawContextOverlay(ctx, frameW, frameH) {
  resizeOverlay();
  const W = overlay.width, H = overlay.height;
  octx.clearRect(0, 0, W, H);
  if (!frameW || !frameH) return;

  // Approximate straight-ahead path corridor (the server's exact corridor is in metres, this is a guide)
  const horizon = H * HORIZON_RATIO;
  octx.beginPath();
  octx.moveTo(W * 0.30, H); octx.lineTo(W * 0.70, H); octx.lineTo(W * 0.515, horizon); octx.lineTo(W * 0.485, horizon);
  octx.closePath();
  octx.fillStyle = "rgba(255,198,26,.10)"; octx.fill();
  octx.setLineDash([10, 8]); octx.strokeStyle = "rgba(255,198,26,.55)"; octx.lineWidth = 2; octx.stroke();
  octx.setLineDash([]);

  const sx = W / frameW, sy = H / frameH;
  const primaryId = ctx.primary ? ctx.primary.track_id : null;
  (ctx.objects || []).forEach((o) => {
    const [x1, y1, x2, y2] = o.bbox;
    const rx = x1 * sx, ry = y1 * sy, rw = (x2 - x1) * sx, rh = (y2 - y1) * sy;
    const color = INFO_ONLY.has(o.label) ? "#2b7bb9" : RoadGuardContext.levelColor(o.risk_level);

    octx.strokeStyle = color;
    octx.lineWidth = o.track_id === primaryId ? 4 : 2.5;
    octx.strokeRect(rx, ry, rw, rh);

    const arrow = { left_to_right: " →", right_to_left: " ←" }[o.direction] || "";
    const label = `${hazardName(o.label)} #${o.track_id} · ${RoadGuardContext.formatDistance(o.distance_m)}${arrow}`;
    octx.font = "600 14px Barlow, sans-serif";
    const textWidth = octx.measureText(label).width;
    octx.fillStyle = color;
    octx.fillRect(rx, Math.max(ry - 22, 0), textWidth + 10, 22);
    octx.fillStyle = "#fff";
    octx.fillText(label, rx + 5, Math.max(ry - 6, 15));
  });
}


/* ----- route guidance alongside the camera (turn directions + hazards on the chosen route) ----- */
function renderRouteStrip() {
  const saved = RoadGuardGuidance.loadActiveRoute();
  const strip = $("nr-route-strip");
  if (saved) {
    const dest = saved.destination ? `to ${esc(saved.destination.split(",")[0])}` : "on your chosen route";
    strip.innerHTML = `Voice directions ${dest} · ${saved.route.distance_km} km <a href="/navigate.html">Change route</a>`;
  } else {
    strip.innerHTML = `No route chosen - camera alerts only. <a href="/navigate.html">Plan a route</a> to add turn directions.`;
  }
}

function startGuidance() {
  const saved = RoadGuardGuidance.loadActiveRoute();
  if (!saved) return;
  guidance = new RoadGuardGuidance.GuidanceSession(saved.route, {
    voice: RoadGuardVoice,
    announceStart: false,
    onState: renderNextCard,
    onEvent: renderGuidanceEvent,
    onStatus: (msg) => { if (msg && msg !== lastGpsStatus) logEntry(msg, null); lastGpsStatus = msg; },
  });
  if (!guidance.start()) guidance = null;
}

function stopGuidance() {
  if (guidance) guidance.stop();
  guidance = null;
  $("nr-next-card").hidden = true;
}

function renderNextCard(state) {
  // The route's own GPS speed is a good fallback if our watcher hasn't got a fix yet
  if (Number.isFinite(state.speed) && state.speed >= 0 && Date.now() - lastSpeedAt > GPS_FRESH_MS) { lastSpeed = state.speed; lastSpeedAt = Date.now(); }
  const card = $("nr-next-card");
  card.hidden = false;
  if (state.arrived) {
    $("nr-next-main").textContent = "You have arrived";
    $("nr-next-sub").textContent = "";
  } else if (state.offRoute) {
    $("nr-next-main").textContent = "Off route";
    $("nr-next-sub").textContent = "Head back to your route";
  } else if (state.next) {
    $("nr-next-main").textContent = `${state.next.text}${state.next.road ? ` onto ${state.next.road}` : ""}`;
    $("nr-next-sub").textContent = `in ${RoadGuardGuidance.formatDistance(state.next.distance_m)} · ${RoadGuardGuidance.formatDistance(state.remaining_m)} to go`;
  } else {
    $("nr-next-main").textContent = "Continue straight";
    $("nr-next-sub").textContent = `${RoadGuardGuidance.formatDistance(state.remaining_m)} to go`;
  }
}

function renderGuidanceEvent(e) {
  if (e.kind === "hazard" || e.kind === "offroute") {
    logEntry(e.text, null);
    const el = $("nr-next-alert");
    el.textContent = e.text;
    el.hidden = false;
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => { el.hidden = true; }, 7000);
  }
}

/* ----- keep the screen awake (a phone that sleeps stops the camera loop and GPS) ----- */
async function keepScreenOn() {
  try {
    if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen");
  } catch (_) { /* denied or unsupported - the rider can raise the screen timeout instead */ }
}
function releaseScreen() {
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
}
// The lock is released automatically when the tab is hidden; take it again on return
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && running) keepScreenOn();
});

renderRouteStrip();
