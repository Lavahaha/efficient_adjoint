"""Real CST initial-geometry audit; no shape updates or field ASCII exports."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst import _apply_blocks, _connect, _run_solver, _save
from eaopt.solver.cst_results import read_s_params

cfg = CaseConfig.from_yaml(ROOT / "configs/divider.yaml")
setup, model = load_case(cfg)
out = ROOT / "results/divider_baseline_audit_2026-11-01" / datetime.now(
    timezone.utc).strftime("run_%Y%m%dT%H%M%S_%fZ")
out.mkdir(parents=True, exist_ok=False)

def log(message):
    print(message, flush=True)
    with (out / "run.log").open("a", encoding="utf-8") as stream:
        stream.write(str(message) + "\n")

def write(name, data):
    (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

initial = unary_union([Polygon(p.vertices) for p in cfg.initial_metal])
assert initial.is_valid and initial.geom_type == "Polygon" and not initial.interiors
polys = [np.asarray(initial.exterior.coords)]
write("run_config.json", {"config": asdict(cfg), "setup": asdict(setup),
      "purpose": "current initial CAD, forward/backward, no optimization, no ASCII field export"})
write("shape.json", {"polygons": [p.tolist() for p in polys], "unit": "mm"})
log(f"AUDIT_OUTPUT={out}")
de = None
project = None
results = {}
try:
    de = _connect(attach=False, force_new=True, log=log, quiet=True)
    for tag in ("fwd", "bwd"):
        stim = setup.stimulus(tag, cfg.objective)
        path = out / f"divider_{tag}.cst"
        project = de.new_mws()
        blocks = model.template_blocks(f"divider_{tag}", stim)
        blocks.append(("audit initial YAML union", model.design_region_update(
            polys, setup.metal_thickness_mm, material="PEC")))
        write(f"blocks_{tag}.json", blocks)
        _apply_blocks(project.model3d, blocks, what=tag, log=log)
        _save(project, path, log)
        seconds = _run_solver(project.model3d, log)
        _save(project, path, log)
        values = read_s_params(path, stim, setup.ports, setup.frequency_ghz, log=log)
        assert all(np.isfinite(s.real) and np.isfinite(s.imag) for s in values.values())
        results[tag] = {"seconds": seconds,
            "s_params": [[i, j, s.real, s.imag] for (i, j), s in values.items()],
            "s_db": {f"S{i}{j}": float(20*np.log10(abs(s))) for (i,j),s in values.items()}}
        write("s_params.json", results)
        log(f"RESULT_{tag}={results[tag]}")
        project.close()
        project = None
    write("status.json", {"status": "complete", "geometry_updates": 0})
except BaseException:
    write("status.json", {"status": "failed", "traceback": traceback.format_exc()})
    raise
finally:
    try:
        if project is not None:
            project.close()
    finally:
        if de is not None:
            de.close()
