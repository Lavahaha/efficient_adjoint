"""Retained CAD -> copy/reinitialize -> shared export, without CST solves."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import triangulate, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from eaopt.config import BoxSpec, CaseConfig, PolygonSpec
from eaopt.geometry.design import compose_metal_regions
from eaopt.geometry.state import CadGeometryState
from check_cad_gradient_transport import CadMesh, objective, test_shapes

OUT = ROOT/'results/cad_state_pipeline_2026-11-13'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    max_export_error, max_fd_error = 0., 0.
    for name, original in test_shapes().items():
        cfg = copy.deepcopy(CaseConfig.from_yaml(ROOT/'configs/antenna.yaml'))
        cfg.design_region.box = BoxSpec(x=(-16., 16.), y=(-16., 16.))
        cfg.design_region.grid_step_mm = .2
        parts = [original] if original.geom_type == 'Polygon' else original.geoms
        solid_parts = []
        for part in parts:
            solid_parts.extend([p for p in triangulate(part) if part.covers(p)] if part.interiors else [part])
        cfg.initial_metal = [PolygonSpec(vertices=np.asarray(p.exterior.coords).tolist()) for p in solid_parts]
        grid = als.Grid2D(161, 161, .2, -16., -16.)
        mesh = CadMesh.from_geometry(original, .4)
        points, _, weights, _ = mesh.quadrature()
        coefficients = (.03, -.02)
        g = 1+points@np.asarray(coefficients)
        velocity = mesh.gradient_velocity(g)
        predicted = float(np.sum(weights*g*mesh.normal_velocity(velocity)))
        baseline = objective(original, coefficients)
        scans = []
        for h in (0., .01, -.01, .001, -.001, 1e-6, -1e-6):
            moved = mesh.moved(velocity, h)
            state = CadGeometryState(moved, grid.x, grid.y)
            ls = als.LevelSet(grid, state.phi, geometry_state=state).copy()
            for _ in range(4):
                ls.reinit()
                ls.geometry_state.validate_phi(ls.phi)
            # Force persistence through JSON; exporting only saved phi would
            # still lose the CAD corners this gate is meant to protect.
            recovered = CadGeometryState.from_dict(json.loads(json.dumps(ls.geometry_state.to_dict())))
            regions = compose_metal_regions(cfg, ls.phi, geometry_state=recovered)
            exported = unary_union([Polygon(r.outer, r.holes) for r in regions])
            error = exported.boundary.hausdorff_distance(moved.boundary)
            max_export_error = max(max_export_error, error)
            assert error < 1e-11, (name, h, error)
            delta = objective(exported, coefficients)-baseline
            scans.append(dict(h=h, export_error_mm=error,
                actual_delta=delta, predicted_delta=h*predicted,
                taylor_remainder=abs(delta-h*predicted)))
        fd = (scans[-2]['actual_delta']-scans[-1]['actual_delta'])/2e-6
        fd_error = abs(fd-predicted)/abs(predicted)
        max_fd_error = max(max_fd_error, fd_error)
        assert fd_error < 1e-7, (name, fd_error)
        rows.append(dict(case=name, central_fd_relative_error=fd_error, scan=scans))
    sources = ('adjoint_levelset.py', 'eaopt/geometry/state.py', 'eaopt/geometry/design.py',
               'eaopt/runner.py', 'eaopt/plotting.py', 'scripts/check_cad_state_pipeline.py')
    result = dict(passed=True, cases=rows, max_export_error_mm=max_export_error,
        max_fd_relative_error=max_fd_error, copies_reinitializations_and_json_roundtrip=True,
        production_default_path_changed=False, production_geometry_gate_passed=False,
        full_goal_achieved=False,
        scope='CAD update reference through shared state/copy/reinit/export, not production HJ',
        source_hashes={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sources})
    (OUT/'results.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('cases', 'source_hashes')}, indent=2))


if __name__ == '__main__':
    main()
