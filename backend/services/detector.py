"""Computer-vision wrapper around a YOLO model.

* If `ultralytics` is installed AND weights exist at MODEL_PATH -> real detection.
* Otherwise -> DEMO MODE returns a fake pothole so you can build/test the whole
  pipeline (API, DB, map, dashboard) before your model is trained. The demo
  result is flagged so the frontend can label it clearly.
* Optionally (USE_GENERAL_MODEL=true) a second, general COCO model adds pedestrians,
  animals and traffic signals on top of your road-damage model.
* Live frames that are dark get a brightness/contrast boost before detection
  (Night Rider Mode) - see `enhance_low_light`.
"""
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from config import settings
from .severity import compute_severity

log = logging.getLogger("roadguard.detector")

# Must match the class names used when training (see backend/ml/data.yaml).
# Keep in sync with models.HAZARD_TYPES minus the manual-only ones
# (accident, other) which AI doesn't currently classify.
CLASS_NAMES = [
    "pothole", "crack", "waterlogging", "debris", "construction_barrier",
    "parked_vehicle", "animal", "pedestrian", "speed_breaker",
    "damaged_sign", "broken_streetlight", "traffic_signal",
]

# Labels that are useful for a live rider alert but are NOT road defects, so they
# must never become a saved hazard report / map marker.
LIVE_ONLY_LABELS = {"traffic_signal", "vehicle"}

# General COCO model class -> RoadGuard class. Vehicles are deliberately absent:
# a single frame can't tell a *parked* vehicle from a moving one, so that class
# has to come from your own trained model.
GENERAL_LABEL_MAP = {
    "person": "pedestrian",
    "dog": "animal", "cat": "animal", "cow": "animal", "horse": "animal",
    "sheep": "animal", "elephant": "animal",
    "traffic light": "traffic_signal",
}

# Opt-in COCO vehicle classes -> one "vehicle" class. Off by default in
# remap_general_detections() because a single frame can't tell parked from moving; with
# the tracker (services/context) the motion between frames can, so the live pipeline
# turns it on via settings.detect_vehicles.
VEHICLE_LABEL_MAP = {
    "car": "vehicle", "bus": "vehicle", "truck": "vehicle",
    "motorcycle": "vehicle", "bicycle": "vehicle",
}

# Mean brightness (0-255 grey) below which a frame counts as "night" footage.
LOW_LIGHT_MEAN_THRESHOLD = 70


@dataclass
class Detection:
    label: str
    confidence: float
    bbox: list[float]     # x1, y1, x2, y2


@dataclass
class DetectionResult:
    detections: list[Detection]
    primary: Detection | None
    severity: str | None
    annotated_path: Path | None
    demo_mode: bool


@dataclass
class FrameResult:
    """Everything the live endpoint needs from one camera frame."""
    detections: list[Detection]
    width: int
    height: int
    demo_mode: bool
    low_light: bool


_model = None
_model_checked = False
_general_model = None
_general_checked = False


def _load_model():
    """Lazy-load the YOLO model once. Returns None if unavailable."""
    global _model, _model_checked
    if _model_checked:
        return _model
    _model_checked = True
    try:
        from ultralytics import YOLO  # heavy import, so done lazily
        if settings.model_path.exists():
            _model = YOLO(str(settings.model_path))
            log.info("Loaded YOLO weights from %s", settings.model_path)
        else:
            log.warning("No weights at %s - running in DEMO MODE", settings.model_path)
    except ImportError:
        log.warning("ultralytics not installed - running in DEMO MODE")
    return _model


def _load_general_model():
    """Lazy-load the optional general COCO model. Returns None if disabled/unavailable."""
    global _general_model, _general_checked
    if _general_checked:
        return _general_model
    _general_checked = True
    if not settings.use_general_model:
        return None
    try:
        from ultralytics import YOLO
        _general_model = YOLO(settings.general_model_path)   # auto-downloads e.g. yolov8n.pt if missing
        log.info("Loaded general model %s", settings.general_model_path)
    except Exception as exc:   # ImportError, or no network for the first-time download
        log.warning("General model unavailable (%s) - continuing without it", exc)
    return _general_model


def remap_general_detections(
    raw: list[tuple[str, float, list[float]]], skip_labels: set[str],
    include_vehicles: bool = False,
) -> list[Detection]:
    """Translate general-model (class_name, confidence, bbox) rows into RoadGuard
    detections, dropping classes we don't care about and any label your own
    model already covers (so nothing is reported twice). With `include_vehicles`,
    COCO car/bus/truck/motorcycle/bicycle rows become a live-only "vehicle" class."""
    found = []
    for name, conf, bbox in raw:
        mapped = GENERAL_LABEL_MAP.get(name)
        if mapped is None and include_vehicles:
            mapped = VEHICLE_LABEL_MAP.get(name)
        if mapped is None or mapped in skip_labels:
            continue
        found.append(Detection(mapped, round(float(conf), 3), [round(float(v), 1) for v in bbox]))
    return found


