"""Compare an exact length perturbation against the existing HJ/constraint chain."""
from __future__ import annotations

import json
import numpy as np
from shapely.geometry import Polygon

import check_antenna_geometry_consistency as d
from check_cad_normal_transport import normal_transport

OUT = d.ROOT/'results/antenna_directional_validation_2026-11-11/length_geometry'


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    base = Polygon(d.rectangle()[0])
    phi = d.INITIAL.phi.copy()
    normals_x, _ = d.als.LevelSet(d.GRID, phi).normals_grid()
    x, _ = np.meshgrid(d.INITIAL.xs, d.INITIAL.ys, indexing='ij')
    speed = ((x+20)/20)*normals_x
    vmax = float(abs(speed).max())
    records = []
    for h in (0., .08, -.08, .04, -.04, .02, -.02, .01, -.01, .001, -.001, 1e-6, -1e-6):
        expected = Polygon(d.rectangle(h, 'length', False)[0])
        assert abs(expected.area-(400+20*h)) < 1e-10
        cad = normal_transport(base, lambda xy, n: np.sum(d.displacement(xy, 'length')*n, axis=1), h)
        assert cad.boundary.hausdorff_distance(expected.boundary) < 1e-10
        # Negative perturbations use a reversed speed and positive pseudo-time.
        velocity = speed*(1 if h >= 0 else -1)
        chain = d.stages(phi.copy(), velocity, abs(h)*vmax)
        # Away from corners the frozen extension transports x+20 exponentially.
        flow_h = 20*np.expm1(h/20)
        flow_reference = Polygon(d.rectangle(flow_h, 'length', False)[0])
        row = dict(h_mm=h, pseudo_time_abs_mm=abs(h), velocity_max=vmax,
            analytic_area_mm2=expected.area, exact_cad_error=d.geometry_delta(cad, expected),
            straight_edge_flow_h_mm=float(flow_h), stages={})
        for name, values in chain.items():
            actual = d.shape(values)
            row['stages'][name] = dict(against_linear_cad=d.geometry_delta(actual, expected),
                against_straight_edge_flow=d.geometry_delta(actual, flow_reference),
                area_mm2=actual.area,
                area_directional_quotient_mm=(actual.area-base.area)/h if h else None,
                bounds=list(actual.bounds))
        if h == 0:
            assert row['stages']['full']['against_linear_cad']['hausdorff_mm'] < 1e-9
        records.append(row)
    result = dict(completed=True, cases=records,
        scope='existing production chain with prescribed length speed; no optimizer step changes',
        note='Finite-h scalar HJ evolution need not equal the affine CAD map; inspect convergence as h tends to zero',
        exact_cad_reference_passed=True)
    (OUT/'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps([(r['h_mm'],r['stages']['full']['against_linear_cad'],
        r['stages']['full']['area_directional_quotient_mm']) for r in records], indent=2))


if __name__ == '__main__':
    main()
