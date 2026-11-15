"""Independent Maxwell/CAD directional check; saves failed gates as evidence."""
from pathlib import Path
import hashlib
import json
import time

import numpy as np
from shapely.geometry import Polygon

from pec_polygon_reference import SourceReference, boundary_quadrature, als
from eaopt.geometry.evolution import CadBoundaryMesh

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'results/polygon_smatrix_2026-11-13'


def packed(a):
    return {'real': np.real(a).tolist(), 'imag': np.imag(a).tolist()}


def check_taylor():
    """Actual CAD Taylor remainders at fixed, independently refined PDE settings."""
    OUT.mkdir(parents=True, exist_ok=True)
    theta = 2*np.pi*np.arange(64)/64
    geometry = Polygon(10*np.column_stack((np.cos(theta), np.sin(theta))))
    mesh = CadBoundaryMesh.from_geometry(geometry, 2.)
    velocity = mesh.vertices/10*(1+.4*np.cos(3*theta))[:, None]
    sol = SourceReference(geometry, cluster_count=9).solve(fit_order=12, check_order=15)
    p, n, w, t = boundary_quadrature(geometry, 16, graded=True, with_parameter=True)
    repeat = len(p)//len(mesh.edges)
    va = np.repeat(velocity[mesh.edges[:, 0]], repeat, axis=0)
    vb = np.repeat(velocity[mesh.edges[:, 1]], repeat, axis=0)
    vn = np.sum(((1-t[:, None])*va+t[:, None]*vb)*n, axis=1)
    pred = np.sum(sol.all_densities(p, n)*(w*vn)[:, None, None], axis=0)
    abs_pred = np.real(np.conj(sol.scattering)/abs(sol.scattering)*pred)
    report = dict(clusters=9, fit_order=12, gradient_order=16, scattering=packed(sol.scattering),
                  prediction=packed(pred), abs_prediction=abs_pred.tolist(),
                  residual=sol.residual, power_error=sol.power_error, tests=[],
                  full_goal_achieved=False)
    for step in (.04, .02, .01, .005):
        trials = [SourceReference(mesh.moved(velocity, sign*step), cluster_count=9).solve(
            fit_order=12, check_order=15) for sign in (1., -1.)]
        plus, minus = [s.scattering for s in trials]
        fd, abs_fd = (plus-minus)/(2*step), (abs(plus)-abs(minus))/(2*step)
        report['tests'].append(dict(step_mm=step, fd=packed(fd),
            entry_complex_relative_error=(abs(pred-fd)/abs(fd)).tolist(),
            abs_relative_error=(abs(abs_pred-abs_fd)/abs(abs_fd)).tolist(),
            complex_remainder=abs(plus-sol.scattering-step*pred).tolist(),
            abs_remainder=abs(abs(plus)-abs(sol.scattering)-step*abs_pred).tolist(),
            trial_residuals=[s.residual for s in trials],
            trial_reciprocity_errors=[s.reciprocity_error for s in trials],
            trial_power_errors=[s.power_error for s in trials]))
        (OUT/'taylor.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report['tests'][-1]), flush=True)


def refine_quadrature():
    """Separate PDE fit quadrature error from source-count refinement."""
    OUT.mkdir(parents=True, exist_ok=True)
    theta = 2*np.pi*np.arange(64)/64
    geometry = Polygon(10*np.column_stack((np.cos(theta), np.sin(theta))))
    mesh = CadBoundaryMesh.from_geometry(geometry, 2.)
    velocity = mesh.vertices/10*(1+.4*np.cos(3*theta))[:, None]
    step = .02
    rows = []
    for fit_order in (8, 12):
        solutions = [SourceReference(mesh.moved(velocity, h), cluster_count=7).solve(
            fit_order=fit_order, check_order=fit_order+3) for h in (0., step, -step)]
        sol, plus, minus = solutions
        fd = (plus.scattering-minus.scattering)/(2*step)
        abs_fd = (abs(plus.scattering)-abs(minus.scattering))/(2*step)
        predictions = []
        for order in (8, 12, 16):
            p, n, w, t = boundary_quadrature(geometry, order, graded=True, with_parameter=True)
            repeat = len(p)//len(mesh.edges)
            va = np.repeat(velocity[mesh.edges[:, 0]], repeat, axis=0)
            vb = np.repeat(velocity[mesh.edges[:, 1]], repeat, axis=0)
            vn = np.sum(((1-t[:, None])*va+t[:, None]*vb)*n, axis=1)
            pred = np.sum(sol.all_densities(p, n)*(w*vn)[:, None, None], axis=0)
            abs_pred = np.real(np.conj(sol.scattering)/abs(sol.scattering)*pred)
            predictions.append(dict(order=order, prediction=packed(pred),
                complex_relative_error=float(np.linalg.norm(pred-fd)/np.linalg.norm(fd)),
                entry_complex_relative_error=(abs(pred-fd)/abs(fd)).tolist(),
                abs_relative_error=(abs(abs_pred-abs_fd)/abs(abs_fd)).tolist()))
        rows.append(dict(fit_order=fit_order, step_mm=step, fd=packed(fd), predictions=predictions,
            residuals=[s.residual for s in solutions], power_errors=[s.power_error for s in solutions],
            reciprocity_errors=[s.reciprocity_error for s in solutions],
            conditions=[s.condition_number for s in solutions]))
        (OUT/'quadrature_refinement.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
        print(json.dumps(rows[-1]), flush=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    theta = 2*np.pi*np.arange(64)/64
    geometry = Polygon(10*np.column_stack((np.cos(theta), np.sin(theta))))
    mesh = CadBoundaryMesh.from_geometry(geometry, 2.)
    directions = {'dilation': mesh.vertices/10,
                  'nonuniform': mesh.vertices/10*(1+.4*np.cos(3*theta))[:, None]}
    rows = []
    for count in (5, 7, 9):
        start = time.perf_counter()
        sol = SourceReference(geometry, cluster_count=count).solve()
        row = dict(clusters=count, residual=sol.residual, power_error=sol.power_error,
                   reciprocity_error=sol.reciprocity_error, scattering=packed(sol.scattering),
                   rank=sol.rank, unknowns=sol.unknowns, directions=[])
        # Independent quadrature orders expose unresolved corner integration.
        quadratures = []
        for order, graded in ((2, False), (5, True), (8, True)):
            p, n, w, t = boundary_quadrature(geometry, order, graded=graded, with_parameter=True)
            density = sol.all_densities(p, n)
            quadratures.append((order, graded, p, n, w, t, density))
        for name, velocity in directions.items():
            predictions = []
            for order, graded, p, n, w, t, density in quadratures:
                repeat = len(p)//len(mesh.edges)
                va = np.repeat(velocity[mesh.edges[:, 0]], repeat, axis=0)
                vb = np.repeat(velocity[mesh.edges[:, 1]], repeat, axis=0)
                vn = np.sum(((1-t[:, None])*va+t[:, None]*vb)*n, axis=1)
                prediction = np.sum(density*(w*vn)[:, None, None], axis=0)
                if order == 8:
                    e, h = sol.fields(p)
                    for m, incident in ((0, 0), (1, 0)):
                        public = als.s_parameter_shape_variation(
                            n, w*1e-3*sol.reference.length, vn*1e-3,
                            e[:, :, incident].T, h[:, :, incident].T,
                            e[:, :, m].T, h[:, :, m].T, sol.reference.omega, 1., 1.)
                        np.testing.assert_allclose(public, prediction[m, incident], rtol=2e-12, atol=1e-14)
                predictions.append(prediction)
            tests = []
            for step in (.04, .02, .01):
                plus = SourceReference(mesh.moved(velocity, step), cluster_count=count).solve()
                minus = SourceReference(mesh.moved(velocity, -step), cluster_count=count).solve()
                fd = (plus.scattering-minus.scattering)/(2*step)
                abs_fd = (abs(plus.scattering)-abs(minus.scattering))/(2*step)
                abs_pred = np.real(np.conj(sol.scattering)/abs(sol.scattering)*predictions[-1])
                tests.append(dict(step_mm=step, fd=packed(fd), abs_fd=abs_fd.tolist(),
                    abs_prediction=abs_pred.tolist(),
                    entry_complex_relative_error=(abs(predictions[-1]-fd)/abs(fd)).tolist(),
                    abs_relative_error=(abs(abs_pred-abs_fd)/abs(abs_fd)).tolist(),
                    complex_relative_error_by_quadrature=[float(np.linalg.norm(v-fd)/np.linalg.norm(fd)) for v in predictions],
                    trial_max_residual=max(plus.residual, minus.residual),
                    trial_max_power_error=max(plus.power_error, minus.power_error)))
            row['directions'].append(dict(name=name, predictions=[packed(v) for v in predictions], tests=tests))
        row['seconds'] = time.perf_counter()-start
        rows.append(row)
        report = dict(scope='64-sided PEC cylinder, fixed cylindrical modes, 1 W peak phasor normalization',
            production_modified=False, full_goal_achieved=False, cases=rows,
            source_hashes={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                          ('scripts/check_polygon_smatrix.py', 'scripts/pec_polygon_reference.py',
                           'eaopt/geometry/evolution.py', 'adjoint_levelset.py')})
        (OUT/'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'clusters': count, 'residual': sol.residual, 'power_error': sol.power_error,
            'directions': [{'name': d['name'], 'last': d['tests'][-1]} for d in row['directions']],
            'seconds': row['seconds']}), flush=True)


if __name__ == '__main__':
    main()
