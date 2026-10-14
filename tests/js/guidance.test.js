// Run with:  node --test tests/js/guidance.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const G = require("../../frontend/static/js/guidance.js");

const M_PER_DEG = 111194.9;
const LABELS = { pothole: "Pothole", construction_barrier: "Road obstruction" };
const labelFor = (t) => LABELS[t] || t;

// Route: start -> 3 km north -> 2 km east. Only 3 vertices, so long straight
// segments (like real highways) exercise the segment projection, not vertex snapping.
const START = [11.0, 76.9];
const CORNER = [11.0 + 3000 / M_PER_DEG, 76.9];
const END = [CORNER[0], 76.9 + 2000 / (M_PER_DEG * Math.cos((CORNER[0] * Math.PI) / 180))];
const GEOMETRY = [START, CORNER, END];
const CUM = G.cumulative(GEOMETRY);

function makeRoute(extra = {}) {
  return {
    geometry: GEOMETRY,
    steps: [
      { maneuver_type: "depart", spoken: "", road: "Main Road", offset_m: 0 },
      { maneuver_type: "new name", spoken: "", road: "Main Road 2", offset_m: 1500 },   // silent step
      { maneuver_type: "turn", spoken: "Turn right", road: "Ring Road", offset_m: CUM[1] },
      { maneuver_type: "arrive", spoken: "Arrive at your destination", road: "", offset_m: CUM[2] },
    ],
    hazards: [
      { id: 1, hazard_type: "pothole", severity: "High", offset_m: 1200 },
      { id: 2, hazard_type: "pothole", severity: "Low", offset_m: 1215 },        // duplicate report, same spot
      { id: 3, hazard_type: "construction_barrier", severity: "High", offset_m: 4100 },
      { id: 4, hazard_type: "debris", severity: "Low", offset_m: 10 },           // right at the start: too late to warn
    ],
    ...extra,
  };
}

/** Ride the route in `stepM` increments, collecting every event with where it fired. */
function ride(engine, fromM = 0, toM = CUM[2] + 20, stepM = 10) {
  const out = [];
  for (let d = fromM; d <= toM; d += stepM) {
    const [lat, lon] = G.pointAt(GEOMETRY, CUM, d);
    const { events } = engine.update(lat, lon, d * 100);   // fake clock, monotonic
    events.forEach((e) => out.push({ ...e, at: d }));
  }
  return out;
}

test("speakDistance rounds like a person would talk", () => {
  assert.equal(G.speakDistance(1234), "1.2 kilometers");
  assert.equal(G.speakDistance(1000), "1 kilometer");
  assert.equal(G.speakDistance(520), "500 meters");
  assert.equal(G.speakDistance(203), "200 meters");
  assert.equal(G.speakDistance(64), "60 meters");
  assert.equal(G.speakDistance(3), "10 meters");
});

test("segment projection works on sparse vertices", () => {
  // Halfway up the first 3 km segment there is no vertex within 1.4 km - vertex snapping would fail badly
  const [lat, lon] = G.pointAt(GEOMETRY, CUM, 1500);
  const snap = G.projectOnRoute(lat, lon, GEOMETRY, CUM);
  assert.ok(snap.dist < 1, `off-route by ${snap.dist}`);
  assert.ok(Math.abs(snap.along - 1500) < 2, `along=${snap.along}`);
});

test("duplicate hazard reports within 40 m collapse into one", () => {
  const kept = G.dedupeHazards(makeRoute().hazards, 40);
  assert.equal(kept.filter((h) => h.hazard_type === "pothole").length, 1);
  assert.equal(kept.length, 3);
});

test("full ride: hazard warnings and turn instructions fire once each, in order", () => {
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  const events = ride(engine);
  const texts = events.map((e) => e.text);

  // Route hazards: warned once, ~300 m out, using the spoken label
  const pothole = events.filter((e) => e.text.includes("Pothole"));
  assert.equal(pothole.length, 1);
  assert.equal(pothole[0].text, "Caution. Pothole on your route, 300 meters ahead.");
  assert.ok(pothole[0].at >= 890 && pothole[0].at <= 910, `pothole warned at ${pothole[0].at}`);

  const obstruction = events.filter((e) => e.text.includes("Road obstruction"));
  assert.equal(obstruction.length, 1);
  assert.equal(obstruction[0].text, "Caution. Road obstruction on your route, 300 meters ahead.");

  // Hazard sitting at the very start is too late to warn about
  assert.ok(!texts.some((t) => t.includes("debris")));

  // Turn: 500 m, 200 m, then "now" - and nothing else for that step
  const turns = events.filter((e) => e.text.startsWith("Turn right")).map((e) => e.text);
  assert.deepEqual(turns, [
    "Turn right in 500 meters onto Ring Road.",
    "Turn right in 200 meters onto Ring Road.",
    "Turn right now.",
  ]);

  // Silent "new name" step never speaks; arrival sequence is announced
  assert.ok(!texts.some((t) => t.includes("Main Road 2")));
  assert.ok(texts.includes("Your destination is in 500 meters."));
  assert.equal(texts[texts.length - 1], "You have arrived at your destination.");
  assert.equal(events.filter((e) => e.kind === "arrive").length, 1);
});

