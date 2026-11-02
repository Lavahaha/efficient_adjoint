"""Preserve completed CST solver logs before the next geometry clears them."""
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import time
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--root', default='results/divider_local_serial_smoke')
parser.add_argument('--case', default='divider')
args = parser.parse_args()
root = Path(args.root).resolve()
deadline = datetime.now() + timedelta(hours=1)
seen = set()
while datetime.now() < deadline:
    shapes = sorted(root.glob("iter_*/shape.json"), key=lambda p: p.stat().st_mtime)
    for tag in ("fwd", "bwd"):
        folder = root / "cst" / f"{args.case}_{tag}" / "Result"
        log = folder / "Model.log"
        if not log.exists():
            continue
        try:
            stamp = log.stat().st_mtime
            content = log.read_text(encoding="utf-8", errors="replace")
            if "Solver finished at:" not in content and "Postprocessing finished." not in content:
                continue
            candidates = [p for p in shapes if p.stat().st_mtime <= stamp]
            if not candidates:
                continue
            state = candidates[-1].parent
            key = (state.name, tag, stamp)
            if key in seen:
                continue
            for name in ("Model.log", "MCalc.log", "Model.msh", "simulation_overview.json",
                         "log.tet", "moegeom.tet", "topot.tet", "bc3d.tet", "tetquality.tet"):
                source = folder / name
                if source.exists():
                    shutil.copy2(source, state / f"cst_{tag}_{name}")
            seen.add(key)
            print(f"Captured {state.name} {tag}", flush=True)
        except (FileNotFoundError, PermissionError):
            pass
    if (root / "adjoint_levelset_summary.json").exists():
        break
    time.sleep(10)
