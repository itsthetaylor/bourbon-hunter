"""Run each intake integration script in a subprocess with a fresh database.

The scripts are self-contained and exit non-zero on any failed check; their
full output is shown when one fails. Run with:  python -m pytest tests/
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPTS = HERE / "scripts"

# (script, needs a database)
CASES = [
    ("render_test.py", False),   # Flask collection template, mock data
    ("test_split.py",  True),    # multi-bottle split + photo assignment
    ("test_batch.py",  True),    # 20-image cap, downscale, failed/processed routing
    ("test_item6.py",  True),    # write-loop isolation, dedup, failed vision call
    ("test_shelf.py",  True),    # 110-photo / 30-bottle shelf, end to end
]


def _run(script, db_url):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["ANTHROPIC_API_KEY"] = env.get("ANTHROPIC_API_KEY") or "test-stub-never-called"
    if db_url:
        env["DATABASE_URL"] = db_url
    else:
        env.setdefault("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/unused")
    return subprocess.run([sys.executable, str(SCRIPTS / script)], env=env, cwd=str(REPO),
                          capture_output=True, text=True, timeout=600)


@pytest.mark.parametrize("script,needs_db", CASES, ids=[c[0] for c in CASES])
def test_script(script, needs_db, request):
    db_url = request.getfixturevalue("fresh_db") if needs_db else None
    r = _run(script, db_url)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-60:])
        pytest.fail(f"{script} exited {r.returncode}\n--- last 60 lines ---\n{tail}")
