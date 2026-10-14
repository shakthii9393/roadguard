// Run with:  node --test tests/js/context-ui.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../frontend/static/js/context-ui.js");

const ctx = (over = {}) => ({
  risk_level: "HIGH", risk_score: 72.4, event: "PEDESTRIAN_CROSSING", action: "REDUCE_SPEED", lane_side: null,
  message: "Pedestrian crossing ahead", speech: "Warning. Pedestrian crossing ahead. Reduce speed.", speak: true,
  primary: { track_id: 4, label: "pedestrian", distance_m: 12.2, direction: "left_to_right", approach: "steady", in_path: true, zone: "in_path", risk_level: "HIGH" },
  objects: [{ track_id: 4, label: "pedestrian", distance_m: 12.2, direction: "left_to_right", risk_level: "HIGH" }],
  ...over,
});

test("HUD matches the example in the brief", () => {
  const h = C.hudModel(ctx());
  assert.equal(h.riskText, "HIGH RISK");
  assert.equal(h.eventEmoji, "👤");
  assert.equal(h.title, "Pedestrian crossing");
  assert.equal(h.distance, "12 m");
  assert.equal(h.direction, "Left → Right");
  assert.equal(h.path, "Yes");
  assert.equal(h.action, "REDUCE SPEED");
  assert.equal(h.score, 72);
  assert.equal(h.speech, "Warning. Pedestrian crossing ahead. Reduce speed.");
});

test("no objects -> calm 'road clear' HUD", () => {
  for (const c of [null, undefined, { primary: null }]) {
    const h = C.hudModel(c);
    assert.equal(h.clear, true);
    assert.equal(h.riskText, "ROAD CLEAR");
  }
});

test("distance is rounded like the voice: 1 m under 20 m, 5 m beyond", () => {
  assert.equal(C.formatDistance(12.4), "12 m");
  assert.equal(C.formatDistance(37), "35 m");
  assert.equal(C.formatDistance(0.2), "1 m");
  assert.equal(C.formatDistance(NaN), "—");
});

test("direction, path and action texts", () => {
  assert.equal(C.directionText("right_to_left"), "Right → Left");
  assert.equal(C.directionText("none"), "Not crossing");
  assert.equal(C.directionText("unknown"), "—");
  assert.equal(C.pathText({ in_path: false, zone: "near_path" }, "NONE"), "Close to it");
  assert.equal(C.pathText({ in_path: false, zone: "outside" }, "ANIMAL_ENTERING_PATH"), "Entering");
  assert.equal(C.pathText({ in_path: false, zone: "outside" }, "NONE"), "No");
  assert.equal(C.actionText("CHANGE_LANE", "left"), "CHANGE LANE (LEFT)");
  assert.equal(C.actionText("BRAKE_IMMEDIATELY"), "BRAKE IMMEDIATELY");
  assert.equal(C.actionText("NONE"), "");
});

test("an object with no event is described by what it is", () => {
  const h = C.hudModel(ctx({ event: "NONE", risk_level: "LOW", primary: { track_id: 2, label: "pothole", distance_m: 40, direction: "none", approach: "steady", in_path: false, zone: "outside" } }));
  assert.equal(h.title, "Pothole detected");
  assert.equal(h.eventEmoji, "🕳️");
  assert.equal(h.riskText, "LOW RISK");
});

test("unknown level from the server falls back to LOW instead of crashing", () => {
  assert.equal(C.hudModel(ctx({ risk_level: "WHATEVER" })).level, "LOW");
});

test("voice plan: priority rises with danger, and only speaks when the server says so", () => {
  assert.deepEqual(C.voicePlan(ctx({ risk_level: "CRITICAL" })).priority, 5);
  assert.equal(C.voicePlan(ctx()).priority, 4);
  assert.equal(C.voicePlan(ctx({ risk_level: "MEDIUM" })).priority, 3);
  assert.equal(C.voicePlan(ctx({ speak: false })), null);
  assert.equal(C.voicePlan(ctx({ speech: "" })), null);
  assert.equal(C.voicePlan(null), null);
  assert.equal(C.voicePlan(ctx()).key, "context");
});

test("risk-event payload: only HIGH/CRITICAL, deduplicated per track+event", () => {
  const last = {};
  const opts = (over = {}) => ({ sessionId: "abc", gps: { lat: 11.01, lon: 76.95 }, speedMps: 5, lastLogged: last, nowMs: 1000, ...over });
  const p = C.riskEventPayload(ctx(), opts());
  assert.equal(p.event, "PEDESTRIAN_CROSSING");
  assert.equal(p.latitude, 11.01);
  assert.equal(p.session_id, "abc");
  assert.equal(C.riskEventPayload(ctx(), opts({ nowMs: 5000 })), null);              // same track+event within 30 s
  assert.ok(C.riskEventPayload(ctx(), opts({ nowMs: 40000 })));                       // cooldown over
  assert.equal(C.riskEventPayload(ctx({ risk_level: "MEDIUM" }), opts({ lastLogged: {} })), null);
  assert.equal(C.riskEventPayload(ctx({ event: "NONE" }), opts({ lastLogged: {} })), null);
  assert.equal(C.riskEventPayload(ctx(), opts({ lastLogged: {}, sessionId: "" })), null);
});

test("risk-event payload works without GPS or speed", () => {
  const p = C.riskEventPayload(ctx(), { sessionId: "abc", gps: null, speedMps: null, lastLogged: {}, nowMs: 0 });
  assert.equal(p.latitude, null);
  assert.equal(p.speed_mps, null);
});

test("session ids are unique and match the server's allowed pattern", () => {
  const a = C.newSessionId(), b = C.newSessionId();
  assert.notEqual(a, b);
  assert.match(a, /^[A-Za-z0-9_-]{1,64}$/);
});

test("object labels for the HUD list", () => {
  assert.equal(C.objectLabel({ label: "pedestrian", track_id: 4, distance_m: 12, direction: "left_to_right" }), "👤 Pedestrian #4 · 12 m →");
  assert.equal(C.objectLabel({ label: "pothole", track_id: 1, distance_m: 30, direction: "none" }), "🕳️ Pothole #1 · 30 m");
});
