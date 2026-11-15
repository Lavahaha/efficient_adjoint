"""CST/analytic S11 reference: fixed lossy coax layer and movable PEC short."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als

FREQ = 5.
OMEGA = 2*np.pi*FREQ*1e9
K0 = OMEGA*np.sqrt(als.EPS0*als.MU0)
Z0 = np.sqrt(als.MU0/als.EPS0)*np.log(3)/(2*np.pi)
OUT = ROOT/'results/coax_shape_reference_2026-11-12/loss_enabled'


def pair(z):
    return [float(np.real(z)), float(np.imag(z))]


def write(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def analytic(h_mm, power=.5):
    def line(length, eps_r):
        k, z = K0*np.sqrt(eps_r), Z0/np.sqrt(eps_r)
        c, s = np.cos(k*length), np.sin(k*length)
        return np.array([[c, 1j*z*s], [1j*s/z, c]])
    fixed = line(.010, 1)@line(.008, 4*(1-.1j))
    length = .012-h_mm*1e-3
    c, s = np.cos(K0*length), np.sin(K0*length)
    matrix = fixed@line(length, 1)
    # h is metal growth: the final air line gets shorter.
    derivative = -fixed@(K0*np.array([[-s, 1j*Z0*c], [1j*c/Z0, -s]]))
    b, d = matrix[:, 1]
    db, dd = derivative[:, 1]
    sm = (b-Z0*d)/(b+Z0*d)
    ds = ((db-Z0*dd)*(b+Z0*d)-(b-Z0*d)*(db+Z0*dd))/(b+Z0*d)**2
    current = 2*np.sqrt(2*power*Z0)/(b+Z0*d)
    q = als.MU0*current**2*np.log(3)/(2*np.pi)
    np.testing.assert_allclose(ds, -1j*OMEGA/(4*power)*q, rtol=1e-12, atol=1e-9)
    return sm, ds, current


def self_check():
    for h in (0., -.1, .1):
        s, ds, _ = analytic(h)
        fd = (analytic(h+1e-4)[0]-analytic(h-1e-4)[0])/(2e-7)
        np.testing.assert_allclose(fd, ds, rtol=1e-8)
        assert abs(np.real(s.conjugate()/abs(s)*ds)) > 1
        for phase in (0., .37, -.81):
            incoming = np.sqrt(Z0)*np.exp(1j*phase)
            z = .005
            v = incoming*(np.exp(-1j*K0*z)+s*np.exp(1j*K0*z))
            i = incoming/Z0*(np.exp(-1j*K0*z)-s*np.exp(1j*K0*z))
            reconstructed, a = port_projection(v, i, z)
            np.testing.assert_allclose(reconstructed, s, atol=1e-14)
            np.testing.assert_allclose(a, np.sqrt(.5)*np.exp(1j*phase), atol=1e-14)
    print('PASS analytic ABCD derivative, PEC integral and nonzero amplitude derivative', flush=True)


def port_projection(voltage, current, z_m):
    vplus, vminus = (voltage+Z0*current)/2, (voltage-Z0*current)/2
    a = vplus*np.exp(1j*K0*z_m)/np.sqrt(2*Z0)
    # b(z)/a(z)=S(0)*exp(+2jkz) for exp(+jwt), so reverse this phase.
    return vminus/vplus*np.exp(-2j*K0*z_m), a


def build(project, h, step):
    from eaopt.solver import cst_model as m
    from eaopt.solver.cst_model_antenna import _cylinder
    front = 30-h
    assert 18 < front < 31
    sigma = OMEGA*als.EPS0*4*.1
    material = m.material_normal('lossy_reference', 4, 1, 0.).replace(
        '    .Create', f'    .Sigma "{sigma:.16g}"\n    .TanDGiven "False"\n    .Create')
    blocks = [m.UNITS_BLOCK, m.MESH_CREATOR, material,
              _cylinder('inner', 'reference', 'PEC', 0, 0, 1, 0, 31),
              _cylinder('outer', 'reference', 'PEC', 0, 0, 3.5, 0, 31, inner_radius=3),
              _cylinder('short', 'reference', 'PEC', 0, 0, 3, front, 31, inner_radius=1),
              _cylinder('input_air', 'reference', 'Vacuum', 0, 0, 3, 0, 10, inner_radius=1),
              _cylinder('dielectric', 'reference', 'lossy_reference', 0, 0, 3, 10, 18, inner_radius=1),
              _cylinder('spacer_air', 'reference', 'Vacuum', 0, 0, 3, 18, front, inner_radius=1),
              m.waveguide_port(1, 'TEM_reference', 'zmin', 0, -3.5, 3.5, -3.5, 3.5),
              m.set_boundaries(*(['electric']*6)),
              m.frequency_domain_solver_setup(1, FREQ).replace('"1e-6"', '"1e-8"').replace('.MaxCPUs "2"', '.MaxCPUs "1"'),
              'With FDSolver\n .Type "Direct"\n .SetAllowFloatDirectSolver "False"\nEnd With\n',
              m.field_monitor('Efield', FREQ), m.field_monitor('Hfield', FREQ)]
    for name in ('input_air', 'dielectric', 'spacer_air', 'short'):
        blocks.append(f'Solid.SetMeshStepwidthTet "reference:{name}", "{step:g}"\n')
    for code in blocks:
        project.model3d.add_to_history(m.block_header(code), code)


def sample(project, folder, power, s):
    from eaopt.solver import cst_results as r
    nodes, radial_weights = np.polynomial.legendre.leggauss(20)
    radius = 2+nodes
    theta = 2*np.pi*(np.arange(48)+.5)/48
    rr, tt = np.meshgrid(radius, theta, indexing='ij')
    x, y = (rr*np.cos(tt)).ravel(), (rr*np.sin(tt)).ravel()
    zvalues = [5., 29.95, 29.975, 29.9875]
    points = np.concatenate([np.column_stack((x, y, np.full(len(x), z))) for z in zvalues])
    e, magnetic = [r.export_field_points(project.model3d, kind, FREQ, points,
                    folder/f'{kind}.txt', stimulus_port=1) for kind in ('Efield', 'Hfield')]
    np.savez_compressed(folder/'fields.npz', points_mm=points, E=e, H=magnetic)
    e = e.reshape(len(zvalues), len(radius), len(theta), 3)
    magnetic = magnetic.reshape(e.shape)
    er = e[0, :, :, 0]*np.cos(theta)+e[0, :, :, 1]*np.sin(theta)
    hphi = -magnetic[0, :, :, 0]*np.sin(theta)+magnetic[0, :, :, 1]*np.cos(theta)
    voltage = np.sum(np.mean(er, axis=1)*radial_weights)*1e-3
    current = np.mean(hphi*rr)*2*np.pi*1e-3
    reconstructed_s, a = port_projection(voltage, current, .005)
    area = radial_weights[:, None]*rr*(2*np.pi/len(theta))*1e-6
    predictions = []
    for i, z in enumerate(zvalues[1:], 1):
        qe = np.sum(als.EPS0*e[i, :, :, 2]**2*area)
        qh = np.sum(als.MU0*(magnetic[i, :, :, 0]**2+magnetic[i, :, :, 1]**2)*area)
        default = -1j*OMEGA*(qe+qh)/(4*power)*1e-3
        measured = -1j*OMEGA*(qe+qh)/(4*a*a)*1e-3
        predictions.append(dict(offset_mm=30-z, complex_per_mm=pair(default),
                                abs_per_mm=float(np.real(s.conjugate()/abs(s)*default)),
                                incident_projection_complex_per_mm=pair(measured),
                                incident_projection_abs_per_mm=float(np.real(s.conjugate()/abs(s)*measured)),
                                electric_integral=pair(qe), magnetic_integral=pair(qh)))
    return dict(incident_amplitude_sqrt_w=pair(a), reconstructed_incident_power_w=abs(a)**2,
                reconstructed_s11=pair(reconstructed_s), s11_reconstruction_error=abs(reconstructed_s-s),
                predictions=predictions, voltage_at_z5mm=pair(voltage), current_at_z5mm=pair(current))


def native_geometry(project, folder, h):
    path = folder/'geometry_native.txt'
    project.model3d._execute_vba_code(f'''Sub Main
Dim xmin As Double, xmax As Double, ymin As Double, ymax As Double, zmin As Double, zmax As Double
Dim ok As Boolean
Open "{path}" For Output As #1
Print #1, Solid.GetVolume("reference:short")
ok = Solid.GetLooseBoundingBoxOfShape("reference:short", xmin, xmax, ymin, ymax, zmin, zmax)
If Not ok Then Err.Raise 1, , "Short boundary unavailable"
Print #1, zmin
Print #1, zmax
Close #1
End Sub''')
    values = [float(v) for v in path.read_text().splitlines()]
    np.testing.assert_allclose(values[1:], [30-h, 31], atol=1e-9, rtol=0)
    expected_volume = np.pi*8*(1+h)
    # CST's curved-solid volume measurement differs from pi*r^2*l by 0.26 ppm.
    np.testing.assert_allclose(values[0], expected_volume, atol=1e-8, rtol=1e-6)
    return dict(short_volume_mm3=values[0], short_zmin_mm=values[1], short_zmax_mm=values[2],
                volume_relative_error=abs(values[0]-expected_volume)/expected_volume)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, default=39084)
    parser.add_argument('--step', type=float, nargs='+', default=[.6, .4])
    parser.add_argument('--h', type=float, nargs='+', default=[0.])
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--resume', action='store_true', help='Resume this diagnostic from its retained CST projects')
    args = parser.parse_args()
    self_check()
    if args.self_check:
        return
    import cst.interface
    from eaopt.solver import cst_results as r
    from eaopt.solver.cst import _run_solver, _save, _clear_results
    from check_antenna_field_sampling import read_slim
    OUT.mkdir(parents=True, exist_ok=True)
    destination = OUT/'results.json'
    report = json.loads(destination.read_text()) if destination.exists() else dict(evaluations=[])
    report.update(analytic_port_impedance_ohm=Z0, geometry='r_inner=1,r_outer=3; air 0..10, dielectric 10..18, air 18..30-h mm',
                  dielectric='eps_r=4, sigma=omega*eps0*4*0.1; loss_tangent=0.1 at 5GHz',
                  conductivity_s_per_m=OMEGA*als.EPS0*4*.1, completed=False)
    de = cst.interface.DesignEnvironment.connect(args.pid)
    assert not de.list_open_projects(), 'Keep unrelated projects untouched'
    quiet = de.in_quiet_mode()
    project = None
    de.set_quiet_mode(True)
    try:
        for step in args.step:
            for h in args.h:
                label = f'mesh{step:g}_h{h:+g}'.replace('+', 'plus').replace('-', 'minus')
                if any(row['label'] == label for row in report['evaluations']):
                    continue
                folder = OUT/label
                folder.mkdir(exist_ok=True)
                path = folder/'reference.cst'
                if path.exists():
                    assert args.resume, f'Incomplete project retained for inspection; use --resume: {path}'
                    project = de.open_project(str(path))
                else:
                    project = de.new_mws()
                    build(project, h, step)
                audit = native_geometry(project, folder, h)
                if args.resume:
                    assert _clear_results(project.model3d, what='resume coax reference', log=print)
                _save(project, path, print)
                seconds = _run_solver(project.model3d, print)
                _save(project, path, print)
                assert project.model3d.get_solver_run_info()['state'] == 'SUCCESS'
                s = r.read_s_params(path, 1, response_ports=(1,), freq_ghz=FREQ, exact_frequency=True)[1, 1]
                power = r.read_incident_power(path, 1, FREQ)
                exact, ds, current = analytic(h, power)
                mesh = read_slim(path.with_suffix('')/'Result/3d.slim')[2]
                row = dict(label=label, step_mm=step, h_mm=h, project_path=str(path), seconds=seconds,
                           s11=pair(s), magnitude=abs(s), incident_power_w=power, mesh=mesh,
                           analytic_s11=pair(exact), complex_s_error=abs(s-exact),
                           analytic_complex_slope_per_mm=pair(ds*1e-3),
                           analytic_abs_slope_per_mm=float(np.real(exact.conjugate()/abs(exact)*ds)*1e-3),
                           analytic_short_current=pair(current), native_geometry=audit)
                if h == 0:
                    row['fields'] = sample(project, folder, power, s)
                report['evaluations'].append(row)
                write(destination, report)
                print('CASE_DONE', label, 'S error', row['complex_s_error'], flush=True)
                project.close()
                project = None
        report['completed'] = True
        report.pop('error', None)
    except Exception as error:
        report['error'] = repr(error)
        raise
    finally:
        if project is not None:
            project.close()
        de.set_quiet_mode(quiet)
        report['final_state'] = dict(pid=args.pid, projects=de.list_open_projects(), quiet=de.in_quiet_mode())
        write(destination, report)


if __name__ == '__main__':
    main()
