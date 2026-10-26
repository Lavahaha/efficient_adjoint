"""Run the exploratory divider optimizer with 4 GHz and 6 GHz targets."""

from pathlib import Path

from run_coupler import main as _run


def main() -> None:
    config = Path(__file__).resolve().parents[1] / "configs" / "divider_multifreq.yaml"
    _run(str(config))


if __name__ == "__main__":
    main()
