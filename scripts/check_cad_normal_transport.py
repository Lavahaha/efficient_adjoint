"""Offline CAD transport reference; does not replace level-set topology updates."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from shapely.affinity import affine_transform, rotate, translate
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/antenna_corner_analysis_2026-11-10'


def normal_transport(polygon, normal_speed, dt):
    """Intersect the normal displacement conditions of each incident edge.

    normal_speed(xy, normals) returns normal velocities at vertices, approached
    from the corresponding incident edge. Corners retain both edge normals.
    A scalar continuous speed can ignore the normals argument.
    """
    if not polygon.is_valid or polygon.area <= 0 or not np.isfinite(dt):
        raise ValueError('A valid nonempty polygon and finite dt are required')
    polygon = orient(polygon, sign=1.)
    moved = []
    for ring in (polygon.exterior, *polygon.interiors):
        xy = np.asarray(ring.coords)[:-1]
        tangent = np.roll(xy, -1, axis=0) - xy
        length = np.linalg.norm(tangent, axis=1)
        if np.any(length <= 1e-12):
            raise ValueError('Zero-length CAD edges have no normal')
        outgoing = np.column_stack((tangent[:, 1], -tangent[:, 0])) / length[:, None]
        incoming = np.roll(outgoing, 1, axis=0)
        speeds = [np.asarray(normal_speed(xy, normals), float)
                  for normals in (incoming, outgoing)]
        if any(v.shape != (len(xy),) or not np.isfinite(v).all() for v in speeds):
            raise ValueError('Normal speeds must be finite arrays of shape (N,)')
        displacement = []
        for first, second, a, b in zip(incoming, outgoing, *speeds):
            matrix = np.vstack((first, second))
            velocity = np.linalg.lstsq(matrix, np.array([a, b]), rcond=1e-12)[0]
            if np.linalg.norm(matrix @ velocity - [a, b]) > 1e-9 * max(1., abs(a), abs(b)):
                raise ValueError('Incident normals prescribe incompatible corner motion')
            displacement.append(dt * velocity)
        moved.append(xy + displacement)
    result = Polygon(moved[0], moved[1:])
    if not result.is_valid or result.area <= 0:
        raise ValueError('CAD transport changes topology or creates an invalid polygon')
    return result


def cases():
    rectangle = Polygon([[-10., -10.], [10., -10.], [10., 10.], [-10., 10.]])
    frame = Polygon([[-3., -2.], [3., -2.], [3., 2.], [-3., 2.]],
                    [[[-1., -.5], [-1., .5], [1., .5], [1., -.5]]])
    return {
        'aligned_rectangle': rectangle,
        'translated_rectangle': translate(rectangle, .037, .021),
        'rotated17_rectangle': rotate(rectangle, 17),
        'rotated45_rectangle': rotate(translate(rectangle, .037, .021), 45),
        'concave_L': Polygon([[-2., -2.], [2., -2.], [2., 0.], [0., 0.], [0., 2.], [-2., 2.]]),
        'frame_with_hole': frame,
        'rotated_frame_with_hole': rotate(translate(frame, .037, .021), 17),
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, polygon in cases().items():
        zero = normal_transport(polygon, lambda xy, n: np.zeros(len(xy)), 1.)
        assert zero.symmetric_difference(polygon).area < 1e-12
        for h in (.01, .001, .0001, .000001):
            actual = normal_transport(polygon, lambda xy, n: -np.ones(len(xy)), h)
            expected = polygon.buffer(-h, join_style=2)
            error = actual.boundary.hausdorff_distance(expected.boundary)
            quotient = (actual.area - polygon.area) / h
            assert error < 1e-10, (name, h, error)
            # For polygons, the area Taylor remainder is quadratic in h.
            assert abs(quotient + polygon.length) < 100 * h + 1e-6, (name, h, quotient)
            rows.append(dict(case=name, h_mm=h, geometry_error_mm=error,
                             area_quotient_mm=quotient, expected_area_derivative_mm=-polygon.length))
        matrix = np.array([[.1, .03], [-.02, .2]])
        offset = np.array([.04, -.05])
        speed = lambda xy, n: np.sum((xy @ matrix.T + offset) * n, axis=1)
        h = .001
        actual = normal_transport(polygon, speed, h)
        m = np.eye(2) + h * matrix
        expected = affine_transform(polygon, [m[0, 0], m[0, 1], m[1, 0], m[1, 1], *(h*offset)])
        error = actual.boundary.hausdorff_distance(expected.boundary)
        assert error < 1e-10, (name, error)
        rows.append(dict(case=name, direction='affine', h_mm=h, geometry_error_mm=error))
    # The local field supplies two different incident normal speeds at a corner.
    sample = cases()['aligned_rectangle']
    try:
        normal_transport(sample, lambda xy, n: np.full(len(xy), np.nan), .01)
    except ValueError:
        pass
    else:
        raise AssertionError('Nonfinite speeds must fail before constructing CAD')
    report = dict(scope='independent explicit-CAD reference, no production optimizer changes',
                  step_control_modified=False, checks_passed=len(rows), cases=rows)
    (OUT / 'normal_transport.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'{len(rows)} CAD transport checks passed across {len(cases())} shapes.')
    print('Maximum geometry error:', max(row['geometry_error_mm'] for row in rows), 'mm')


if __name__ == '__main__':
    main()
