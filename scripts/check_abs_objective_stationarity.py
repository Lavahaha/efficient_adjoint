"""Fixed-port lossless two-port analytic counterexample near zero reflection.

Normalized shunt susceptance b: continuity gives t=1+r and current balance
1-r-t=j*b*t, hence r=-j*b/(2+j*b), t=2/(2+j*b). This circuit parameter is
not a CAD displacement and this check does not certify the antenna solver.
"""
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als


def scattering(b):
    r, t = -1j*b/(2+1j*b), 2/(2+1j*b)
    return np.array([[r, t], [t, r]])


def gradient(b, output):
    s = scattering(b)[output, 0]
    _, weight = als.fom_value_and_weight(s, 'abs')
    ds = -2j/(2+1j*b)**2
    return float(np.real(np.conj(weight)*ds))


def main():
    rows = []
    for b in (-1., -.07, -.001, .001, .07, 1.):
        s = scattering(b)
        np.testing.assert_allclose(s.conj().T@s, np.eye(2), atol=1e-14)
        np.testing.assert_array_equal(s, s.T)
        for output in (0, 1):
            fd = (abs(scattering(b+1e-7)[output, 0])-abs(scattering(b-1e-7)[output, 0]))/2e-7
            np.testing.assert_allclose(gradient(b, output), fd, rtol=3e-6, atol=1e-9)
        np.testing.assert_allclose(gradient(b, 0), 4*np.sign(b)/(4+b*b)**1.5, rtol=1e-12)
        np.testing.assert_allclose(gradient(b, 1), -2*b/(4+b*b)**1.5, rtol=1e-12)
    for step in (.1, .05):
        for output, sign in ((0, -1), (1, 1)):
            b = .07
            trajectory = []
            for iteration in range(800):
                value, g = abs(scattering(b)[output, 0]), gradient(b, output)
                new_b = b+sign*step*g
                new_value = abs(scattering(new_b)[output, 0])
                trajectory.append(dict(iteration=iteration, b=b, value=float(value), gradient=g,
                                       new_b=new_b, new_value=float(new_value),
                                       signed_gain=float(sign*(new_value-value))))
                b = new_b
            tail = trajectory[-100:]
            rows.append(dict(target=f'S{output+1}1', step=step, initial_b=.07,
                final_b=b, final_value=float(abs(scattering(b)[output, 0])),
                tail_max_abs_b=max(abs(r['b']) for r in tail),
                tail_min_abs_gradient=min(abs(r['gradient']) for r in tail),
                worsening_steps=sum(r['signed_gain'] < -1e-14 for r in trajectory),
                history=trajectory))
    reflection = [r for r in rows if r['target'] == 'S11']
    transmission = [r for r in rows if r['target'] == 'S21']
    assert all(r['tail_min_abs_gradient'] > .49 and r['worsening_steps'] > 100 for r in reflection)
    assert all(abs(r['final_b']) < 4e-6 and r['worsening_steps'] == 0 for r in transmission)
    out = ROOT/'results/abs_objective_stationarity_2026-11-13'
    out.mkdir(parents=True, exist_ok=True)
    report = dict(scope='analytic fixed-port reactive two-port; exact scalar circuit update',
                  production_changed=False, antenna_cause_proven=False,
                  full_goal_achieved=False, cases=rows)
    (out/'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k != 'history'} for r in rows]), flush=True)


if __name__ == '__main__':
    main()
