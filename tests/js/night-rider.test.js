// Run with:  node --test tests/js/night-rider.test.js
// Loads the real voice.js / guidance.js / context-ui.js / night-rider.js into a fake DOM and feeds
// them server responses, to check that the HUD, overlay, voice and risk-event logging are wired up.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const JS = path.join(__dirname, "..", "..", "frontend", "static", "js");

/* ---------- a tiny fake DOM ---------- */
function makeCtx() {
  const calls = [];
  return new Proxy({ calls, measureText: () => ({ width: 40 }) }, {
    get: (t, k) => (k in t ? t[k] : (...a) => { calls.push([k, ...a]); }),
    set: () => true,
  });
}
function makeEl() {
  const classes = new Set();
  const el = {
    hidden: false, dataset: {}, textContent: "", innerHTML: "", disabled: false, checked: true, width: 640, height: 480,
    clientWidth: 640, clientHeight: 480, children: [],
    style: { props: {}, setProperty(k, v) { this.props[k] = v; } },
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c), toggle: (c, on) => (on === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c)) : on ? classes.add(c) : classes.delete(c)) },
    addEventListener() {}, getContext: () => (el._ctx ||= makeCtx()),
    prepend(child) { el.children.unshift(child); }, removeChild(c) { el.children = el.children.filter((x) => x !== c); },
    get lastChild() { return el.children[el.children.length - 1]; }, offsetWidth: 1,
    toBlob(cb) { cb({}); },
  };
  return el;
}
const els = {};
global.window = global;
global.addEventListener = () => {};
global.document = { getElementById: (id) => (els[id] ||= makeEl()), createElement: () => makeEl(), addEventListener() {}, visibilityState: "visible" };
global.navigator = {};
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.sessionStorage = global.localStorage;
const fetches = [];
global.fetch = (url, opts) => { fetches.push({ url, opts }); return Promise.resolve({ ok: true, json: async () => ({}) }); };
global.isSecureContext = true;

for (const f of ["voice.js", "guidance.js", "context-ui.js"]) vm.runInThisContext(fs.readFileSync(path.join(JS, f), "utf8"), { filename: f });
const said = [];
globalThis.RoadGuardVoice.say = (text, opts) => { said.push({ text, ...opts }); return true; };
vm.runInThisContext(fs.readFileSync(path.join(JS, "night-rider.js"), "utf8"), { filename: "night-rider.js" });
const run = (code) => vm.runInThisContext(code);

/* ---------- fixtures: what /api/detect/live returns ---------- */
const obj = (over = {}) => ({ track_id: 4, label: "pedestrian", confirmed: true, distance_m: 12, distance_method: "ground", lateral_m: -0.2,
  direction: "left_to_right", approach: "steady", in_path: true, zone: "in_path", ttc_s: null, risk_score: 62.5, risk_level: "HIGH",
  event: "PEDESTRIAN_CROSSING", bbox: [280, 200, 320, 300], ...over });
const response = (ctxOver = {}, detections) => ({
  detections: detections || [{ label: "pedestrian", confidence: 0.9, severity: "Medium", bbox: [280, 200, 320, 300], estimated_distance_m: 12, urgency: "Slow down" }],
  frame_width: 640, frame_height: 480, demo_mode: false, low_light: false,
  context: { risk_level: "HIGH", risk_score: 62.5, event: "PEDESTRIAN_CROSSING", action: "REDUCE_SPEED", lane_side: null,
    message: "Pedestrian crossing ahead", speech: "Warning. Pedestrian crossing ahead. Reduce speed.", speak: true,
    primary: obj(), objects: [obj()], scene: { relations: [], left_clear: true, right_clear: true }, assumptions: {}, ...ctxOver },
});
const reset = () => { said.length = 0; fetches.length = 0; run("lastAlertAt = {}; lastLogged = {}; sessionId = 'test-session'; lastLevel = 'LOW'"); };

test("HUD shows the example from the brief", () => {
  reset();
  run("handleDetections").call(null, response());
  assert.equal(els["nr-hud"].hidden, false);
  assert.equal(els["nr-hud"].dataset.level, "HIGH");
  assert.equal(els["hud-risk"].textContent, "⚠️ HIGH RISK");
  assert.equal(els["hud-event"].textContent, "👤 Pedestrian crossing");
  assert.equal(els["hud-distance"].textContent, "12 m");
  assert.match(els["hud-direction"].textContent, /^Left → Right/);
  assert.equal(els["hud-path"].textContent, "Yes");
  assert.equal(els["hud-action"].textContent, "REDUCE SPEED");
  assert.equal(els["hud-action-box"].hidden, false);
  assert.match(els["hud-voice"].textContent, /Pedestrian crossing ahead/);
});

