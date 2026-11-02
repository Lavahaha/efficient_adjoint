"""Validate actual saved results of the two-update local divider run."""
import json
from pathlib import Path
import re
import sys

import numpy as np
from shapely.affinity import scale
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eaopt.solver import cst_model_divider as model

root = ROOT / "results/divider_local_serial_smoke"
def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def params(path):
    return {(i,j): complex(r,s) for i,j,r,s in read(path)}

summary = read(root / "adjoint_levelset_summary.json")
records = [json.loads(s) for s in (root / "adjoint_levelset_history.jsonl").read_text().splitlines()]
assert len(records) == 2 and all(r["updated"] for r in records)
evaluations = [json.loads(s) for s in (root / "cst_evaluations.jsonl").read_text().splitlines()]
assert len([r for r in evaluations if r["kind"] == "forward"]) == 3
fixed = [Polygon(v) for v in model.fixed_metal_polygons()]
rows = []
previous = None
for i in range(3):
    folder = root / f"iter_{i:03d}"
    shape = unary_union([Polygon(v) for v in read(folder / "shape.json")["polygons"]])
    assert shape.is_valid and shape.geom_type == "Polygon"
    full = unary_union([shape, *fixed])
    assert full.is_valid and full.geom_type == "Polygon"
    contact = [shape.intersection(p).length for p in fixed]
    assert np.allclose(contact, [2,2,2], atol=1e-8), contact
    fwd = params(folder / "s_params_fwd.json")
    row = {"state": i, "s_db": {f"S{k}1": float(20*np.log10(abs(fwd[k,1]))) for k in [1,2,3]},
        "s21_real": fwd[2,1].real, "s21_imag": fwd[2,1].imag,
        "p2_over_p3": abs(fwd[2,1]/fwd[3,1])**2,
        "output_phase_difference_deg_raw": float(np.angle(fwd[2,1]/fwd[3,1],deg=True)),
        "recorded_mode_power_sum": sum(abs(s)**2 for s in fwd.values()),
        "design_area_mm2": shape.area, "contact_lengths_mm": contact,
        "upper_branch_area_mm2": shape.intersection(box(model.FORK[0],0,model.BOX_X1,model.BOX_Y1)).area,
        "lower_branch_area_mm2": shape.intersection(box(model.FORK[0],model.BOX_Y0,model.BOX_X1,0)).area,
        "symmetry_difference_mm2": shape.symmetric_difference(scale(shape,yfact=-1,origin=(0,0))).area}
    if previous is not None:
        row["change_area_mm2"] = shape.symmetric_difference(previous).area
        row["contour_hausdorff_mm"] = shape.boundary.hausdorff_distance(previous.boundary)
    previous = shape
    row["fields"] = {}
    for tag in ["fwd"] + (["bwd"] if i < 2 else []):
        for kind in ("e", "h"):
            path = folder / f"fields_{tag}_{kind}.npz"
            with np.load(path) as data:
                assert np.isfinite(data["data"]).all()
                row["fields"][f"{tag}_{kind}"] = {"shape": list(data["data"].shape),
                    "origin": data["origin"].tolist(), "spacing": data["spacing"].tolist()}
        log = (folder / f"cst_{tag}_Model.log").read_text(encoding="utf-8", errors="replace")
        assert "Steady state energy criterion met" in log
        row[f"{tag}_mesh_cells"] = int(re.search(r"Number of mesh cells:\s*(\d+)",log)[1])
        row[f"{tag}_missing_qtem_ports"] = sorted(set(map(int,re.findall(
            r"Some \(Q\)TEM modes are not defined at port (\d+)",log))))
    if i < 2:
        backward = params(folder / "s_params_bwd.json")
        row["reciprocity_relative_complex"] = abs(fwd[2,1]-backward[1,2])/abs(fwd[2,1])
        row["reciprocity_magnitude_difference_db"] = float(20*np.log10(abs(backward[1,2])/abs(fwd[2,1])))
    rows.append(row)

with np.load(root / "iter_000/fields_fwd_e.npz") as data:
    axes = [data["origin"][j]+data["spacing"][j]*np.arange(data["data"].shape[j]) for j in range(3)]
    ix = int(np.argmin(abs(axes[0]-32)))
    iy = int(np.argmin(abs(axes[1]-3.6)))
    jy = int(np.argmin(abs(axes[1]+3.6)))
    iz = int(np.argmin(abs(axes[2]+.1)))
    e = data["data"][:,:,iz,:]
    ratio = e[ix,iy,2]/e[ix,jy,2]
    initial_symmetry = {"z_mm": float(axes[2][iz]),
        "relative_mirror_difference": float(np.linalg.norm(e-e[:,::-1,:]*[1,-1,1])/np.linalg.norm(e)),
        "Ez_upper_lower_phase_deg_at_x32_y3_6": float(np.angle(ratio,deg=True))}

bridge_events = [{"length_mm":float(length), "new_frozen_nodes":int(count)}
    for length,count in re.findall(r"桥接（最短路径 ([\d.]+) mm），冻结 (\d+) 节点",
        (root / "run.log").read_text(encoding="utf-8"))]
assert len(bridge_events) == 2 and sum(e["new_frozen_nodes"] for e in bridge_events) == 509
report = {"summary": summary, "rows": rows, "updates": records,
    "initial_field_symmetry": initial_symmetry,
    "connectivity_bridge_events": bridge_events,
    "checks": ["two updates and three forward evaluations", "valid connected geometry at all states",
        "all three 2 mm feed contacts preserved", "all saved forward/backward E/H fields finite",
        "completed CST solver logs retained for each solved state and excitation"]}
(root / "validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({"summary":summary,"rows":[{k:v for k,v in r.items() if k!="fields"} for r in rows],
    "updates":records},ensure_ascii=False,indent=2))
