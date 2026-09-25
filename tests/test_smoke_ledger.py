"""Smoke test: the sample ledger validates with beancount's ``bean-check``.

Runs the checker through the project environment using ``sys.executable -m`` so it
does not depend on ``bean-check`` being on ``PATH``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_LEDGER = REPO_ROOT / "sample" / "smoke.beancount"


def test_smoke_ledger_passes_bean_check() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "beancount.scripts.check", str(SAMPLE_LEDGER)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"bean-check failed for {SAMPLE_LEDGER} (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
