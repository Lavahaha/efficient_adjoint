"""Offline prototype: retain CAD as geometry and phi as its sampled auxiliary state."""
from __future__ import annotations

import json

import numpy as np
from shapely import contains_xy, distance, points
from shapely.geometry import Polygon
from shapely.ops import unary_union

import check_antenna_geometry_consistency as d
from check_cad_normal_transport import cases, normal_transport
from eaopt.geometry.design import _regions, contour_point_limit

OUT = d.ROOT/'results/antenna_isolation_2026-11-11/cad_state'


def synchronized_state(cad):
    if cad.geom_type != 'Polygon' or not cad.is_valid or cad.area <= 0:
        raise ValueError('Prototype requires valid polygon topology')
    x, y = np.meshgrid(d.INITIAL.xs, d.INITIAL.ys, indexing='ij')
    absolute = distance(cad.boundary, points(x, y))
    phi = np.where(contains_xy(cad, x, y), -absolute, absolute)
    regions = _regions(cad)
    if any(len(ring) > contour_point_limit(d.CFG) for ring in d.region_rings(regions)):
        raise ValueError('CAD exceeds the existing export point budget')
    return cad, phi, regions


def validate_phi_sync(cad, phi):
    expected = synchronized_state(cad)[1]
    if phi.shape != expected.shape or not np.isfinite(phi).all():
        raise ValueError('Invalid auxiliary phi')
    # Positive rescaling during reinitialization is allowed, interface movement is not.
    if not np.array_equal(phi < 0, expected < 0) or not np.array_equal(phi == 0, expected == 0):
        raise ValueError('Phi changed material signs without a synchronized CAD update')
    for axis in (0, 1):
        first = [slice(None), slice(None)]; second = first.copy()
        first[axis] = slice(None, -1); second[axis] = slice(1, None)
        a, b = expected[tuple(first)], expected[tuple(second)]
        c, e = phi[tuple(first)], phi[tuple(second)]
        cut = a*b < 0
        if not np.allclose(a[cut]/(a[cut]-b[cut]), c[cut]/(c[cut]-e[cut]), atol=1e-9, rtol=0):
            raise ValueError('Phi moved interface crossings without a CAD update')


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    rows = []
    for name, original in cases().items():
        for sign in (-1, 1):
            for h in (.01, .001, .0001, .000001):
                speed = lambda xy, normals: sign*np.ones(len(xy))
                moved = normal_transport(original, speed, h)
                expected = original.buffer(sign*h, join_style=2)
                cad, phi, regions = synchronized_state(moved)
                exported = unary_union([Polygon(r.outer, r.holes) for r in regions])
                assert exported.boundary.hausdorff_distance(expected.boundary) < 1e-10
                assert len(exported.interiors) == len(original.interiors)
                validate_phi_sync(cad, phi)
                ls = d.als.LevelSet(d.GRID, phi.copy()); ls.reinit()
                validate_phi_sync(cad, ls.phi)
                second = normal_transport(cad, lambda xy, n: np.zeros(len(xy)), 1.)
                assert second.boundary.hausdorff_distance(cad.boundary) < 1e-12
                quotient = (cad.area-original.area)/h
                assert abs(quotient-sign*original.length) < 100*h+1e-6
                rows.append(dict(case=name, sign=sign, h_mm=h,
                    export_hausdorff_mm=exported.boundary.hausdorff_distance(expected.boundary),
                    area_quotient_mm=quotient, expected_derivative_mm=sign*original.length))
    original = Polygon(d.rectangle()[0])
    contact_rows = []
    for h in (.01, .001, .0001, .000001):
        def speed(xy, normals):
            ramp = (xy[:, 0]+20)/20
            velocity = np.column_stack((-ramp, -ramp*xy[:, 1]/10))
            return np.sum(velocity*normals, axis=1)
        cad = normal_transport(original, speed, h)
        expected = Polygon([[-20, -10], [-h, -10+h], [-h, 10-h], [-20, 10]])
        cad, phi, regions = synchronized_state(cad)
        assert cad.boundary.hausdorff_distance(expected.boundary) < 1e-10
        processed = d.stages(phi.copy(), np.ones_like(phi), 0.)['full']
        validate_phi_sync(cad, processed)
        # Export uses retained CAD, never reconstructs corners from sampled phi.
        exported = unary_union([Polygon(r.outer, r.holes) for r in regions])
        assert exported.boundary.hausdorff_distance(expected.boundary) < 1e-10
        contact_rows.append(dict(h_mm=h, area_quotient_mm=(cad.area-original.area)/h,
            geometric_error=d.geometry_delta(exported, expected), constraints_phi_sync=True))
    cad, phi, _ = synchronized_state(original)
    for label, corrupted in [('sign', -phi), ('interface', phi+.001)]:
        try:
            validate_phi_sync(cad, corrupted)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{label} mismatch was silently accepted')
    try:
        normal_transport(Polygon([[0, 0], [1, 0], [1, 1], [0, 1]]),
            lambda xy, n: -np.ones(len(xy)), .5)
    except ValueError:
        pass
    else:
        raise AssertionError('Collapsed topology was silently accepted')
    result = dict(completed=True, scope='offline retained-CAD prototype; not integrated in optimizer',
        geometry_cases=rows, feed_fixed_cases=contact_rows, rejection_checks=3,
        limitations=['No topology creation or deletion',
            'Production HJ velocity-to-CAD coupling not integrated',
            'General morphology constraints require explicit CAD projection; incompatible phi is rejected'])
    (OUT/'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f'{len(rows)} geometry cases, {len(contact_rows)} feed-fixed cases, 3 rejection checks passed.')


if __name__ == '__main__':
    main()
