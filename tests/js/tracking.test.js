// Run with:  node --test tests/js/tracking.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const T = require("../../frontend/static/js/tracking.js");

test("arrowFor picks an arrow from the spoken instruction", () => {
  assert.equal(T.arrowFor("Turn left", "turn"), "←");
  assert.equal(T.arrowFor("Turn right", "turn"), "→");
  assert.equal(T.arrowFor("Turn sharp left", "turn"), "↰");
  assert.equal(T.arrowFor("Keep right", "fork"), "↗");
  assert.equal(T.arrowFor("Make a U-turn", "turn"), "↶");
  assert.equal(T.arrowFor("Take the roundabout", "roundabout"), "⟳");
  assert.equal(T.arrowFor("Arrive at your destination", "arrive"), "⚑");
  assert.equal(T.arrowFor("Continue straight", "continue"), "↑");
});

test("routeSpeedMps derives speed from distance and duration, with a fallback", () => {
  assert.ok(Math.abs(T.routeSpeedMps({ distance_km: 36, duration_min: 60 }) - 10) < 1e-9);
  assert.ok(T.routeSpeedMps({}) > 0);
});

test("etaSeconds uses route speed, blending in live speed only when clearly moving", () => {
  assert.equal(T.etaSeconds(1000, 10, null), 100);
  assert.equal(T.etaSeconds(1000, 10, 0), 100);        // stopped at a light: don't inflate the ETA
  assert.equal(T.etaSeconds(1000, 10, 2), 100);        // crawling below the 3 m/s threshold: ignored
  assert.equal(T.etaSeconds(1000, 10, 20), 1000 / 15); // moving fast: blend 50/50
});

test("arrivalDate adds the remaining seconds", () => {
  const now = new Date("2026-01-01T10:00:00Z");
  assert.equal(T.arrivalDate(now, 600).toISOString(), "2026-01-01T10:10:00.000Z");
});

test("bearing gives compass directions", () => {
  assert.ok(Math.abs(T.bearing(0, 0, 1, 0) - 0) < 0.01);     // north
  assert.ok(Math.abs(T.bearing(0, 0, 0, 1) - 90) < 0.01);    // east
  assert.ok(Math.abs(T.bearing(1, 0, 0, 0) - 180) < 0.01);   // south
  assert.ok(Math.abs(T.bearing(0, 1, 0, 0) - 270) < 0.01);   // west
});

test("splitAtDistance splits a polyline at the right place", () => {
  const geometry = [[0, 0], [0, 1], [0, 2]];
  const cum = [0, 100, 200];
  const mid = T.splitAtDistance(geometry, cum, 150);
  assert.deepEqual(mid.done, [[0, 0], [0, 1], [0, 1.5]]);
  assert.deepEqual(mid.left, [[0, 1.5], [0, 2]]);

  assert.deepEqual(T.splitAtDistance(geometry, cum, 0).done, [[0, 0]]);
  assert.deepEqual(T.splitAtDistance(geometry, cum, 500).left, [[0, 2]]);
  assert.deepEqual(T.splitAtDistance([], [], 10), { done: [], left: [] });
});

test("RerouteMonitor waits out a grace period before asking for a re-route", () => {
  const m = new T.RerouteMonitor({ graceMs: 8000, cooldownMs: 30000 });
  assert.equal(m.observe(false, 0), false);
  assert.equal(m.observe(true, 1000), false);    // just went off route
  assert.equal(m.observe(true, 5000), false);    // still inside the grace period
  assert.equal(m.observe(true, 9500), true);     // 8.5 s off route -> re-route
});

test("RerouteMonitor forgets a brief detour once the rider is back on route", () => {
  const m = new T.RerouteMonitor({ graceMs: 8000, cooldownMs: 30000 });
  m.observe(true, 0);
  m.observe(false, 4000);                         // back on route
  assert.equal(m.observe(true, 5000), false);     // a fresh detour restarts the clock
  assert.equal(m.observe(true, 12000), false);
  assert.equal(m.observe(true, 13500), true);
});

test("RerouteMonitor does not re-route again inside the cooldown", () => {
  const m = new T.RerouteMonitor({ graceMs: 1000, cooldownMs: 30000 });
  m.observe(true, 0);
  assert.equal(m.observe(true, 2000), true);
  m.markDone(2000);
  m.observe(true, 3000);
  assert.equal(m.observe(true, 10000), false);    // off route again, but too soon after the last re-route
  assert.equal(m.observe(true, 33000), true);     // cooldown over
});

test("pointAtDistance returns the point that far along the route", () => {
  const geometry = [[0, 0], [0, 1], [0, 2]];
  const cum = [0, 100, 200];
  assert.deepEqual(T.pointAtDistance(geometry, cum, 150), [0, 1.5]);
  assert.deepEqual(T.pointAtDistance(geometry, cum, 0), [0, 0]);
  assert.deepEqual(T.pointAtDistance(geometry, cum, 999), [0, 2]);
});

test("shortestDelta turns the short way round the compass", () => {
  assert.equal(T.shortestDelta(350, 10), 20);     // across north: +20, not -340
  assert.equal(T.shortestDelta(10, 350), -20);
  assert.equal(T.shortestDelta(90, 90), 0);
  assert.equal(Math.abs(T.shortestDelta(0, 180)), 180);
});