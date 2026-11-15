"""Independent Maxwell reference for the PEC kernel and all modal S entries.

An infinite PEC cylinder has exact cylindrical-wave scattering coefficients.
A fixed orthogonal mode mixer produces nonzero reflection/transmission amplitude
derivatives. No CST data or fitted coefficients enter this reference.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.special import hankel1, hankel2, h1vp, h2vp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als


def scattering(radius, omega, orders, mixer):
    k = omega * np.sqrt(als.EPS0 * als.MU0)
    z = k * radius
    incoming = h1vp(orders, z)
    outgoing = h2vp(orders, z)
    # Outgoing H has a minus sign so incoming/outgoing E have the same
    # reciprocal port orientation. Omitting it reverses the reported S phase.
    s = incoming / outgoing
    ds = k * (h1vp(orders, z, 2) * outgoing - incoming * h2vp(orders, z, 2)) / outgoing**2
    return mixer.T @ np.diag(s) @ mixer, mixer.T @ np.diag(ds) @ mixer


def angular_fields(radius, omega, orders, radial, derivative, theta):
    """Unnormalised Hz, Er, Etheta of independent angular modes (mode, point)."""
    order = orders[:, None]
    hz = radial[:, None] * np.cos(order * theta)
    er = 1j * order / (omega * als.EPS0 * radius) * radial[:, None] * np.sin(order * theta)
    et = 1j / (omega * als.EPS0) * derivative[:, None] * np.cos(order * theta)
    return hz, er, et


def fields(radius, omega, orders, mixer, powers, phases, length, theta, *, surface=True):
    k = omega * np.sqrt(als.EPS0 * als.MU0)
    z = k * radius
    incoming = angular_fields(radius, omega, orders, hankel1(orders, z), k*h1vp(orders, z), theta)
    outgoing = angular_fields(radius, omega, orders, -hankel2(orders, z), -k*h2vp(orders, z), theta)
    scale = np.sqrt(powers * omega * als.EPS0 / length) * np.exp(1j*phases)
    if not surface:
        return tuple(tuple((mixer.T @ component) * scale[:, None] for component in wave)
                     for wave in (incoming, outgoing))
    s = h1vp(orders, z) / h2vp(orders, z)
    return tuple((mixer.T @ (fi + s[:, None]*fo)) * scale[:, None]
                 for fi, fo in zip(incoming, outgoing))


def matrix_pair(value):
    return np.stack((np.real(value), np.imag(value)), axis=-1).tolist()


def relative(actual, expected):
    return float(np.linalg.norm(actual-expected) / max(np.linalg.norm(expected), 1e-30))


def objective(s, kind):
    if kind == 'imag':
        return s.imag, -1j*np.ones_like(s)
    if kind == 'real':
        return s.real, np.ones_like(s)
    if kind == 'abs':
        return abs(s), s.conj()/abs(s)
    if kind == 'abs2':
        return abs(s)**2, 2*s.conj()
    if kind == 'db':
        return 20*np.log10(abs(s)), 20/np.log(10)*s.conj()/abs(s)**2
    raise ValueError(kind)


def validate_case(name, radius, frequency, orders, mixer, powers, phases, length=.01):
    orders = np.asarray(orders)
    powers, phases = np.asarray(powers), np.asarray(phases)
    omega = 2*np.pi*frequency
    count, size = 1024, len(orders)
    theta = 2*np.pi*(np.arange(count)+.5)/count
    np.testing.assert_allclose(mixer.T@mixer, np.eye(size), atol=1e-14)
    assert len(set(orders)) == size and np.all(orders >= 1)
    assert np.all(powers > 0) and radius > 0 and frequency > 0
    s, ds = scattering(radius, omega, orders, mixer)
    np.testing.assert_allclose(s.T, s, atol=1e-14)
    np.testing.assert_allclose(s.conj().T@s, np.eye(size), atol=1e-14)
    hz, er, et = fields(radius, omega, orders, mixer, powers, phases, length, theta)
    assert np.max(abs(et)) < 1e-9*max(np.max(abs(er)), 1.)
    normal = np.column_stack((np.cos(theta), np.sin(theta)))
    area_weight = length*radius*2*np.pi/count
    boundary = als.Boundary(radius*normal, normal, np.full(count, area_weight), np.ones(count, bool))
    electric = np.stack((er*np.cos(theta), er*np.sin(theta), np.zeros_like(er)), axis=1)
    magnetic = np.stack((np.zeros_like(hz), np.zeros_like(hz), hz), axis=1)
    denominator = 4*np.sqrt(powers[:, None]*powers[None, :])*np.exp(1j*(phases[:, None]+phases[None, :]))
    ge = -1j*omega/denominator*als.EPS0*(er@er.T)*area_weight
    gh = -1j*omega/denominator*als.MU0*(hz@hz.T)*area_weight
    predicted = np.empty_like(ds)
    for m in range(size):
        for n in range(size):
            kernel = als.shape_derivative_from_fields(
                boundary, electric[n], magnetic[n], electric[m], magnetic[m], -1j*omega/denominator[m, n])
            legacy = np.sum(kernel*boundary.weights)
            predicted[m, n] = als.s_parameter_shape_variation(
                normal, boundary.weights, np.full(count, 1e-6),
                electric[n], magnetic[n], electric[m], magnetic[m], omega,
                np.sqrt(powers[n])*np.exp(1j*phases[n]), np.sqrt(powers[m])*np.exp(1j*phases[m]))/1e-6
            np.testing.assert_allclose(predicted[m, n], legacy, rtol=1e-11, atol=1e-10)
    np.testing.assert_allclose(predicted, ge+gh, rtol=1e-11, atol=1e-10)
    np.testing.assert_allclose(predicted, ds, rtol=1e-10, atol=1e-9)

    # Verify actual power and the bilinear reaction independently at fixed R.
    port_radius = .10
    incoming, outgoing = fields(port_radius, omega, orders, mixer, powers, phases, length, theta, surface=False)
    port_weight = port_radius*length*2*np.pi/count
    measured_in = -.5*np.real(np.sum(incoming[2]*incoming[0].conj(), axis=1))*port_weight
    measured_out = .5*np.real(np.sum(outgoing[2]*outgoing[0].conj(), axis=1))*port_weight
    reaction = (outgoing[2]@incoming[0].T - outgoing[0]@incoming[2].T)*port_weight
    np.testing.assert_allclose(measured_in, powers, rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(measured_out, powers, rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(reaction, np.diag(-4*powers*np.exp(2j*phases)), atol=1e-11)

    rows = []
    for h in radius*np.array([1e-3, 5e-4, 2.5e-4]):
        sp = scattering(radius+h, omega, orders, mixer)[0]
        sm = scattering(radius-h, omega, orders, mixer)[0]
        fd = (sp-sm)/(2*h)
        fds, residuals = {}, {}
        for kind in ('abs',):
            f0, weight = objective(s, kind)
            fp, _ = objective(sp, kind)
            fm, _ = objective(sm, kind)
            grad = np.real(weight*predicted)
            fds[kind] = relative((fp-fm)/(2*h), grad)
            residuals[kind] = float(np.linalg.norm(fp-f0-h*grad))
            for m in range(size):
                for n in range(size):
                    _, c = als.fom_value_and_weight(s[m, n], kind)
                    np.testing.assert_allclose(als.real_gradient(predicted[m, n], c), grad[m, n], atol=1e-9)
        rows.append(dict(h_m=float(h), complex_fd_relative_error=relative(fd, predicted),
                         objective_fd_relative_errors=fds,
                         complex_taylor_residual=float(np.linalg.norm(sp-s-h*predicted)),
                         objective_taylor_residuals=residuals))
    ratios = [rows[i]['complex_taylor_residual']/rows[i+1]['complex_taylor_residual'] for i in range(2)]
    fd_ratios = [rows[i]['complex_fd_relative_error']/rows[i+1]['complex_fd_relative_error'] for i in range(2)]
    assert all(3.8 < x < 4.2 for x in ratios+fd_ratios)
    assert max(rows[-1]['objective_fd_relative_errors'].values()) < 2e-4
    objective_ratios = {kind: [rows[i]['objective_taylor_residuals'][kind]/rows[i+1]['objective_taylor_residuals'][kind]
                              for i in range(2)] for kind in rows[0]['objective_taylor_residuals']}
    assert all(3.7 < x < 4.3 for values in objective_ratios.values() for x in values)

    # Negative controls ensure the exact reference can distinguish these errors.
    wrong_conjugate = -1j*omega/denominator*(als.EPS0*(er.conj()@er.T)+als.MU0*(hz.conj()@hz.T))*area_weight
    wrong = dict(flipped_normal_or_overall_sign=relative(-predicted, ds),
                 factor_two=relative(2*predicted, ds), electric_term_sign=relative(gh-ge, ds),
                 conjugated_field_product=relative(wrong_conjugate, ds),
                 omitted_electric_term=relative(gh, ds), omitted_magnetic_term=relative(ge, ds))
    assert min(wrong.values()) > .01
    return dict(name=name, radius_m=radius, frequency_hz=frequency, length_m=length,
                orders=orders.tolist(), mixer=mixer.tolist(), powers_w=powers.tolist(), source_phases_rad=phases.tolist(),
                S=matrix_pair(s), exact_dS_per_m=matrix_pair(ds), predicted_dS_per_m=matrix_pair(predicted),
                predicted_electric_dS_per_m=matrix_pair(ge), predicted_magnetic_dS_per_m=matrix_pair(gh),
                abs_dS_per_mm=(np.real(s.conj()/abs(s)*ds)*1e-3).tolist(),
                complex_derivative_relative_error=relative(predicted, ds),
                max_entry_relative_error=float(np.max(abs(predicted-ds)/np.maximum(abs(ds), 1e-14))),
                incoming_power_w=measured_in.tolist(), outgoing_power_w=measured_out.tolist(),
                reaction=matrix_pair(reaction), perturbations=rows,
                complex_residual_halving_ratios=ratios, complex_fd_error_halving_ratios=fd_ratios,
                objective_residual_halving_ratios=objective_ratios, negative_controls_relative_errors=wrong)


def perturbed_scattering(radius, h, omega, mixer, max_order, *, check_count=1024):
    """Solve the deformed PEC boundary independently by outgoing multipoles.

    Only cosine modes occur because the boundary and the incident waves are
    even in theta. Column scaling avoids large high-order Hankel values.
    """
    k = omega*np.sqrt(als.EPS0*als.MU0)
    modes = np.arange(max_order+1)
    source_modes = np.array([1, 2])
    norm = np.ones(max_order+1)
    norm[0] = 1/np.sqrt(2)
    scale = hankel2(modes, k*radius)

    def boundary_matrices(theta):
        speed = 1+.25*np.cos(2*theta)+.15*np.cos(3*theta)
        speed_prime = -.5*np.sin(2*theta)-.45*np.sin(3*theta)
        r, rp = radius+h*speed, h*speed_prime
        nr, nt = r/np.hypot(r, rp), -rp/np.hypot(r, rp)
        def derivative(orders, wave, dwave):
            kr = k*r[:, None]
            angle = theta[:, None]*orders
            return (nr[:, None]*k*dwave(orders, kr)*np.cos(angle)
                    - (nt/r)[:, None]*orders*wave(orders, kr)*np.sin(angle))
        outgoing = derivative(modes, hankel2, h2vp)*norm/scale
        incoming = derivative(source_modes, hankel1, h1vp)@mixer
        return outgoing, incoming

    theta = 2*np.pi*(np.arange(256)+.5)/256
    a, b = boundary_matrices(theta)
    coefficients, _, rank, _ = np.linalg.lstsq(a, b, rcond=None)
    assert rank == max_order+1
    theta_check = 2*np.pi*(np.arange(check_count)+.137)/check_count
    ac, bc = boundary_matrices(theta_check)
    boundary_residual = relative(ac@coefficients, bc)
    all_outputs = coefficients/scale[:, None]
    power_error = float(np.max(abs(np.sum(abs(all_outputs)**2, axis=0)-1)))
    return mixer.T@all_outputs[source_modes], boundary_residual, power_error


def validate_nonuniform(mixer):
    radius, omega, length = .01, 2*np.pi*5e9, .01
    count, powers = 1024, np.array([.5, .5])
    theta = 2*np.pi*(np.arange(count)+.5)/count
    speed = 1+.25*np.cos(2*theta)+.15*np.cos(3*theta)
    hz, er, _ = fields(radius, omega, np.array([1, 2]), mixer, powers, np.zeros(2), length, theta)
    weights = length*radius*2*np.pi/count*speed
    predicted = -1j*omega/(4*.5)*(als.EPS0*(er*weights)@er.T+als.MU0*(hz*weights)@hz.T)
    base = scattering(radius, omega, np.array([1, 2]), mixer)[0]
    base_collocation, _, _ = perturbed_scattering(radius, 0., omega, mixer, 12)
    np.testing.assert_allclose(base_collocation, base, atol=1e-12)
    rows = []
    for h in radius*np.array([1e-3, 5e-4, 2.5e-4]):
        sp, rp, pp = perturbed_scattering(radius, h, omega, mixer, 12)
        sm, rm, pm = perturbed_scattering(radius, -h, omega, mixer, 12)
        sp_fine, rf, pf = perturbed_scattering(radius, h, omega, mixer, 16)
        sm_fine, rfm, pfm = perturbed_scattering(radius, -h, omega, mixer, 16)
        fd = (sp-sm)/(2*h)
        objective_errors = {}
        objective_residuals = {}
        for kind in ('abs',):
            f0, w = objective(base, kind)
            fp, _ = objective(sp, kind)
            fm, _ = objective(sm, kind)
            gf = np.real(w*predicted)
            objective_errors[kind] = relative((fp-fm)/(2*h), gf)
            objective_residuals[kind] = float(np.linalg.norm(fp-f0-h*gf))
        rows.append(dict(h_m=float(h), complex_fd_relative_error=relative(fd, predicted),
                         complex_taylor_residual=float(np.linalg.norm(sp-base-h*predicted)),
                         spectral_refinement_complex_change=max(float(np.max(abs(sp_fine-sp))), float(np.max(abs(sm_fine-sm)))),
                         held_out_boundary_residual=max(rp, rm, rf, rfm), power_balance_error=max(pp, pm, pf, pfm),
                         objective_fd_relative_errors=objective_errors, objective_taylor_residuals=objective_residuals))
    ratios = [rows[i]['complex_taylor_residual']/rows[i+1]['complex_taylor_residual'] for i in range(2)]
    fd_ratios = [rows[i]['complex_fd_relative_error']/rows[i+1]['complex_fd_relative_error'] for i in range(2)]
    assert all(3.8 < r < 4.2 for r in ratios+fd_ratios)
    assert max(r['spectral_refinement_complex_change'] for r in rows) < 1e-10
    assert max(r['held_out_boundary_residual'] for r in rows) < 1e-9
    assert max(r['power_balance_error'] for r in rows) < 1e-9
    assert max(rows[-1]['objective_fd_relative_errors'].values()) < 1e-5
    return dict(speed='1+0.25*cos(2*theta)+0.15*cos(3*theta)', radius_m=radius,
                S=matrix_pair(base), predicted_dS_per_m=matrix_pair(predicted),
                abs_dS_per_mm=(np.real(base.conj()/abs(base)*predicted)*1e-3).tolist(),
                perturbations=rows, complex_residual_halving_ratios=ratios, complex_fd_error_halving_ratios=fd_ratios,
                independent_forward_method='multipole boundary collocation, orders 0..12 and 0..16')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'results/pec_cylinder_smatrix_2026-11-12/results.json')
    args = parser.parse_args()
    two = np.array([[1., 1.], [1., -1.]])/np.sqrt(2)
    three = np.eye(3)
    for i, j, angle in ((0, 1, .43), (1, 2, -.62), (0, 2, .29)):
        rotate = np.eye(3)
        rotate[i, i] = rotate[j, j] = np.cos(angle)
        rotate[i, j], rotate[j, i] = -np.sin(angle), np.sin(angle)
        three = three@rotate
    cases = []
    for name, radius, freq, orders, mixer in (
            ('two_channel', .01, 5e9, [1, 2], two),
            ('three_channel_low', .006, 3e9, [1, 2, 3], three),
            ('three_channel_high', .014, 8e9, [1, 2, 3], three)):
        size = len(orders)
        cases.append(validate_case(name, radius, freq, orders, mixer, [.5]*size, [0.]*size))
        cases.append(validate_case(name+'_scaled_phased', radius, freq, orders, mixer,
                                   [.2, .7, 1.3][:size], [.37, -.81, 1.13][:size]))
        np.testing.assert_allclose(cases[-1]['predicted_dS_per_m'], cases[-2]['predicted_dS_per_m'], atol=1e-9)
    nonuniform = validate_nonuniform(two)
    report = dict(completed=True, scope='Cylindrical modal channels; not a CST port certification or antenna optimisation',
                  phasor='exp(+j*omega*t), peak fields', normal='metal into dielectric',
                  no_fitted_constants=True, cases=cases, nonuniform=nonuniform,
                  max_complex_derivative_relative_error=max(c['complex_derivative_relative_error'] for c in cases),
                  max_entry_relative_error=max(c['max_entry_relative_error'] for c in cases),
                  max_smallest_step_objective_fd_relative_error=max(max(c['perturbations'][-1]['objective_fd_relative_errors'].values()) for c in cases))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('cases', 'nonuniform')}, indent=2))
    print('Nonuniform boundary: smallest-step complex FD relative error', nonuniform['perturbations'][-1]['complex_fd_relative_error'])


if __name__ == '__main__':
    main()
