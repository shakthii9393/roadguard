/* RoadGuard route guidance: turns a chosen route + the rider's GPS position into
   spoken events ("Turn left in 200 meters", "Caution. Road obstruction on your route...").

   Two layers, kept apart on purpose:
     GuidanceEngine  - pure logic. Give it a position, get back what to say. No browser
                       APIs, so it is unit-tested in Node (tests/js/guidance.test.js).
     GuidanceSession - the browser glue: GPS watching (or a simulated drive), and
                       handing the engine's events to RoadGuardVoice.

   All distances are measured ALONG the route (not straight-line), using the same
   yardstick the server used for step and hazard offsets. That keeps "200 meters"
   honest around bends and stops a turn on a parallel street from triggering early.
*/
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RoadGuardGuidance = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const DEFAULTS = {
    announceAtM: [500, 200],   // "in N meters" announcements, largest first
    announceWindowM: 100,      // a larger threshold only speaks if we cross it within this distance (avoids 300 m then 200 m back-to-back)
    nowM: 40,                  // "Turn left now" from here
    passedM: 10,               // a manoeuvre this close (or behind) is considered done
    arriveM: 30,               // "You have arrived" from here
    hazardWarnM: 300,          // warn about a route hazard from this far away
    hazardMinWarnM: 25,        // ...but not once it's basically underneath us (the camera covers that)
    hazardMergeM: 40,          // same-type reports this close together are one hazard
    offRouteM: 60,             // farther than this from the route = off route
    offRouteFixes: 3,          // ...for this many GPS fixes in a row
    offRouteRepeatMs: 30000,   // don't nag more often than this
    labelFor: (type) => String(type).replace(/_/g, " "),
  };

  /* ---------------- geometry helpers ---------------- */

  const R = 6371000;
  const rad = (d) => (d * Math.PI) / 180;

  function haversine(lat1, lon1, lat2, lon2) {
    const dPhi = rad(lat2 - lat1), dLam = rad(lon2 - lon1);
    const a = Math.sin(dPhi / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dLam / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  function cumulative(geometry) {
    const cum = [0];
    for (let i = 1; i < geometry.length; i++) {
      cum.push(cum[i - 1] + haversine(geometry[i - 1][0], geometry[i - 1][1], geometry[i][0], geometry[i][1]));
    }
    return cum;
  }

  /** Snap a point to the segments [fromSeg, toSeg] of the route. Same maths as the server's project_on_polyline_m. */
  function projectOnRoute(lat, lon, geometry, cum, fromSeg = 0, toSeg = geometry.length - 2) {
    let best = { dist: Infinity, along: 0, seg: fromSeg };
    const last = Math.min(toSeg, geometry.length - 2);
    for (let i = Math.max(0, fromSeg); i <= last; i++) {
      const [aLat, aLon] = geometry[i];
      const [bLat, bLon] = geometry[i + 1];
      const cosLat = Math.cos(rad(aLat));
      const bx = rad(bLon - aLon) * cosLat * R, by = rad(bLat - aLat) * R;
      const px = rad(lon - aLon) * cosLat * R, py = rad(lat - aLat) * R;
      const lenSq = bx * bx + by * by;
      const t = lenSq === 0 ? 0 : Math.max(0, Math.min(1, (px * bx + py * by) / lenSq));
      const dist = Math.hypot(px - t * bx, py - t * by);
      if (dist < best.dist) best = { dist, along: cum[i] + t * Math.sqrt(lenSq), seg: i };
    }
    return best;
  }

  /** The lat/lon that is `d` metres along the route (used by the demo drive). */
  function pointAt(geometry, cum, d) {
    if (d <= 0) return geometry[0];
    if (d >= cum[cum.length - 1]) return geometry[geometry.length - 1];
    let i = 1;
    while (i < cum.length && cum[i] < d) i++;
    const segLen = cum[i] - cum[i - 1] || 1;
    const t = (d - cum[i - 1]) / segLen;
    return [
      geometry[i - 1][0] + t * (geometry[i][0] - geometry[i - 1][0]),
      geometry[i - 1][1] + t * (geometry[i][1] - geometry[i - 1][1]),
    ];
  }

  /* ---------------- wording helpers ---------------- */

  /** Round a distance the way a person would say it: 1.2 kilometers / 350 meters / 60 meters. */
  function speakDistance(m) {
    if (m >= 950) {
      const km = Math.round(m / 100) / 10;
      return `${km} ${km === 1 ? "kilometer" : "kilometers"}`;
    }
    if (m >= 100) return `${Math.round(m / 50) * 50} meters`;
    return `${Math.max(10, Math.round(m / 10) * 10)} meters`;
  }

  function turnPhrase(step, remaining) {
    const d = speakDistance(remaining);
    if (step.maneuver_type === "arrive") return `Your destination is in ${d}.`;
    const road = step.road ? ` onto ${step.road}` : "";
    if (step.maneuver_type === "roundabout" || step.maneuver_type === "rotary") {
      const s = step.spoken;
      return `In ${d}, ${s.charAt(0).toLowerCase()}${s.slice(1)}${road}.`;
    }
    return `${step.spoken} in ${d}${road}.`;
  }

  /** Reports of the same hazard type within `mergeM` along the route are one hazard (people report the same pothole many times). */
  function dedupeHazards(hazards, mergeM) {
    const sorted = [...hazards].sort((a, b) => a.offset_m - b.offset_m);
    const lastByType = {};
    const kept = [];
    for (const h of sorted) {
      const prev = lastByType[h.hazard_type];
      if (prev !== undefined && h.offset_m - prev < mergeM) continue;
      lastByType[h.hazard_type] = h.offset_m;
      kept.push(h);
    }
    return kept;
  }

  /* ---------------- engine ---------------- */

  class GuidanceEngine {
    /** route = one entry of the /api/navigation/route response: { geometry, steps, hazards } */
    constructor(route, config = {}) {
      this.cfg = Object.assign({}, DEFAULTS, config);
      this.geometry = route.geometry || [];
      this.cum = cumulative(this.geometry);
      this.total = this.cum.length ? this.cum[this.cum.length - 1] : 0;
      this.steps = (route.steps || []).map((s, i) => ({ ...s, i, fired: new Set(), passed: false }));
      this.hazards = dedupeHazards(route.hazards || [], this.cfg.hazardMergeM).map((h) => ({ ...h, warned: false }));

      this.seg = 0;
      this.along = 0;
      this.started = false;
      this.arrived = false;
      this.offCount = 0;
      this.lastOffAt = -Infinity;
      this.next = null;
    }

    _snap(lat, lon) {
      // Look near where we last were first (cheap, and avoids jumping to a distant part of a looping route)...
      let snap = projectOnRoute(lat, lon, this.geometry, this.cum, this.seg - 2, this.seg + 150);
      // ...then fall back to the whole route (first fix, or recovering after a GPS gap).
      if (snap.dist > this.cfg.offRouteM) {
        const full = projectOnRoute(lat, lon, this.geometry, this.cum);
        if (full.dist < snap.dist) snap = full;
      }
      return snap;
    }

    /** Feed one GPS position. Returns { events, state }. */
    update(lat, lon, now = Date.now()) {
      const c = this.cfg;
      const events = [];
      if (this.geometry.length < 2) return { events, state: this._state(false) };

      const snap = this._snap(lat, lon);

      if (snap.dist > c.offRouteM) {
        this.offCount += 1;
        const confirmed = this.offCount >= c.offRouteFixes;
        if (confirmed && now - this.lastOffAt > c.offRouteRepeatMs) {
          this.lastOffAt = now;
          events.push({ kind: "offroute", text: "You appear to be off your route.", priority: 3, ttlMs: 6000, key: "offroute" });
        }
        // While off route, turn/hazard announcements would be wrong, so say nothing else.
        return { events, state: this._state(confirmed) };
      }

      this.offCount = 0;
      this.seg = snap.seg;
      // Never let GPS jitter move us backwards along the route
      this.along = this.started ? Math.max(this.along, snap.along) : snap.along;
      this.started = true;

      if (!this.arrived) {
        this._turnEvents(events);
        this._hazardEvents(events);
      }
      return { events, state: this._state(false) };
    }

    _turnEvents(events) {
      const c = this.cfg;

      for (const s of this.steps) {
        if (s.passed || s.maneuver_type === "arrive") continue;
        const rem = s.offset_m - this.along;
        // A spoken turn is only "done" once its "now" call has been made - otherwise a fast
        // rider on 1 Hz GPS (30 m per fix) can jump straight over the 10-40 m window and never hear it.
        // Far enough behind us, though, it's simply missed.
        const done = s.spoken ? (rem <= c.passedM && s.fired.has("now")) || rem < -c.passedM : rem <= c.passedM;
        if (done) s.passed = true;
      }

      const next = this.steps.find((s) => !s.passed && s.spoken);
      this.next = null;
      if (!next) return;

      const remaining = Math.max(0, next.offset_m - this.along);
      this.next = { text: next.spoken, road: next.road || "", distance_m: remaining, type: next.maneuver_type };

      if (next.maneuver_type === "arrive") {
        if (remaining <= c.arriveM) {
          next.passed = true;
          this.arrived = true;
          events.push({ kind: "arrive", text: "You have arrived at your destination.", priority: 3, ttlMs: 10000, key: "arrive" });
          return;
        }
      } else if (remaining <= c.nowM) {
        if (!next.fired.has("now")) {
          next.fired.add("now");
          c.announceAtM.forEach((t) => next.fired.add(t));   // the "in N meters" ones are moot now
          events.push({ kind: "turn", text: `${next.spoken} now.`, priority: 3, ttlMs: 4000, key: `turn-now:${next.i}` });
        }
        return;
      }

      const smallest = Math.min(...c.announceAtM);
      for (const t of c.announceAtM) {
        if (remaining > t || next.fired.has(t)) continue;
        next.fired.add(t);                                   // crossed (or first seen inside) this threshold
        const worthSaying = t === smallest || remaining >= t - c.announceWindowM;
        if (worthSaying) {
          events.push({ kind: "turn", text: turnPhrase(next, remaining), priority: 3, ttlMs: 6000, key: `turn:${next.i}` });
        }
      }
    }

    _hazardEvents(events) {
      const c = this.cfg;
      for (const h of this.hazards) {
        if (h.warned) continue;
        const remaining = h.offset_m - this.along;
        if (remaining < c.hazardMinWarnM) { h.warned = true; continue; }   // too late/passed - stay quiet
        if (remaining > c.hazardWarnM) continue;

        h.warned = true;
        const name = c.labelFor(h.hazard_type);
        events.push({
          kind: "hazard",
          text: `Caution. ${name} on your route, ${speakDistance(remaining)} ahead.`,
          priority: 2, ttlMs: 7000, key: `hazard:${h.id ?? h.offset_m}`,
          hazard: h, distance_m: remaining,
        });
        break;   // one at a time; a second nearby hazard is announced on the next fix
      }
    }

    _state(offRoute) {
      return {
        along: this.along,
        seg: this.seg,
        remaining_m: Math.max(0, this.total - this.along),
        offRoute,
        arrived: this.arrived,
        next: this.next,
      };
    }
  }

  /* ---------------- browser session (GPS + voice) ---------------- */

  const MAX_ACCEPTED_ACCURACY_M = 80;

  class GuidanceSession {
    /**
     * opts: {
     *   voice     - RoadGuardVoice (optional; without it events are only passed to onEvent)
     *   onEvent   - (event) => void        every spoken/visual event
     *   onState   - (state) => void        every fix: next turn, distance left, position
     *   onStatus  - (message) => void      GPS problems ("" clears)
     *   announceStart - false if the caller already says something at start-up
     *   simulate  - true = replay a drive along the route instead of using real GPS
     *   simSpeedMps - simulated speed (default 20 m/s = 72 km/h)
     * }
     */
    constructor(route, opts = {}) {
      this.route = route;
      this.opts = opts;
      this.voice = opts.voice || null;
      this.engine = new GuidanceEngine(route, { labelFor: (t) => (this.voice ? this.voice.label(t) : String(t)) });
      this.watchId = null;
      this.simTimer = null;
    }

    start() {
      if (this.voice && this.opts.announceStart !== false) this.voice.say("Voice guidance started.", { priority: 1, key: "start" });
      if (this.opts.simulate) return this._startSimulation();
      return this._startGps();
    }

    stop() {
      if (this.watchId !== null && typeof navigator !== "undefined") navigator.geolocation.clearWatch(this.watchId);
      this.watchId = null;
      clearInterval(this.simTimer);
      this.simTimer = null;
    }

    get running() { return this.watchId !== null || this.simTimer !== null; }

    _startGps() {
      if (typeof navigator === "undefined" || !("geolocation" in navigator)) {
        this._status("This browser can't share your location, so route guidance isn't available.");
        return false;
      }
      this.watchId = navigator.geolocation.watchPosition(
        (pos) => this._onFix(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy, pos.coords.heading, pos.coords.speed),
        (err) => this._status(err.code === 1
          ? "Location permission was denied. Allow location for this site to get voice directions."
          : "Can't get a GPS fix right now."),
        { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 },
      );
      return true;
    }

    _startSimulation() {
      const { geometry } = this.engine;
      const cum = this.engine.cum;
      const speed = this.opts.simSpeedMps || 20;
      const tickMs = 500;
      let d = 0;
      this.simTimer = setInterval(() => {
        d += (speed * tickMs) / 1000;
        const [lat, lon] = pointAt(geometry, cum, d);
        this._onFix(lat, lon, 5);
        if (d >= this.engine.total + 30) this.stop();
      }, tickMs);
      return true;
    }

    _status(msg) { if (this.opts.onStatus) this.opts.onStatus(msg); }

    _onFix(lat, lon, accuracy, heading = null, speed = null) {
      if (accuracy > MAX_ACCEPTED_ACCURACY_M) { this._status(`GPS is too weak for navigation (accuracy ±${Math.round(accuracy)} m). Go outdoors, or use a phone - laptops only know their rough location.`); return; }
      this._status("");
      const { events, state } = this.engine.update(lat, lon, Date.now());
      state.position = { lat, lon };
      state.heading = heading;   // degrees from north, or null when the GPS has none (e.g. standing still)
      state.speed = speed;       // m/s, or null
      for (const e of events) {
        if (this.voice) this.voice.say(e.text, { priority: e.priority, ttlMs: e.ttlMs, key: e.key });
        if (this.opts.onEvent) this.opts.onEvent(e);
      }
      if (this.opts.onState) this.opts.onState(state);
    }
  }

  /* ---------------- handing a route from the Navigation page to Night Rider ---------------- */

  const ACTIVE_ROUTE_KEY = "roadguard.activeRoute";
  const ACTIVE_ROUTE_MAX_AGE_MS = 6 * 60 * 60 * 1000;   // an old route from yesterday shouldn't silently steer a ride

  /** Remember the route the rider chose so /night-rider.html can guide along it. Returns false if storage refused. */
  function saveActiveRoute(route, destination) {
    try {
      sessionStorage.setItem(ACTIVE_ROUTE_KEY, JSON.stringify({ route, destination: destination || "", savedAt: Date.now() }));
      return true;
    } catch (_) { return false; }
  }

  function loadActiveRoute() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(ACTIVE_ROUTE_KEY) || "null");
      if (!saved || !saved.route || Date.now() - saved.savedAt > ACTIVE_ROUTE_MAX_AGE_MS) return null;
      return saved;
    } catch (_) { return null; }
  }

  function clearActiveRoute() {
    try { sessionStorage.removeItem(ACTIVE_ROUTE_KEY); } catch (_) { /* ignore */ }
  }

  /** "350 m" / "1.2 km" for on-screen text (speech uses speakDistance instead). */
  function formatDistance(m) {
    if (m >= 1000) return `${(m / 1000).toFixed(1)} km`;
    return `${Math.max(10, Math.round(m / 10) * 10)} m`;
  }

  return {
    GuidanceEngine, GuidanceSession, DEFAULTS,
    haversine, cumulative, projectOnRoute, pointAt, speakDistance, dedupeHazards, turnPhrase,
    saveActiveRoute, loadActiveRoute, clearActiveRoute, formatDistance,
  };
});