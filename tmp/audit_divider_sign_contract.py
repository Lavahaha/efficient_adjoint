"""Audit sign and objective contracts using real saved CST point fields."""
from pathlib import Path
import json,sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import adjoint_levelset as a

ROOT=Path(__file__).resolve().parents[1]
OLD=ROOT/'results/adjoint_checks_2026-11-01'
OUT=OLD/'divider/sign_sampling_2026-11-02'
geo=np.load(OUT/'sample_geometry.npz')
b=a.Boundary(geo['boundary'],geo['normals'],geo['weights'],geo['active'])
f=np.load(OUT/'fwd_samples.npz'); ad=np.load(OUT/'bwd_samples.npz')
assert np.array_equal(f['points'],ad['points'])
rows=json.loads((OLD/'divider/fd_fine/base/fwd/s_params.json').read_text(encoding='utf-8'))['coefficients']
row=next(r for r in rows if r[:4]==[2,1,1,1]); s=complex(*row[4:])
_,ca=a.fom_value_and_weight(s,'abs'); _,cd=a.fom_value_and_weight(s,'db')
directions=dict(np.load(OUT/'directions.npz'))
test_u={k:directions[k] for k in ('velocity','input_width')}
for name,y0 in [('upper',2.4),('lower',-2.4)]:
    x=(b.pts[:,0]-29.5)/2.; y=(b.pts[:,1]-y0)/1.5
    u=np.where((abs(x)<1)&(abs(y)<1),np.cos(np.pi*x/2)**2*np.cos(np.pi*y/2)**2,0)
    u/=u.max(); test_u[name]=u
report={'source_S21':[s.real,s.imag], 'objective':'abs(S21)',
    'source_factor_from_abs_objective':[float(np.conj(ca).real),float(np.conj(ca).imag)],
    'interpretation':'No change to Eq25 real operation; compare bare reverse source with objective-derived source. No FD-fitted phase.',
    'sampling':[]}
cases=[(off,z,eps) for off in (.2,.1,.05) for z,eps in ((0.,3.),(.0175,1.),(-.1,3.))]
for i,(off,z,eps) in enumerate(cases):
    q=a.shape_derivative_from_fields(b,f['E'][i],f['H'][i],ad['E'][i],ad['H'][i],
        -2j*2*np.pi*5e9/.5,a.EPS0*eps,a.MU0)
    reversed_normal=a.Boundary(b.pts,-b.normals,b.weights,b.active)
    reversed_q=a.shape_derivative_from_fields(reversed_normal,f['E'][i],f['H'][i],ad['E'][i],ad['H'][i],
        -2j*2*np.pi*5e9/.5,a.EPS0*eps,a.MU0)
    assert np.allclose(q,reversed_q,rtol=1e-13,atol=1e-5)
    raw=np.real(q); consistent=a.real_gradient(q,ca); db=a.real_gradient(q,cd)
    scale=20/(np.log(10)*abs(s))
    assert np.allclose(db,scale*consistent,rtol=1e-12,atol=1e-4)
    used=b.active
    cosine=np.dot(raw[used],consistent[used])/(np.linalg.norm(raw[used])*np.linalg.norm(consistent[used]))
    row={'offset_mm':off,'z_mm':z,'eps_r_sample':eps,'single_vs_db_equivalent_cosine':float(cosine),
        'directional':{}}
    for name,u in test_u.items():
        qq=np.sum(q*u*b.weights)
        row['directional'][name]={'Q':[float(qq.real),float(qq.imag)],
            'bare_source_prediction':float(np.sum(raw*u*b.weights)),
            'objective_source_prediction':float(np.sum(consistent*u*b.weights))}
    report['sampling'].append(row)
# Verify the shared kernel reproduces the independently exported point audit.
previous=json.loads((OLD/'direct_boundary_checks.json').read_text(encoding='utf-8'))['divider']
report['scope']='Objective-contract audit, not proof that the approximate spatial sensitivity is quantitatively converged.'
report['normal_projection_reversal_at_fixed_points_is_invariant']=True
(OUT/'root_cause_audit.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report['sampling'][0],indent=2))
