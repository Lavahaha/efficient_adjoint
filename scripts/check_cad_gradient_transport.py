"""Independent CAD displacement/shape-gradient coupling, not a runner replacement.

The production HJ/constraint pipeline remains unchanged. This reference uses
piecewise linear vector motion on retained CAD; it rejects topology events.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_cad_normal_transport import cases

OUT = ROOT / 'results/cad_gradient_transport_2026-11-13'


from eaopt.geometry.evolution import CadBoundaryMesh as CadMesh


def test_shapes():
    shapes = cases()
    shapes['two_components'] = unary_union([
        Polygon([[-3, -1], [-1, -1], [-1, 1], [-3, 1]]),
        Polygon([[1, -1], [3, -1], [3, 1], [1, 1]])])
    return shapes


def objective(geometry, coefficients):
    """Exact integral of 1 + a*x + b*y over the polygonal metal."""
    a, b = coefficients
    return float(geometry.area*(1+a*geometry.centroid.x+b*geometry.centroid.y))


def rejection_checks():
    square = Polygon([[0, 0], [1, 0], [1, 1], [0, 1]])
    mesh = CadMesh.from_geometry(square, 2.)
    invalid = [
        lambda: CadMesh.from_geometry(square, 0.),
        lambda: mesh.gradient_velocity(np.full(8, np.nan)),
        lambda: mesh.gradient_velocity(np.ones(8), np.full(4, -1.)),
        lambda: mesh.moved(np.zeros((3, 2)), .01),
        lambda: mesh.moved(np.full((4, 2), np.nan), .01),
        lambda: mesh.moved(-mesh.vertices, 1.),
        lambda: mesh.moved(np.column_stack((-2*mesh.vertices[:, 0], np.zeros(4))), 1.),
    ]
    pair = unary_union([square, Polygon([[2, 0], [3, 0], [3, 1], [2, 1]])])
    pair_mesh = CadMesh.from_geometry(pair, 2.)
    velocity = np.zeros_like(pair_mesh.vertices)
    velocity[pair_mesh.vertices[:, 0] > 1, 0] = -1.5
    invalid.append(lambda: pair_mesh.moved(velocity, 1.))
    for operation in invalid:
        try:
            operation()
        except ValueError:
            continue
        raise AssertionError('Invalid CAD input/update was silently accepted')
    return len(invalid)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    max_fd, max_zero, min_ratio = 0., 0., np.inf
    for name, original in test_shapes().items():
        mesh = CadMesh.from_geometry(original, .4)
        points, _, weights, _ = mesh.quadrature()
        zero = mesh.moved(np.zeros_like(mesh.vertices), 0.)
        error = zero.boundary.hausdorff_distance(original.boundary)
        max_zero = max(max_zero, error)
        assert error < 1e-12
        for coefficients in ((0., 0.), (.07, -.04)):
            density = 1+points@np.asarray(coefficients)
            # Freeze a complete leftmost boundary edge, including its corners.
            free = (mesh.vertices[:, 0] > original.bounds[0]+1e-10).astype(float)
            for label, mobility in (('free', None), ('fixed_left', free)):
                velocity = mesh.gradient_velocity(density, mobility)
                predicted = float(np.sum(weights*density*mesh.normal_velocity(velocity)))
                assert predicted > 0
                if mobility is not None:
                    np.testing.assert_array_equal(velocity[mobility == 0], 0.)
                np.testing.assert_allclose(mesh.gradient_velocity(-density, mobility), -velocity,
                                           rtol=0, atol=0)
                base = objective(original, coefficients)
                scan = []
                for h in (.002, .001, .0005, 1e-6, -1e-6):
                    moved = mesh.moved(velocity, h)
                    delta = objective(moved, coefficients)-base
                    remainder = abs(delta-h*predicted)
                    scan.append(dict(h=h, actual_delta=delta, predicted_delta=h*predicted,
                        taylor_remainder=remainder,
                        displacement_mm=moved.boundary.hausdorff_distance(original.boundary)))
                # A frame can have exactly cancelling outer/hole quadratic
                # terms. Do not estimate an order from roundoff in that case.
                roundoff = 100*np.finfo(float).eps*max(1., abs(base))
                if scan[0]['taylor_remainder'] > 100*roundoff:
                    for first, second in zip(scan[:2], scan[1:3]):
                        ratio = first['taylor_remainder']/second['taylor_remainder']
                        assert 3.8 < ratio < 4.2, (name, label, coefficients, ratio)
                        min_ratio = min(min_ratio, ratio)
                else:
                    assert max(r['taylor_remainder'] for r in scan[:3]) < 100*roundoff
                fd = (scan[3]['actual_delta']-scan[4]['actual_delta'])/2e-6
                fd_error = abs(fd-predicted)/abs(predicted)
                max_fd = max(max_fd, fd_error)
                assert fd_error < 1e-7, (name, fd_error)
                assert scan[3]['actual_delta'] > 0 and scan[4]['actual_delta'] < 0
                assert scan[3]['displacement_mm'] < 5e-6
                rows.append(dict(case=name, coefficients=coefficients, mobility=label,
                    predicted_derivative=predicted, central_fd_relative_error=fd_error, scan=scan))
    # Arbitrary vector flow gives a directly known rectangle length map.
    original = Polygon([[-20, -10], [0, -10], [0, 10], [-20, 10]])
    mesh = CadMesh.from_geometry(original, .4)
    velocity = np.column_stack(((mesh.vertices[:, 0]+20)/20, np.zeros(len(mesh.vertices))))
    _, _, weights, _ = mesh.quadrature()
    predicted = float(np.sum(weights*mesh.normal_velocity(velocity)))
    assert abs(predicted-20) < 1e-12
    length_rows = []
    for h in (.08, .01, .001, 1e-6, -.08, -.01, -.001, -1e-6):
        moved = mesh.moved(velocity, h)
        expected = Polygon([[-20, -10], [h, -10], [h, 10], [-20, 10]])
        error = moved.boundary.hausdorff_distance(expected.boundary)
        quotient = (moved.area-original.area)/h
        assert error < 1e-12 and abs(quotient-20) < 1e-6
        length_rows.append(dict(h_mm=h, hausdorff_mm=error, area_quotient_mm=quotient))
    # Repeated retained-CAD updates must not rebuild via a lossy phi contour.
    repeat = original
    for _ in range(20):
        mesh = CadMesh.from_geometry(repeat, .4)
        velocity = np.column_stack((np.ones(len(mesh.vertices)), np.zeros(len(mesh.vertices))))
        repeat = mesh.moved(velocity, 1e-5)
    from shapely.affinity import translate
    repeated_error = repeat.boundary.hausdorff_distance(translate(original, xoff=2e-4).boundary)
    assert repeated_error < 1e-11
    # The adjoint construction must also hold for arbitrary signed densities,
    # independent of the smooth geometric objectives used for Taylor tests.
    rng = np.random.default_rng(351)
    worst_signed_work = np.inf
    fixed_edge_error = 0.
    for original in test_shapes().values():
        mesh = CadMesh.from_geometry(original, .4)
        _, _, weights, _ = mesh.quadrature()
        mobility = (rng.uniform(size=len(mesh.vertices)) > .3).astype(float)
        for _ in range(20):
            density = rng.normal(size=len(weights))
            velocity = mesh.gradient_velocity(density, mobility)
            work = float(np.sum(weights*density*mesh.normal_velocity(velocity)))
            worst_signed_work = min(worst_signed_work, work)
            assert work >= -1e-12
    # A finite displacement preserves the entire feed edge, not only its nodes.
    original = Polygon([[-20, -10], [0, -10], [0, 10], [-20, 10]])
    mesh = CadMesh.from_geometry(original, .4)
    _, _, weights, _ = mesh.quadrature()
    mobility = (mesh.vertices[:, 0] > -20+1e-10).astype(float)
    velocity = mesh.gradient_velocity(np.ones(len(weights)), mobility)
    moved = mesh.moved(velocity, .001)
    from shapely.geometry import LineString
    contact = LineString([[-20, -10], [-20, 10]])
    fixed_edge_error = contact.difference(moved.boundary).length
    assert fixed_edge_error < 1e-12
    rejections = rejection_checks()
    result = dict(passed=True, scope='retained CAD/vector displacement reference only',
        production_geometry_changed=False, full_goal_achieved=False,
        max_zero_update_error_mm=max_zero, max_fd_relative_error=max_fd,
        minimum_taylor_halving_ratio=min_ratio, repeated_update_error_mm=repeated_error,
        signed_density_checks=160, minimum_signed_density_work=worst_signed_work,
        fixed_edge_missing_length_mm=fixed_edge_error, rejection_checks=rejections,
        cases=rows, antenna_length=length_rows,
        remaining=['No coupling to production HJ or morphology/connectivity projections',
            'No creation/deletion/merging of topology; invalid events are rejected',
            'No new EM test or optimization convergence certification',
            'Production CAD point-budget/remeshing and field-gradient adaptation remain open'])
    (OUT/'results.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key not in ('cases', 'antenna_length')}, indent=2))


if __name__ == '__main__':
    main()
