/* RoadGuard context layer - browser side.

   The server (services/roadguard_context.py) decides WHAT is happening (risk level, road event,
   recommended action, spoken sentence). This file turns that JSON into what the rider sees and
   hears: HUD text, colours, voice priority, and which moments are worth logging.

   Same split as voice.js / guidance.js: everything here is a pure function of the `context`
   object, so it is unit-tested in Node (tests/js/context-ui.test.js). night-rider.js does the DOM.
*/
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RoadGuardContext = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

  const LEVEL_STYLE = {
    LOW:      { emoji: "✅", text: "LOW RISK",      color: "#2f8f5b" },
    MEDIUM:   { emoji: "⚠️", text: "MEDIUM RISK",   color: "#d98e04" },
    HIGH:     { emoji: "⚠️", text: "HIGH RISK",     color: "#e2572f" },
    CRITICAL: { emoji: "🚨", text: "CRITICAL RISK", color: "#d0021b" },
  };

  const EVENTS = {
    PEDESTRIAN_CROSSING:  { emoji: "👤", title: "Pedestrian crossing" },
    PEDESTRIAN_IN_PATH:   { emoji: "👤", title: "Pedestrian in your path" },
    PEDESTRIAN_NEAR_ROAD: { emoji: "👤", title: "Pedestrian near the road" },
    ANIMAL_ENTERING_PATH: { emoji: "🐕", title: "Animal entering your path" },
    ANIMAL_IN_PATH:       { emoji: "🐕", title: "Animal in your path" },
    POTHOLE_AHEAD:        { emoji: "🕳️", title: "Pothole ahead" },
    VEHICLE_APPROACHING:  { emoji: "🚗", title: "Vehicle approaching" },
    ROAD_OBSTRUCTION:     { emoji: "🚧", title: "Road obstruction" },
    ROAD_SURFACE_HAZARD:  { emoji: "⚠️", title: "Road surface hazard" },
    POSSIBLE_COLLISION:   { emoji: "💥", title: "Possible collision" },
    NONE:                 { emoji: "🛣️", title: "Monitoring the road" },
  };

  const ACTIONS = {
    NONE: "", STAY_ALERT: "STAY ALERT", REDUCE_SPEED: "REDUCE SPEED", CHANGE_LANE: "CHANGE LANE",
    PREPARE_TO_BRAKE: "PREPARE TO BRAKE", BRAKE_IMMEDIATELY: "BRAKE IMMEDIATELY",
  };

  const OBJECT_EMOJI = {
    pedestrian: "👤", animal: "🐕", vehicle: "🚗", pothole: "🕳️", debris: "🪨", construction_barrier: "🚧",
    parked_vehicle: "🚗", waterlogging: "🌊", speed_breaker: "⚠️", crack: "🪨", traffic_signal: "🚦",
    damaged_sign: "🛑", broken_streetlight: "💡",
  };

  const OBJECT_NAME = {
    pedestrian: "Pedestrian", animal: "Animal", vehicle: "Vehicle", pothole: "Pothole", debris: "Debris",
    construction_barrier: "Obstruction", parked_vehicle: "Parked vehicle", waterlogging: "Water", speed_breaker: "Speed breaker",
    crack: "Crack", traffic_signal: "Signal", damaged_sign: "Damaged sign", broken_streetlight: "Streetlight",
  };

  /** Round like the voice does: the estimate isn't precise enough to justify "37 metres". */
  function formatDistance(m) {
    if (!Number.isFinite(m)) return "—";
    const r = m < 20 ? Math.round(m) : Math.round(m / 5) * 5;
    return `${Math.max(r, 1)} m`;
  }

  function directionText(dir) {
    return { left_to_right: "Left → Right", right_to_left: "Right → Left", none: "Not crossing" }[dir] || "—";
  }

  function approachText(approach) {
    return { approaching: "Getting closer", receding: "Moving away", steady: "Same distance" }[approach] || "";
  }

  function pathText(primary, event) {
    if (!primary) return "—";
    if (primary.in_path) return "Yes";
    if (event === "ANIMAL_ENTERING_PATH") return "Entering";
    if (primary.zone === "near_path") return "Close to it";
    return "No";
  }

  function actionText(action, laneSide) {
    if (action === "CHANGE_LANE" && laneSide) return `CHANGE LANE (${String(laneSide).toUpperCase()})`;
    return ACTIONS[action] || "";
  }

  function objectLabel(o) {
    const arrow = { left_to_right: "→", right_to_left: "←" }[o.direction] || "";
    return `${OBJECT_EMOJI[o.label] || "⚠️"} ${OBJECT_NAME[o.label] || o.label} #${o.track_id} · ${formatDistance(o.distance_m)}${arrow ? " " + arrow : ""}`;
  }

  /** Everything the HUD shows, as plain strings. `ctx` is the response's `context` block. */
  function hudModel(ctx) {
    if (!ctx || !ctx.primary) {
      return { clear: true, level: "LOW", style: LEVEL_STYLE.LOW, riskText: "ROAD CLEAR", score: 0, eventEmoji: "🛣️",
               title: "Monitoring the road", distance: "", direction: "", approach: "", path: "", action: "", speech: "", objects: [] };
    }
    const level = LEVELS.includes(ctx.risk_level) ? ctx.risk_level : "LOW";
    const ev = EVENTS[ctx.event] || EVENTS.NONE;
    const p = ctx.primary;
    const named = ctx.event === "NONE" || !EVENTS[ctx.event];
    return {
      clear: false,
      level,
      style: LEVEL_STYLE[level],
      riskText: LEVEL_STYLE[level].text,
      score: Math.round(ctx.risk_score || 0),
      eventEmoji: named ? (OBJECT_EMOJI[p.label] || ev.emoji) : ev.emoji,
      title: named ? `${OBJECT_NAME[p.label] || p.label} detected` : ev.title,
      distance: formatDistance(p.distance_m),
      direction: directionText(p.direction),
      approach: approachText(p.approach),
      path: pathText(p, ctx.event),
      action: actionText(ctx.action, ctx.lane_side),
      speech: ctx.speech || "",
      objects: (ctx.objects || []).slice(0, 4).map((o) => ({ text: objectLabel(o), level: o.risk_level })),
    };
  }

  /** What to say, and how urgently. Only when the server's speech gate said `speak`. */
  function voicePlan(ctx) {
    if (!ctx || !ctx.speak || !ctx.speech) return null;
    const table = { CRITICAL: [5, 2500], HIGH: [4, 4000], MEDIUM: [3, 5000] };
    const [priority, ttlMs] = table[ctx.risk_level] || [3, 4000];
    return { text: ctx.speech, priority, ttlMs, key: "context" };
  }

  /** Colour for a box on the camera overlay. */
  function levelColor(level) {
    return (LEVEL_STYLE[level] || LEVEL_STYLE.LOW).color;
  }

  /**
   * HIGH / CRITICAL moments are logged to the server so admins can see where near-misses happen.
   * `lastLogged` is a plain object the caller keeps: "trackId:event" -> timestamp (ms).
   * Returns the request body, or null when there is nothing (new) to log.
   */
  function riskEventPayload(ctx, { sessionId, gps, speedMps, lastLogged, nowMs, cooldownMs = 30000 }) {
    if (!ctx || !ctx.primary || !sessionId) return null;
    if (ctx.risk_level !== "HIGH" && ctx.risk_level !== "CRITICAL") return null;
    if (!ctx.event || ctx.event === "NONE") return null;
    const key = `${ctx.primary.track_id}:${ctx.event}`;
    if (nowMs - (lastLogged[key] || -Infinity) < cooldownMs) return null;
    lastLogged[key] = nowMs;
    return {
      session_id: sessionId,
      event: ctx.event,
      risk_level: ctx.risk_level,
      risk_score: ctx.risk_score,
      action: ctx.action,
      label: ctx.primary.label,
      distance_m: ctx.primary.distance_m,
      speed_mps: Number.isFinite(speedMps) ? speedMps : null,
      latitude: gps ? gps.lat : null,
      longitude: gps ? gps.lon : null,
    };
  }

  /** One id per Night Rider run, so the server can keep that phone's tracker separate. */
  function newSessionId() {
    const c = typeof crypto !== "undefined" ? crypto : null;
    if (c && c.randomUUID) return c.randomUUID();
    return "s" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  return { LEVELS, LEVEL_STYLE, EVENTS, ACTIONS, formatDistance, directionText, approachText, pathText, actionText,
           objectLabel, hudModel, voicePlan, levelColor, riskEventPayload, newSessionId };
});
