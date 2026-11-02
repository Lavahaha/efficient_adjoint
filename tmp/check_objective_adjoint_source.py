"""Algebraic regression with saved real CST fields; performs no EM solves."""
from pathlib import Path
import json
import sys
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adjoint_levelset as a
from eaopt.solver.cst_results import export_field_points
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_iteration_algorithm import load_bundle

out = Path('results/adjoint_checks_2026-11-01/divider/sign_sampling_2026-11-02')
geo = np.load(out / 'sample_geometry.npz')
b = a.Boundary(geo['boundary'], geo['normals'], geo['weights'], geo['active'])
f, ad = [np.load(out / name) for name in ('fwd_samples.npz', 'bwd_samples.npz')]
audit = json.loads((out / 'root_cause_audit.json').read_text())
s = complex(*audit['source_S21'])
ef, hf, eb, hb = f['E'][0], f['H'][0], ad['E'][0], ad['H'][0]
prefactor = -2j * 2 * np.pi * 5e9 / .5
q = a.shape_derivative_from_fields(b, ef, hf, eb, hb, prefactor, a.EPS0 * 3)
for kind in ('abs', 'abs2', 'db', 'real'):
    _, weight = a.fom_value_and_weight(s, kind)
    amplitude = np.conj(weight)
    actual = np.real(a.shape_derivative_from_fields(
        b, ef, hf, amplitude * eb, amplitude * hb, prefactor, a.EPS0 * 3))
    assert np.allclose(actual, a.real_gradient(q, weight), rtol=1e-12, atol=1e-5)
    for ds in (1., 1j, .3-.7j):
        h = 1e-7
        fd = (a.fom_value_and_weight(s+h*ds, kind)[0] -
              a.fom_value_and_weight(s-h*ds, kind)[0]) / (2*h)
        assert np.isclose(fd, np.real(amplitude * ds), rtol=1e-7, atol=1e-8)
_, wa = a.fom_value_and_weight(s, 'abs')
_, wd = a.fom_value_and_weight(s, 'db')
assert np.isclose(abs(np.conj(wa)), 1.)
assert np.allclose(a.real_gradient(q, wd),
                   20/(np.log(10)*abs(s))*a.real_gradient(q, wa))
base = out.parent / 'fd_fine/base'
grid_f, grid_b = load_bundle(base/'fwd'), load_bundle(base/'bwd')
grid_q = a.shape_derivative_complex(b, grid_f, grid_b, prefactor, .2, a.EPS0*3)
weighted_b = a.FieldBundle(x=grid_b.x, y=grid_b.y,
                          E=np.conj(wa)*grid_b.E, H=np.conj(wa)*grid_b.H)
weighted_q = a.shape_derivative_complex(b, grid_f, weighted_b, prefactor, .2, a.EPS0*3)
assert np.allclose(np.real(weighted_q), a.real_gradient(grid_q, wa), rtol=1e-12, atol=1e-5)
for invalid in ([], [[1, 2]], [[0, 0, float('nan')]]):
    try:
        export_field_points(None, 'Efield', 5., invalid, out / 'invalid.txt')
    except ValueError:
        pass
    else:
        raise AssertionError('Invalid point array reached the CST API')
result = {'status': 'passed', 'scope': 'saved real fields and algebra; no new EM solve',
          'objective_kinds': ['abs', 'abs2', 'db', 'real'],
          'source_scaling_equals_objective_chain_rule': True,
          'grid_field_source_scaling_equals_chain_rule': True,
          'invalid_points_rejected_before_CST': True}
(out / 'objective_source_selfcheck.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
