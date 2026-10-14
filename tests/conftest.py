"""Give every pytest session its own throwaway SQLite database.

Several tests seed rows (e.g. a verified hazard on a route). With a fixed on-disk
file those rows piled up between runs and made counts drift, so the suite passed
on a fresh checkout and failed on the second run. This runs before any test module
imports `config`, so the whole session uses a fresh file.
"""
import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="roadguard-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test.db"
os.environ["JWT_SECRET"] = "test-secret"
