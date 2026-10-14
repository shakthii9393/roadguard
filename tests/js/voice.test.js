// Run with:  node --test tests/js/voice.test.js
const test = require("node:test");
const assert = require("node:assert/strict");

// --- minimal fake of the browser speech API, installed before voice.js loads ---
const synth = {
  spoken: [],          // every utterance text that reached speak(), in order
  active: null,        // utterance currently "being spoken"
  speak(u) { if (u.volume === 0) return; this.spoken.push(u.text); this.active = u; },
  cancel() { const u = this.active; this.active = null; if (u && u.onerror) u.onerror({ error: "canceled" }); },
  getVoices() { return []; },
  addEventListener() {},
  finish() { const u = this.active; this.active = null; if (u && u.onend) u.onend({}); },   // speech naturally ends
  reset() { this.spoken = []; this.active = null; },
};
globalThis.window = globalThis;
globalThis.speechSynthesis = synth;
globalThis.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; this.volume = 1; } };
require("../../frontend/static/js/voice.js");
const V = globalThis.RoadGuardVoice;

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
function fresh() { V.setEnabled(true); V.cancelAll(); synth.reset(); }

test("speaks immediately when idle", () => {
  fresh();
  assert.equal(V.say("Hello", { priority: 1 }), true);
  assert.deepEqual(synth.spoken, ["Hello"]);
});

test("equal or lower priority waits for the current message instead of cutting it off", () => {
  fresh();
  V.say("Turn left in 200 meters.", { priority: 3 });
  V.say("Caution. Pothole on your route.", { priority: 2 });
  V.say("Another turn", { priority: 3 });
  assert.deepEqual(synth.spoken, ["Turn left in 200 meters."]);   // nothing interrupted it

  synth.finish();                                                  // first ends -> highest priority waiting goes next
  assert.deepEqual(synth.spoken, ["Turn left in 200 meters.", "Another turn"]);
  synth.finish();
  assert.deepEqual(synth.spoken, ["Turn left in 200 meters.", "Another turn", "Caution. Pothole on your route."]);
});

test("a more urgent message interrupts, and the cancelled one does not restart the queue", async () => {
  fresh();
  V.say("Turn left in 200 meters.", { priority: 3 });
  V.say("waiting hazard", { priority: 2 });
  V.say("Caution. Pothole detected ahead.", { priority: 4 });     // camera hazard
  await wait(120);                                                 // interrupt speaks after a short tick

  assert.equal(synth.spoken[1], "Caution. Pothole detected ahead.");
  assert.equal(synth.spoken.length, 2, "cancelled utterance's error event must not start 'waiting hazard' on top");
  assert.equal(synth.active.text, "Caution. Pothole detected ahead.");
});

test("stale messages are dropped instead of being spoken late", async () => {
  fresh();
  V.say("first", { priority: 3 });
  V.say("stale direction", { priority: 2, ttlMs: 30 });
  await wait(80);
  synth.finish();
  assert.deepEqual(synth.spoken, ["first"]);
});

test("a newer message with the same key replaces the one still waiting", () => {
  fresh();
  V.say("busy", { priority: 3 });
  V.say("Turn left in 500 meters.", { priority: 2, key: "turn:1" });
  V.say("Turn left in 200 meters.", { priority: 2, key: "turn:1" });
  synth.finish();
  assert.deepEqual(synth.spoken, ["busy", "Turn left in 200 meters."]);
});

test("turning voice off silences and cancels; say() reports false so the UI can still show it", () => {
  fresh();
  V.say("talking", { priority: 1 });
  V.setEnabled(false);
  assert.equal(synth.active, null);
  assert.equal(V.say("ignored", { priority: 4 }), false);
  assert.deepEqual(synth.spoken, ["talking"]);
  V.setEnabled(true);
});

test("hazard names are shared between camera alerts and route warnings", () => {
  assert.equal(V.label("construction_barrier"), "Road obstruction");
  assert.equal(V.label("traffic_signal"), "Traffic signal");
  assert.equal(V.label("something_new"), "something_new");
});