test("turn priority beats route-hazard priority (queue relies on this)", () => {
  const events = ride(new G.GuidanceEngine(makeRoute(), { labelFor }));
  const turn = events.find((e) => e.kind === "turn");
  const hazard = events.find((e) => e.kind === "hazard");
  assert.ok(turn.priority > hazard.priority);
});

test("joining mid-approach speaks the real distance once, not the missed 500 m one", () => {
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  const events = ride(engine, 2850, 2990);   // 150 m before the turn
  const turns = events.filter((e) => e.kind === "turn").map((e) => e.text);
  assert.deepEqual(turns, ["Turn right in 150 meters onto Ring Road.", "Turn right now."]);
});

test("hazards already passed when guidance starts stay silent", () => {
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  const events = ride(engine, 1300, 2000);
  assert.equal(events.filter((e) => e.kind === "hazard").length, 0);
});

test("off-route is confirmed after 3 fixes, silences other alerts, and doesn't nag", () => {
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  const [lat, lon] = G.pointAt(GEOMETRY, CUM, 1000);
  const offLon = lon + 200 / (M_PER_DEG * Math.cos((lat * Math.PI) / 180));   // ~200 m east of the road

  let all = [];
  for (let i = 0; i < 6; i++) all = all.concat(engine.update(lat, offLon, 1000 + i * 1000).events);

  assert.equal(all.length, 1);
  assert.equal(all[0].kind, "offroute");
  assert.equal(all[0].text, "You appear to be off your route.");

  // Back on the road: normal guidance resumes (state no longer off-route)
  const back = engine.update(lat, lon, 20000);
  assert.equal(back.state.offRoute, false);
});

test("GPS jitter never moves progress backwards or re-fires announcements", () => {
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  ride(engine, 0, 2600);
  const before = engine.along;
  const [lat, lon] = G.pointAt(GEOMETRY, CUM, 2450);   // GPS says we're 150 m back
  const { events, state } = engine.update(lat, lon, 999999);
  assert.equal(events.length, 0);
  assert.ok(state.along >= before);
});

test("state reports next manoeuvre and distance left for the on-screen card", () => {
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  const [lat, lon] = G.pointAt(GEOMETRY, CUM, 2000);
  const { state } = engine.update(lat, lon, 1);
  assert.equal(state.next.text, "Turn right");
  assert.equal(state.next.road, "Ring Road");
  assert.ok(Math.abs(state.next.distance_m - 1000) < 5);
  assert.ok(Math.abs(state.remaining_m - 3000) < 5);
});

test("roundabout phrasing reads naturally", () => {
  const step = { maneuver_type: "roundabout", spoken: "At the roundabout, take exit 2", road: "Bypass Road" };
  assert.equal(G.turnPhrase(step, 200), "In 200 meters, at the roundabout, take exit 2 onto Bypass Road.");
});

test("a fast rider on coarse GPS still hears 'now' for a turn (jumps past the 10-40 m window)", () => {
  // 50 m between fixes = 180 km/h at 1 Hz: positions land on 2950 and then 3000 (the turn itself)
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  const turns = ride(engine, 0, CUM[2] + 20, 50).filter((e) => e.text.startsWith("Turn right")).map((e) => e.text);
  assert.equal(turns[turns.length - 1], "Turn right now.");
  assert.equal(turns.filter((t) => t === "Turn right now.").length, 1);
});

test("a turn that was skipped entirely by a GPS gap is dropped quietly, not announced late", () => {
  const engine = new G.GuidanceEngine(makeRoute(), { labelFor });
  ride(engine, 0, 2400, 10);
  const [lat, lon] = G.pointAt(GEOMETRY, CUM, 3200);   // 200 m past the turn, on the second leg
  const { events, state } = engine.update(lat, lon, 1e9);
  assert.ok(!events.some((e) => e.text.startsWith("Turn right")));
  assert.equal(state.next.type, "arrive");
});
