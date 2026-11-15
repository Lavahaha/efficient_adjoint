"""Measure exact CAD -> nodal signed distance -> production CAD round trips."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

import check_antenna_geometry_consistency as d
from check_cad_normal_transport import normal_transport

OUT = d.ROOT / 'results/antenna_mesh_attribution_2026-11-11/cad_phi_roundtrip'


def reconstruct(phi):
    return unary_union([Polygon(r.outer, r.holes) for r in d.regions(phi)])


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    original = reconstruct(d.INITIAL.phi)
    rows = []
    for direction in ('uniform_inward', 'contact_fixed_inward'):
        def speed(xy, normals):
            if direction == 'uniform_inward':
                return -np.ones(len(xy))
            ramp = (xy[:, 0]+20.)/20.
            velocity = np.column_stack((-ramp, -ramp*xy[:, 1]/10.))
            return np.sum(velocity*normals, axis=1)

        for h in (.01, .001, .0001, .000001):
            moved = normal_transport(original, speed, h)
            if direction == 'uniform_inward':
                expected = original.buffer(-h, join_style=2)
            else:
                expected = Polygon([[-20., -10.], [-h, -10.+h], [-h, 10.-h], [-20., 10.]])
            assert moved.boundary.hausdorff_distance(expected.boundary) < 1e-10
            levelset = d.LevelSet2D(d.INITIAL.box, d.INITIAL.dx)
            levelset.init_from_polygons([np.asarray(moved.exterior.coords)])
            raw = reconstruct(levelset.phi)
            ls = d.als.LevelSet(d.GRID, levelset.phi)
            ls.reinit()
            reinitialized = reconstruct(ls.phi)
            assert raw.boundary.hausdorff_distance(reinitialized.boundary) < 1e-9
            record = dict(direction=direction, h_mm=h,
                explicit_cad_area_quotient_mm=(moved.area-original.area)/h,
                sdf_to_cad_area_quotient_mm=(raw.area-original.area)/h,
                sdf_to_cad_error=d.geometry_delta(raw, moved),
                reinit_extra_error=d.geometry_delta(reinitialized, raw))
            if direction == 'contact_fixed_inward':
                # Zero displacement gives dt=0 without a zero velocity divisor.
                processed = d.stages(levelset.phi, np.ones_like(levelset.phi), 0.)['full']
                full = reconstruct(processed)
                record['constraints_extra_error'] = d.geometry_delta(full, reinitialized)
                record['full_error_against_explicit_cad'] = d.geometry_delta(full, moved)
                record['full_area_quotient_mm'] = (full.area-original.area)/h
            rows.append(record)
    report = dict(scope='diagnostic round trip, no production geometry changes', cases=rows,
                  production_corner_mapping_fixed=False, step_control_modified=False)
    (OUT / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    main()
