"""Runs the browser-side tests (voice queue + route guidance engine) with Node's built-in
test runner, so a plain `pytest` covers the whole project. Skipped if Node isn't installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
@pytest.mark.parametrize("test_file", sorted(p.name for p in JS_DIR.glob("*.test.js")))
def test_js_suite(test_file):
    result = subprocess.run(
        ["node", "--test", str(JS_DIR / test_file)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
