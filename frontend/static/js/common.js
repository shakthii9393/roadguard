/* Shared helpers */
const HAZARD_LABELS = {
  pothole: "Pothole", crack: "Road crack", waterlogging: "Waterlogging",
  debris: "Debris", construction_barrier: "Construction", parked_vehicle: "Parked vehicle",
  animal: "Animal", pedestrian: "Pedestrian", speed_breaker: "Speed breaker",
  damaged_sign: "Damaged sign", broken_streetlight: "Broken streetlight",
  accident: "Accident", other: "Other hazard", traffic_signal: "Traffic signal",
};
const SEVERITY_COLORS = { Low: "#2f8f5b", Medium: "#d98e04", High: "#c8402f" };

const hazardLabel = (t) => HAZARD_LABELS[t] || t;
const fmtDate = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

/** Escape user-supplied text before putting it in innerHTML */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : "Request failed");
  }
  return res.status === 204 ? null : res.json();
}
