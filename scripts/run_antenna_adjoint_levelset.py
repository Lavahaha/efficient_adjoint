"""Run adjoint level-set optimization for the continuous-patch antenna case."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for directory in (ROOT, SCRIPTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from run_coupler_adjoint_levelset import main as _run_case


def main() -> None:
    _run_case(config_default=str(ROOT / "configs" / "antenna.yaml"),
              expected_case="antenna", maximize=False)


if __name__ == "__main__":
    main()
