/* RoadGuard live tracking ("Track" button): the Google-Maps-style layer on top of guidance.js.

   guidance.js already knows WHAT to say and WHEN (turns, hazards, off-route). This file adds
   the parts that make it feel like a navigation app:
     - a heading arrow that follows the rider, with the map following it
     - the part of the route already driven turns grey
     - a "Re-center" escape hatch when the rider pans the map away
     - ETA / time-left maths
     - automatic re-routing after the rider has been off route for a while
     - keeping the phone screen awake while navigating

   Same split as guidance.js: the maths (arrowFor, etaSeconds, splitAtDistance, bearing,
   RerouteMonitor) is pure and unit-tested in Node (tests/js/tracking.test.js); Tracker and
   ScreenAwake touch the browser and are not.
*/
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RoadGuardTracking = api;
})(typeof self !== "undefined" ? self : this, function (root) {
  "use strict";

  const rad = (d) => (d * Math.PI) / 180;
  const deg = (r) => (r * 180) / Math.PI;

  /* ---------------- pure helpers ---------------- */

  /** Big arrow for the top banner, picked from the spoken instruction ("Turn left", "Take the roundabout"...). */
  function arrowFor(spoken, type) {
    const s = String(spoken || "").toLowerCase();
    const t = String(type || "").toLowerCase();
    if (t === "arrive") return "⚑";
    if (s.includes("u-turn") || s.includes("u turn")) return "↶";
    if (t === "roundabout" || t === "rotary" || s.includes("roundabout")) return "⟳";
    if (s.includes("sharp left")) return "↰";
    if (s.includes("sharp right")) return "↱";
    if (s.includes("slight left") || s.includes("keep left")) return "↖";
    if (s.includes("slight right") || s.includes("keep right")) return "↗";
    if (s.includes("left")) return "←";
    if (s.includes("right")) return "→";
    return "↑";
  }

  /** The route's own average speed in m/s (OSRM distance / OSRM duration). */
  function routeSpeedMps(route) {
    if (!route || !route.duration_min || !route.distance_km) return 8;   // ~29 km/h fallback
    return (route.distance_km * 1000) / (route.duration_min * 60);
  }

  /**
   * Seconds to go. Uses the route's average speed, nudged toward the rider's live GPS speed
   * once they're clearly moving (so a slow crawl in traffic shows up in the ETA, but a red
   * light doesn't send it to infinity).
   */
  function etaSeconds(remainingM, routeMps, liveMps) {
    let mps = routeMps > 0 ? routeMps : 8;
    if (Number.isFinite(liveMps) && liveMps > 3) mps = 0.5 * mps + 0.5 * liveMps;
    return remainingM / mps;
  }

  function arrivalDate(now, seconds) {
    return new Date(now.getTime() + seconds * 1000);
  }

  /** Initial compass bearing (0-360, 0 = north) from point 1 to point 2. */
  function bearing(lat1, lon1, lat2, lon2) {
    const dLon = rad(lon2 - lon1);
    const y = Math.sin(dLon) * Math.cos(rad(lat2));
    const x = Math.cos(rad(lat1)) * Math.sin(rad(lat2)) - Math.sin(rad(lat1)) * Math.cos(rad(lat2)) * Math.cos(dLon);
    return (deg(Math.atan2(y, x)) + 360) % 360;
  }

  /** Split a route polyline at `d` metres along it: { done: [...], left: [...] } (both include the split point). */
  function splitAtDistance(geometry, cum, d) {
    if (!geometry.length) return { done: [], left: [] };
    if (d <= 0) return { done: [geometry[0]], left: geometry.slice() };
    const total = cum[cum.length - 1];
    if (d >= total) return { done: geometry.slice(), left: [geometry[geometry.length - 1]] };
    let i = 1;
    while (i < cum.length && cum[i] < d) i++;
    const segLen = cum[i] - cum[i - 1] || 1;
    const t = (d - cum[i - 1]) / segLen;
    const point = [
      geometry[i - 1][0] + t * (geometry[i][0] - geometry[i - 1][0]),
      geometry[i - 1][1] + t * (geometry[i][1] - geometry[i - 1][1]),
    ];
    return { done: geometry.slice(0, i).concat([point]), left: [point].concat(geometry.slice(i)) };
  }

  /** The lat/lon `d` metres along the route. */
  function pointAtDistance(geometry, cum, d) {
    const { done } = splitAtDistance(geometry, cum, d);
    return done.length ? done[done.length - 1] : null;
  }

  /** Smallest signed turn (degrees, -180..180) to get from angle `from` to angle `to`. */
  function shortestDelta(from, to) {
    return ((((to - from) % 360) + 540) % 360) - 180;
  }

  /**
   * Decides when to re-route. Feed it every fix's "am I off route?" flag; it says true once the
   * rider has been off route for `graceMs` (a wrong turn they're about to U-turn out of isn't
   * worth a new route) and at least `cooldownMs` after the previous re-route.
   */
  class RerouteMonitor {
    constructor({ graceMs = 8000, cooldownMs = 30000 } = {}) {
      this.graceMs = graceMs;
      this.cooldownMs = cooldownMs;
      this.offSince = null;
      this.lastAt = -Infinity;
    }

    observe(offRoute, now) {
      if (!offRoute) { this.offSince = null; return false; }
      if (this.offSince === null) this.offSince = now;
      return now - this.offSince >= this.graceMs && now - this.lastAt >= this.cooldownMs;
    }

    markDone(now) {
      this.lastAt = now;
      this.offSince = null;
    }

    reset() {
      this.offSince = null;
      this.lastAt = -Infinity;
    }
  }

  /* ---------------- browser: map follower ---------------- */

  const MOVED_ENOUGH_FOR_BEARING_M = 3;
  const LOOKAHEAD_M = 35;   // heading = direction of the route this far ahead: steady, like a real nav app

  class Tracker {
    /**
     * map - Leaflet map, L - Leaflet
     * opts.mapEl  - the map's container element (#nav-map)
     * opts.stage  - its wrapper; the map is rotated inside it in heading-up mode
     * opts.onFollowChange(bool) - called when the rider pans away (false) or re-centers (true)
     * opts.onModeChange(bool)   - called when heading-up is switched on/off
     */
    constructor(map, L, opts = {}) {
      this.map = map;
      this.L = L;
      this.opts = opts;
      this.marker = null;
      this.doneLine = null;
      this.follow = true;
      this.headingUp = true;       // rotate the map so "up" is the direction of travel
      this.hdg = 0;                // continuous (unwrapped) heading in degrees, so animations never spin the long way round
      this.hdgInit = false;
      this.lastPos = null;
      this.geometry = [];
      this.cum = [];
      this._onDrag = () => this.setFollow(false);
      this._onResize = () => this._layoutRotatingMap();
    }

    begin(route) {
      this.setRoute(route);
      this.follow = true;
      this.lastPos = null;
      this.hdgInit = false;
      this.map.on("dragstart", this._onDrag);
      window.addEventListener("resize", this._onResize);
      this._applyMode();
    }

    /** Called at the start and again after a re-route. */
    setRoute(route) {
      this.geometry = route.geometry || [];
      this.cum = root.RoadGuardGuidance ? root.RoadGuardGuidance.cumulative(this.geometry) : [];
      if (this.doneLine) { this.map.removeLayer(this.doneLine); this.doneLine = null; }
    }

    setFollow(on) {
      if (this.follow === on) return;
      this.follow = on;
      if (this.opts.onFollowChange) this.opts.onFollowChange(on);
    }

    recenter() {
      this.setFollow(true);
      if (this.lastPos) this.map.setView(this.lastPos, Math.max(this.map.getZoom(), 17), { animate: true });
    }

    /** Switch between heading-up (map turns with you, locked on you) and north-up (fixed map you can drag). */
    setHeadingUp(on) {
      this.headingUp = on;
      this._applyMode();
      if (on) this.recenter();
      if (this.opts.onModeChange) this.opts.onModeChange(on);
    }

    /*
     * Heading-up without a rotation plugin: the map container is made a square as big as the
     * screen's diagonal (so no corner is ever empty) and CSS-rotated inside a clipping wrapper.
     * Dragging is turned off while rotating, because Leaflet would pan in the wrong direction.
     */
    _layoutRotatingMap() {
      const { mapEl, stage } = this.opts;
      if (!mapEl || !stage) return;
      if (!this.headingUp) {
        ["position", "width", "height", "left", "top", "transform", "transition"].forEach((k) => { mapEl.style[k] = ""; });
        this.map.invalidateSize();
        return;
      }
      const w = stage.clientWidth, h = stage.clientHeight;
      const d = Math.ceil(Math.hypot(w, h));
      Object.assign(mapEl.style, {
        position: "absolute", width: `${d}px`, height: `${d}px`,
        left: `${(w - d) / 2}px`, top: `${(h - d) / 2}px`, transformOrigin: "50% 50%",
        transition: "transform .6s linear",
      });
      this.map.invalidateSize();
      this._rotateMap();
    }

    _rotateMap() {
      const { mapEl } = this.opts;
      if (mapEl && this.headingUp) mapEl.style.transform = `rotate(${-this.hdg}deg)`;
    }

    _applyMode() {
      if (this.headingUp) this.map.dragging.disable(); else this.map.dragging.enable();
      if (!this.headingUp) { this.hdgInit = false; if (this.opts.mapEl) this.opts.mapEl.style.transform = ""; }
      this._layoutRotatingMap();
    }

    _icon() {
      return this.L.divIcon({
        className: "me-icon",
        iconSize: [44, 44],
        iconAnchor: [22, 22],
        html: `<div class="me-arrow"><svg viewBox="0 0 40 40" width="44" height="44" aria-hidden="true">
          <circle cx="20" cy="20" r="18" fill="rgba(255,198,26,.28)"/>
          <path d="M20 5 L31 33 L20 27 L9 33 Z" fill="#ffc61a" stroke="#14181d" stroke-width="2.5" stroke-linejoin="round"/>
        </svg></div>`,
      });
    }

    /** Best available heading: the route's own direction ahead of us > GPS heading > direction we just moved. */
    _pickHeading(state, ll) {
      const G = root.RoadGuardGuidance;
      if (!state.offRoute && this.geometry.length > 1 && this.cum.length) {
        const here = pointAtDistance(this.geometry, this.cum, state.along || 0);
        const ahead = pointAtDistance(this.geometry, this.cum, (state.along || 0) + LOOKAHEAD_M);
        if (here && ahead && G && G.haversine(here[0], here[1], ahead[0], ahead[1]) >= 5) {
          return bearing(here[0], here[1], ahead[0], ahead[1]);
        }
      }
      if (Number.isFinite(state.heading)) return state.heading;
      if (this.lastPos && G && G.haversine(this.lastPos[0], this.lastPos[1], ll[0], ll[1]) >= MOVED_ENOUGH_FOR_BEARING_M) {
        return bearing(this.lastPos[0], this.lastPos[1], ll[0], ll[1]);
      }
      return null;
    }

    update(state) {
      const p = state.position;
      if (!p) return;
      const ll = [p.lat, p.lon];

      const target = this._pickHeading(state, ll);
      if (target !== null) {
        if (!this.hdgInit) { this.hdg = target; this.hdgInit = true; }
        else this.hdg += shortestDelta(this.hdg, target) * 0.5;   // smooth out jitter
      }

      if (!this.marker) this.marker = this.L.marker(ll, { icon: this._icon(), interactive: false, keyboard: false, zIndexOffset: 1000 }).addTo(this.map);
      else this.marker.setLatLng(ll);
      const el = this.marker.getElement();
      // The arrow lives inside the (possibly rotated) map, so giving it the true heading makes it point
      // "up" on screen in heading-up mode and along the road in north-up mode.
      if (el && el.firstChild) el.firstChild.style.transform = `rotate(${this.hdg}deg)`;

      // Grey out the stretch already driven (skipped while off route: the snapped distance is stale then).
      if (!state.offRoute && this.geometry.length > 1 && this.cum.length) {
        const { done } = splitAtDistance(this.geometry, this.cum, state.along || 0);
        if (done.length > 1) {
          if (this.doneLine) this.doneLine.setLatLngs(done);
          else this.doneLine = this.L.polyline(done, { color: "#7d8791", weight: 6, opacity: 0.95, interactive: false }).addTo(this.map);
        }
      }

      this.lastPos = ll;
      this._rotateMap();
      if (this.follow) {
        if (this.map.getZoom() < 16) this.map.setView(ll, 17, { animate: false });
        else this.map.panTo(ll, { animate: true, duration: 0.5 });
      }
    }

    end() {
      this.map.off("dragstart", this._onDrag);
      window.removeEventListener("resize", this._onResize);
      this.headingUp = false;          // restores the normal map layout
      this.map.dragging.enable();
      this._layoutRotatingMap();
      this.headingUp = true;           // default for the next trip
      if (this.marker) { this.map.removeLayer(this.marker); this.marker = null; }
      if (this.doneLine) { this.map.removeLayer(this.doneLine); this.doneLine = null; }
      this.lastPos = null;
      this.follow = true;
    }
  }

  /* ---------------- browser: keep the screen on ---------------- */

  const ScreenAwake = {
    sentinel: null,
    wanted: false,

    async acquire() {
      this.wanted = true;
      try {
        if (typeof navigator !== "undefined" && "wakeLock" in navigator) {
          this.sentinel = await navigator.wakeLock.request("screen");
        }
      } catch (_) { /* denied or unsupported - navigation still works, the screen may just dim */ }
    },

    release() {
      this.wanted = false;
      if (this.sentinel) { this.sentinel.release().catch(() => {}); this.sentinel = null; }
    },
  };

  // The browser drops a wake lock whenever the tab is hidden; take it back when the rider returns.
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && ScreenAwake.wanted) ScreenAwake.acquire();
    });
  }

  return { arrowFor, routeSpeedMps, etaSeconds, arrivalDate, bearing, splitAtDistance, pointAtDistance, shortestDelta, RerouteMonitor, Tracker, ScreenAwake };
});