test("the server's sentence is spoken with the right priority, once", () => {
  reset();
  run("handleDetections")(response());
  assert.equal(said.length, 1);
  assert.equal(said[0].text, "Warning. Pedestrian crossing ahead. Reduce speed.");
  assert.equal(said[0].priority, 4);
  assert.equal(said[0].key, "context");
  said.length = 0;
  run("handleDetections")(response({ speak: false }));      // the server's speech gate said "don't repeat"
  assert.equal(said.length, 0);
});

test("CRITICAL is spoken at the highest priority and pulses the HUD", () => {
  reset();
  run("handleDetections")(response({ risk_level: "CRITICAL", event: "POSSIBLE_COLLISION", action: "BRAKE_IMMEDIATELY",
    speech: "Danger. Possible collision ahead. Brake immediately.", primary: obj({ risk_level: "CRITICAL" }) }));
  assert.equal(said[0].priority, 5);
  assert.equal(els["hud-action"].textContent, "BRAKE IMMEDIATELY");
  assert.equal(els["nr-hud"].classList.contains("pulse"), true);
});

test("boxes are drawn from tracked objects, plus the path guide", () => {
  reset();
  const calls = els["nr-overlay"].getContext("2d").calls;       // the context night-rider.js captured at load
  calls.length = 0;
  run("handleDetections")(response());
  const names = calls.map((c) => c[0]);
  assert.ok(names.includes("strokeRect"), "object box");
  assert.ok(names.includes("fillText"), "object label");
  assert.ok(names.filter((n) => n === "lineTo").length >= 3, "path corridor polygon");
});

test("HIGH moments are logged to the server once per track+event", () => {
  reset();
  run("handleDetections")(response());
  run("handleDetections")(response());
  const posts = fetches.filter((f) => f.url === "/api/risk-events");
  assert.equal(posts.length, 1);
  const body = JSON.parse(posts[0].opts.body);
  assert.equal(body.event, "PEDESTRIAN_CROSSING");
  assert.equal(body.session_id, "test-session");
  assert.equal(body.risk_level, "HIGH");
});

test("LOW-risk frames are silent and not logged", () => {
  reset();
  run("handleDetections")(response({ risk_level: "LOW", risk_score: 8, event: "NONE", action: "NONE", speech: "", speak: false,
    primary: obj({ label: "pothole", risk_level: "LOW", event: "NONE", in_path: false, zone: "outside", direction: "none" }) }));
  assert.equal(said.length, 0);
  assert.equal(fetches.filter((f) => f.url === "/api/risk-events").length, 0);
  assert.equal(els["nr-hud"].dataset.level, "LOW");
});

test("an empty road shows the calm 'road clear' HUD", () => {
  reset();
  run("handleDetections")(response({ primary: null, objects: [], risk_level: "LOW", event: "NONE", speech: "", speak: false, action: "NONE" }, []));
  assert.equal(els["hud-risk"].textContent, "✅ ROAD CLEAR");
  assert.equal(els["hud-details"].hidden, true);
  assert.equal(els["hud-action-box"].hidden, true);
});

test("traffic signals are still announced calmly alongside the context layer", () => {
  reset();
  const res = response({ primary: null, objects: [], risk_level: "LOW", event: "NONE", speech: "", speak: false }, [
    { label: "traffic_signal", confidence: 0.8, severity: "Low", bbox: [10, 10, 30, 60], estimated_distance_m: 30, urgency: "Be ready to stop" }]);
  run("handleDetections")(res);
  assert.equal(said.length, 1);
  assert.match(said[0].text, /^Traffic signal ahead/);
});

test("vehicles are never announced by the legacy path", () => {
  reset();
  run("handleDetections")({ detections: [{ label: "vehicle", confidence: 0.9, severity: "Low", bbox: [10, 10, 90, 90], estimated_distance_m: 10, urgency: "" }],
    frame_width: 640, frame_height: 480, demo_mode: false, low_light: false, context: null });
  assert.equal(said.length, 0);
});

test("with no context (older backend) the original alerts and overlay still work", () => {
  reset();
  els["nr-hud"].hidden = false;
  run("handleDetections")({ detections: [{ label: "pothole", confidence: 0.9, severity: "High", bbox: [100, 300, 200, 400], estimated_distance_m: 20, urgency: "Reduce speed" }],
    frame_width: 640, frame_height: 480, demo_mode: false, low_light: false });
  assert.equal(els["nr-hud"].hidden, true);
  assert.equal(said.length, 1);
  assert.match(said[0].text, /Pothole detected, approximately 20 meters ahead/);
});
