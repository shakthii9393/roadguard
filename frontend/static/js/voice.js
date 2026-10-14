/* RoadGuard Voice Safety Assistant - one place that decides what gets spoken, and when.

   Why a queue instead of calling speechSynthesis directly:
   a rider can get a pothole warning and a "turn left" instruction within the same
   second. Cancelling the old message for every new one (what a bare speak() does)
   would cut off "Turn left in 200 meters" halfway. Instead each message has a
   priority: a more urgent one interrupts, an equal or less urgent one waits its
   turn, and anything that waits too long is dropped because stale directions
   ("turn left in 200 meters" 10 seconds late) are worse than silence.

   Priorities:  5 = critical collision risk   4 = live camera hazard   3 = turn / off-route   2 = hazard on route   1 = info
*/
(function (root) {
  "use strict";

  const LANG = "en-IN";                       // falls back to any English voice, then the browser default
  const PREF_KEY = "roadguard.voice.enabled";

  /* How each hazard type is named out loud. Shared by the camera alerts and the
     route warnings so a rider hears the same words for the same thing. */
  const SPOKEN_LABELS = {
    pothole: "Pothole", crack: "Road damage", waterlogging: "Waterlogged road",
    debris: "Road debris", construction_barrier: "Road obstruction", parked_vehicle: "Parked vehicle",
    animal: "Animal", pedestrian: "Pedestrian", speed_breaker: "Speed breaker",
    damaged_sign: "Damaged sign", broken_streetlight: "Broken streetlight",
    traffic_signal: "Traffic signal", vehicle: "Vehicle", accident: "Accident", other: "Road hazard",
  };

  const supported = typeof window !== "undefined" && "speechSynthesis" in window
    && typeof window.SpeechSynthesisUtterance !== "undefined";

  let enabled = readPref();
  let current = null;        // { id, priority } - what's being spoken right now
  let nextId = 1;
  let queue = [];            // waiting messages: { text, priority, key, expiresAt, addedAt }

  function readPref() {
    try { return localStorage.getItem(PREF_KEY) !== "off"; } catch (_) { return true; }
  }

  function pickVoice() {
    const voices = window.speechSynthesis.getVoices() || [];
    return voices.find((v) => v.lang === LANG)
      || voices.find((v) => v.lang && v.lang.toLowerCase().startsWith("en"))
      || null;
  }

  function speakNow(item) {
    const id = nextId++;
    current = { id, priority: item.priority };

    const utter = new SpeechSynthesisUtterance(item.text);
    utter.lang = LANG;
    utter.rate = 1.0;
    const voice = pickVoice();
    if (voice) utter.voice = voice;

    // Handlers only act if this utterance is still the current one - cancelling an
    // utterance fires its "end"/"error" events too, and those must not start the queue.
    const finish = () => { if (current && current.id === id) { current = null; pump(); } };
    utter.onend = finish;
    utter.onerror = finish;
    window.speechSynthesis.speak(utter);
  }

  function pump() {
    if (!enabled || current) return;
    const now = Date.now();
    queue = queue.filter((q) => q.expiresAt > now);
    if (queue.length === 0) return;

    // Highest priority first; among equals, the one that has waited longest.
    queue.sort((a, b) => b.priority - a.priority || a.addedAt - b.addedAt);
    speakNow(queue.shift());
  }

  /**
   * Speak `text`.
   *   priority - see table at the top of this file
   *   ttlMs    - how long the message stays worth saying if it has to wait
   *   key      - a newer message with the same key replaces an older waiting one
   * Returns false when voice is off/unsupported (callers should still show it on screen).
   */
  function say(text, { priority = 1, ttlMs = 8000, key = null } = {}) {
    if (!supported || !enabled || !text) return false;

    const item = { text, priority, key, addedAt: Date.now(), expiresAt: Date.now() + ttlMs };
    if (key) queue = queue.filter((q) => q.key !== key);

    if (!current) {
      speakNow(item);
    } else if (priority > current.priority) {
      // More urgent than what's being said: cut in. Clear `current` first so the
      // cancelled utterance's own end/error event doesn't trigger pump().
      current = null;
      window.speechSynthesis.cancel();
      // Some browsers drop a speak() issued in the same tick as cancel()
      setTimeout(() => speakNow(item), 60);
    } else {
      queue.push(item);
    }
    return true;
  }

  /** Call once from a tap/click. Mobile browsers only allow speech after a user gesture. */
  function unlock() {
    if (!supported) return;
    const u = new SpeechSynthesisUtterance("ready");
    u.volume = 0;
    window.speechSynthesis.speak(u);
  }

  function cancelAll() {
    queue = [];
    current = null;
    if (supported) window.speechSynthesis.cancel();
  }

  function setEnabled(on) {
    enabled = !!on;
    try { localStorage.setItem(PREF_KEY, enabled ? "on" : "off"); } catch (_) { /* private mode - fine */ }
    if (!enabled) cancelAll();
  }

  // Chrome loads voices lazily; touching the list early means the first alert can use en-IN.
  if (supported) {
    window.speechSynthesis.getVoices();
    window.speechSynthesis.addEventListener?.("voiceschanged", () => {});
  }

  root.RoadGuardVoice = {
    say, unlock, cancelAll, setEnabled,
    isEnabled: () => enabled,
    isSupported: () => supported,
    labels: SPOKEN_LABELS,
    label: (type) => SPOKEN_LABELS[type] || type,
  };
})(typeof window !== "undefined" ? window : globalThis);
