"""Inspect saved CAD/failed samples without importing or starting CST."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eaopt.artifacts import load_shape
from eaopt.config import CaseConfig
from eaopt.geometry.contour import classify_metal_regions, SIMPLIFY_TOL_CELLS
from eaopt.geometry.design import design_box, polygon_union
from eaopt.solver.case import load_case


def inspect_ray(metal, p, n, offset):
    """Report signed CAD distances and exact metal intervals on the sample ray."""
    p, n = np.asarray(p, float), np.asarray(n, float)
    if not np.isclose(np.linalg.norm(n), 1., atol=1e-6):
        raise ValueError("Expected a unit normal")
    origin = Point(p)
    distance = float(origin.distance(metal.boundary))
    closest = nearest_points(origin, metal.boundary)[1]
    length = 4 * offset
    crossing = metal.intersection(LineString([p, p + length * n]))
    intervals = []

    def collect(geometry):
        if geometry.is_empty:
            return
        if geometry.geom_type == "LineString":
            positions = (np.asarray(geometry.coords) - p) @ n
            intervals.append([float(positions.min()), float(positions.max())])
        elif hasattr(geometry, "geoms"):
            for child in geometry.geoms:
                collect(child)

    collect(crossing)
    probes = []
    for delta in (1e-6, offset / 16, offset / 4, offset / 2, offset, 2 * offset):
        q = Point(p + delta * n)
        probes.append({"offset_mm": delta, "in_metal_xy": bool(metal.covers(q)),
                       "xy_mm": list(q.coords)[0]})
    return {"boundary_to_cad_mm": distance,
            "signed_boundary_to_cad_mm": -distance if metal.contains(origin) else distance,
            "boundary_inside_cad": bool(metal.contains(origin)),
            "nearest_cad_boundary_mm": list(closest.coords)[0],
            "ray_length_mm": length,
            "ray_metal_intervals_mm": sorted(intervals), "probes": probes}


def self_test():
    # Exact reported p/n: a 0.03 mm gap followed by another metal surface.
    p = np.array([28.4, -1.5342290698993466])
    n = np.array([-.39989457393739597, .9165611434789435])
    tangent = np.array([n[1], -n[0]])

    def strip(lo, hi):
        return Polygon([p + t * n + s * tangent
                        for t, s in ((lo, -1), (hi, -1), (hi, 1), (lo, 1))])

    narrow_gap = unary_union([strip(-1, 0), strip(.03, 1)])
    result = inspect_ray(narrow_gap, p, n, .05)
    assert not result["probes"][3]["in_metal_xy"]  # 0.025 mm is air
    assert result["probes"][4]["in_metal_xy"]      # 0.05 mm hits opposite metal
    assert any(abs(a - .03) < 1e-9 for a, _ in result["ray_metal_intervals_mm"])
    # A level-set boundary 0.01 mm inside the CAD, with a wrong pointing normal.
    wrong_normal = inspect_ray(box(28., -2., 28.41, 0.), p, n, .05)
    assert wrong_normal["boundary_inside_cad"]
    assert all(row["in_metal_xy"] for row in wrong_normal["probes"])
    print("Two geometry-only failure modes reproduced; neither is the actual failed CAD.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iteration_dir", nargs="?", type=Path)
    parser.add_argument("--index", type=int)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.iteration_dir is None:
        parser.error("Provide the iter_NNN directory containing the failed sample snapshot")
    folder = args.iteration_dir.resolve()
    config = json.loads((folder.parent / "run_config.json").read_text(encoding="utf-8"))
    cfg = CaseConfig.from_dict(config["config"])
    setup, model = load_case(cfg)
    regions = classify_metal_regions(load_shape(folder))
    fixed = polygon_union(model.fixed_metal_polygons())
    metal = unary_union([Polygon(r.outer, r.holes) for r in regions] + [fixed])
    frozen = polygon_union([p.vertices for p in cfg.initial_metal]).difference(design_box(cfg)).union(fixed)
    contact = design_box(cfg).boundary.intersection(frozen)
    diagnostic = json.loads((folder / "invalid_boundary_samples.json").read_text(encoding="utf-8"))
    offset, z = diagnostic["offset_mm"], diagnostic["z_mm"]
    substrate = unary_union([Polygon(p) for kind, p, _ in model.layout_view()["shapes"]
                             if kind == "substrate"])
    rows = []
    with np.load(folder / "invalid_boundary_samples.npz") as saved:
        indices = [args.index] if args.index is not None else np.flatnonzero(saved["active"])
        for i in indices:
            p, n = saved["boundary"][i], saved["normals"][i]
            q = Point(p + offset * n)
            in_pec = 0 < z <= setup.metal_thickness_mm and metal.covers(q)
            outside = not substrate.contains(q)
            if args.index is None and not (in_pec or outside):
                continue
            row = {"index": int(i), "active": bool(saved["active"][i]),
                   "boundary_mm": p.tolist(), "normal": n.tolist(),
                   "sample_mm": [*list(q.coords)[0], z], "in_pec": bool(in_pec),
                   "outside_substrate": bool(outside),
                   "fixed_metal_at_sample": bool(fixed.covers(q)),
                   "on_fixed_contact": bool(not contact.is_empty and contact.distance(Point(p)) <= 1e-9)}
            row.update(inspect_ray(metal, p, n, offset))
            rows.append(row)
    print(json.dumps({"folder": str(folder), "offset_mm": offset, "z_mm": z,
                      "nominal_simplify_tolerance_mm": SIMPLIFY_TOL_CELLS * cfg.design_region.grid_step_mm,
                      "note": "CAD ray inspection only; no fields or solver changes", "points": rows}, indent=2))


if __name__ == "__main__":
    main()