def _extract(results) -> list[Detection]:
    names = results.names
    return [
        Detection(
            label=names[int(box.cls)],
            confidence=round(float(box.conf), 3),
            bbox=[round(v, 1) for v in box.xyxy[0].tolist()],
        )
        for box in results.boxes
    ]


def _predict_all(model, source) -> list[Detection]:
    """Run your road-damage model, plus the general model if enabled.
    `source` can be an image path or a decoded numpy frame."""
    detections = _extract(model.predict(source, conf=settings.confidence_threshold, verbose=False)[0])

    general = _load_general_model()
    if general is not None:
        covered = set(model.names.values())
        res = general.predict(source, conf=settings.confidence_threshold, verbose=False)[0]
        raw = [
            (res.names[int(b.cls)], float(b.conf), b.xyxy[0].tolist())
            for b in res.boxes
        ]
        detections += remap_general_detections(raw, skip_labels=covered, include_vehicles=settings.detect_vehicles)
    return detections


def enhance_low_light(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """Brighten a dark frame so the detector can see it. Returns (frame, was_dark).

    Night footage from a phone is mostly noise and shadow; a gamma lift plus local
    contrast (CLAHE) noticeably helps. Frames that are already bright pass through
    untouched. Only the copy fed to the model is changed - never what's stored/shown.
    """
    mean = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    if mean >= LOW_LIGHT_MEAN_THRESHOLD:
        return img, False

    # Pick the gamma that would map the current mean brightness to mid-grey (~110),
    # clamped so extremely dark/noisy frames don't get blown out.
    target = 110.0
    gamma = float(np.log(target / 255.0) / np.log(max(mean, 1.0) / 255.0))
    gamma = min(max(gamma, 0.35), 0.9)
    lut = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)], dtype=np.uint8)
    lifted = cv2.LUT(img, lut)

    lab = cv2.cvtColor(lifted, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)
    l_chan = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_chan)
    return cv2.cvtColor(cv2.merge((l_chan, a_chan, b_chan)), cv2.COLOR_LAB2BGR), True


def _run_model(model, image_path: Path) -> list[Detection]:
    return _predict_all(model, str(image_path))


def _fake_detection(image_path: Path) -> list[Detection]:
    img = cv2.imread(str(image_path))
    h, w = img.shape[:2]
    return [Detection("pothole", 0.80, [w * 0.3, h * 0.5, w * 0.6, h * 0.8])]


def _annotate(image_path: Path, detections: list[Detection]) -> Path:
    img = cv2.imread(str(image_path))
    for d in detections:
        x1, y1, x2, y2 = map(int, d.bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 184, 245), 3)
        cv2.putText(img, f"{d.label} {d.confidence:.0%}", (x1, max(y1 - 8, 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 184, 245), 2)
    out = image_path.with_name(f"{image_path.stem}_annotated.jpg")
    cv2.imwrite(str(out), img)
    return out


def detect(image_path: Path) -> DetectionResult:
    """Run detection on a saved image and pick the most severe hazard as primary."""
    model = _load_model()
    demo = model is None
    detections = _fake_detection(image_path) if demo else _run_model(model, image_path)
    # Traffic signals help a live rider but aren't road defects - never save them as reports
    detections = [d for d in detections if d.label not in LIVE_ONLY_LABELS]

    if not detections:
        return DetectionResult([], None, None, None, demo)

    img = cv2.imread(str(image_path))
    img_area = img.shape[0] * img.shape[1]

    best, best_score, best_level = None, -1.0, "Low"
    for d in detections:
        x1, y1, x2, y2 = d.bbox
        ratio = max(0.0, (x2 - x1) * (y2 - y1) / img_area)
        level, score = compute_severity(d.label, d.confidence, ratio)
        if score > best_score:
            best, best_score, best_level = d, score, level

    return DetectionResult(detections, best, best_level, _annotate(image_path, detections), demo)


def detection_to_dict(d: Detection) -> dict:
    return asdict(d)


def detect_bytes(image_bytes: bytes) -> FrameResult:
    """
    Detect hazards directly from raw image bytes, no disk write and no
    annotated-image generation. Built for Night Rider Mode's fast
    detect-alert-discard camera loop, where persisting every frame to disk
    (like /api/hazards/detect does) would flood the uploads folder.

    Dark frames are brightened before detection. Returned boxes are in the
    original frame's pixel coordinates (enhancement doesn't change dimensions).
    """
    model = _load_model()
    demo = model is None

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image bytes")
    h, w = img.shape[:2]

    model_input, low_light = enhance_low_light(img)

    if demo:
        detections = [Detection("pothole", 0.80, [w * 0.35, h * 0.55, w * 0.65, h * 0.85])]
    else:
        detections = _predict_all(model, model_input)

    return FrameResult(detections, w, h, demo, low_light)
