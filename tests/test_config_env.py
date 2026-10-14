import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config

EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def test_inline_comments_are_stripped():
    got = config._parse_env("A=1.2   # phone height\n# whole-line comment\n\nB = two words  # note\nURL=sqlite:///./x.db\nBAD LINE\n")
    assert got == {"A": "1.2", "B": "two words", "URL": "sqlite:///./x.db"}


def test_env_example_is_safe_to_copy_to_dotenv():
    """start.bat copies .env.example to .env, so every value in it must load as a valid setting."""
    values = config._parse_env(EXAMPLE.read_text())
    for key in ("CAMERA_HEIGHT_M", "CAMERA_HFOV_DEG", "HORIZON_Y_RATIO", "PATH_HALF_WIDTH_M", "PATH_NEAR_MARGIN_M",
                "CONFIDENCE_THRESHOLD", "MAX_UPLOAD_MB", "JWT_EXPIRE_MINUTES"):
        assert key in values, key
        float(values[key])                                       # would raise on "1.2   # comment"
    for key in ("DETECT_VEHICLES", "DEMO_SCENARIO", "USE_GENERAL_MODEL"):
        assert values[key].lower() in ("true", "false"), (key, values[key])
    assert all("#" not in v for v in values.values())
