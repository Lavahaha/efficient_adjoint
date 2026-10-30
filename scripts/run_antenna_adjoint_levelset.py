"""Run the antenna case with adjoint_levelset."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eaopt.runner import main as run_case


def main():
    run_case(config_default=str(ROOT / "configs" / "antenna.yaml"),
             expected_case="antenna", maximize=False)


if __name__ == "__main__":
    main()
