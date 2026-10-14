"""Central configuration, read from environment variables (or a .env file)."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent           # backend/
PROJECT_DIR = BASE_DIR.parent                          # roadguard/


def _parse_env(text: str) -> dict[str, str]:
    """KEY=value lines; blank lines and # comments are skipped, and a trailing `  # comment` is dropped."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return values


def _load_dotenv() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for key, value in _parse_env(env_file.read_text()).items():
        os.environ.setdefault(key, value)


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./roadguard.db")

    # Auth
    jwt_secret: str = os.getenv("JWT_SECRET", "dev-secret-change-me")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))  # 8h shift

    # AI model
    model_path: Path = PROJECT_DIR / os.getenv("MODEL_PATH", "backend/ml/weights/best.pt")
    confidence_threshold: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.35"))

    # Optional second model: a general COCO-trained YOLO that already knows people,
    # animals and traffic lights, so those Night Rider alerts work before you have
    # labelled training data for them. Your road-damage model still handles potholes etc.
    use_general_model: bool = os.getenv("USE_GENERAL_MODEL", "false").strip().lower() in ("1", "true", "yes")
    general_model_path: str = os.getenv("GENERAL_MODEL_PATH", "yolov8n.pt")

    # Context-aware layer (tracking, distance, path, collision risk).
    # Also report vehicles (car/bus/truck/motorbike/bicycle) from the general model. They are
    # live-only: shown and analysed in Night Rider, never saved as hazard reports.
    detect_vehicles: bool = os.getenv("DETECT_VEHICLES", "true").strip().lower() in ("1", "true", "yes")
    # Camera mounting, used to turn pixels into metres. Defaults suit a phone on a handlebar /
    # dashboard, about 1.2 m up, facing straight ahead. Tune these against a known distance.
    camera_height_m: float = float(os.getenv("CAMERA_HEIGHT_M", "1.2"))
    camera_hfov_deg: float = float(os.getenv("CAMERA_HFOV_DEG", "65"))
    horizon_y_ratio: float = float(os.getenv("HORIZON_Y_RATIO", "0.42"))   # horizon line, as a fraction of frame height
    path_half_width_m: float = float(os.getenv("PATH_HALF_WIDTH_M", "1.0"))  # half the corridor the vehicle will sweep
    path_near_margin_m: float = float(os.getenv("PATH_NEAR_MARGIN_M", "1.5"))  # "close to the path" band beyond the corridor

    # Demo: with no trained model, Night Rider used to show one fixed fake pothole. Now (and with a
    # session id from the page) it plays a looping road scenario instead - pothole, pedestrian
    # crossing, animal, oncoming vehicle - so you can see the whole context pipeline.
    # Only ever applies in demo mode (no trained weights); set DEMO_SCENARIO=false to turn it off.
    demo_scenario: bool = os.getenv("DEMO_SCENARIO", "true").strip().lower() in ("1", "true", "yes")

    # Uploads
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "8"))
    upload_dir: Path = BASE_DIR / "uploads"

    # Routing (OSRM public demo server - see README for self-hosting notes)
    osrm_base_url: str = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org")

    frontend_dir: Path = PROJECT_DIR / "frontend"


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
