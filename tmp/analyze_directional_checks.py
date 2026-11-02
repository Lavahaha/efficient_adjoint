"""Summarize completed FD pairs without a per-direction scale/sign fit."""
from pathlib import Path
import json
import numpy as np
from shapely.geometry import Polygon
from shapely.affinity import scale as reflect_scale

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/adjoint_checks_2026-11-01'
result={}
for case in ('divider','coupler'):
    root=OUT/case/'fd_fine'
    path=root/'directional_checks.json'
    if not path.exists():
        continue
    data=json.loads(path.read_text(encoding='utf-8'))
    if len(data['rows'])!=4:
        continue
    rows=data['rows']
    names=list(dict.fromkeys(row['direction'] for row in rows))
    g=np.array([next(r['adjoint_raw'] for r in rows if r['direction']==name) for name in names])
    hs={}
    vectors=[]
    for h in (.04,.08):
        f=np.array([next(r['finite_difference_per_mm'] for r in rows if r['direction']==name and r['h_mm']==h) for name in names])
        assert np.isfinite(f).all() and np.linalg.norm(f)>0
        scale=float(np.dot(g,f)/np.dot(g,g))
        hs[str(h)]={'finite_difference':f.tolist(),
            'sign_matches':(g*f>0).tolist(),
            'cosine':float(np.dot(g,f)/(np.linalg.norm(g)*np.linalg.norm(f))),
            'best_common_scale':scale,
            'relative_residual_after_common_scale':float(np.linalg.norm(scale*g-f)/np.linalg.norm(f))}
        vectors.append(f)
    mesh=(root/'base/fwd/Model.msh').read_bytes()
    folders=['base/bwd']+[f"{r['direction']}_{r['h_mm']:.2f}_{s}" for r in rows for s in ('plus','minus')]
    assert all((root/p/'Model.msh').read_bytes()==mesh for p in folders)
    counts=np.frombuffer(mesh,dtype='<i4',count=5)[2:].tolist()
    result[case]={'base_objective':data['base_objective'],'directions':names,'adjoint_raw':g.tolist(),
        'h_results':hs,'FD_h_relative_change':float(np.linalg.norm(vectors[0]-vectors[1])/np.linalg.norm(vectors[1])),
        'FD_h_component_relative_changes':(abs(vectors[0]-vectors[1])/np.maximum(abs(vectors[1]),1e-15)).tolist(),
        'all_actual_meshes_equal':True,'actual_mesh_header_counts':counts}
    assert len(mesh)==32+8*sum(counts) and np.frombuffer(mesh,dtype='<i4',count=1)[0]==20080423
    axes=np.split(np.frombuffer(mesh,dtype='<f8',offset=32),np.cumsum(counts)[:-1])
    assert all(np.all(np.diff(v)>0) for v in axes)
    if case=='divider':
        result[case]['mesh_y_reflection_error_mm']=float(np.max(abs(axes[1]+axes[1][::-1]-axes[1][0]-axes[1][-1]))*1e3)
        mirror_rows=[]
        for h in (.04,.08):
            values={}
            for sign in ('plus','minus'):
                upper=root/f'upper_{h:.2f}_{sign}'
                lower=root/f'lower_{h:.2f}_{sign}'
                polys=[Polygon(json.loads((p/'config.json').read_text(encoding='utf-8'))['shape'][0]) for p in (upper,lower)]
                assert reflect_scale(polys[0],xfact=1,yfact=-1,origin=(0,0)).symmetric_difference(polys[1]).area<1e-8
                coeff=json.loads((upper/'s_params.json').read_text(encoding='utf-8'))['coefficients']
                row=next(r for r in coeff if r[:4]==[3,1,1,1])
                values[sign]=abs(complex(*row[4:]))
            inferred=(values['plus']-values['minus'])/(2*h)
            actual=next(r['finite_difference_per_mm'] for r in rows if r['direction']=='lower' and r['h_mm']==h)
            mirror_rows.append({'h_mm':h,'upper_perturb_S31_FD':inferred,'lower_perturb_S21_FD':actual,
                'relative_difference':float(abs(inferred-actual)/max(abs(actual),1e-15))})
        result[case]['physical_reflection_check']=mirror_rows
    assert abs(hs['0.04']['cosine'])<=1+1e-12
    direct_path=OUT/'direct_boundary_checks.json'
    if direct_path.exists():
        direct=json.loads(direct_path.read_text(encoding='utf-8'))[case]
        for comparison in direct['comparison_to_regular_grid_at_same_points'].values():
            assert comparison['native_corners_reconstructed_E_relative']<1e-6
            assert comparison['native_corners_reconstructed_H_relative']<1e-6
        dg=next(r['directional'] for r in direct['direct_predictions'] if r['z_mm']==0)
        dv=np.array([dg[name] for name in names])
        result[case]['direct_z0_sampling']={'adjoint_raw':dv.tolist(),'h_results':{}}
        for h in ('0.04','0.08'):
            fv=np.array(hs[h]['finite_difference'])
            scalar=np.dot(dv,fv)/np.dot(dv,dv)
            result[case]['direct_z0_sampling']['h_results'][h]={
                'sign_matches':(dv*fv>0).tolist(),
                'cosine':float(np.dot(dv,fv)/(np.linalg.norm(dv)*np.linalg.norm(fv))),
                'relative_residual_after_common_real_scale':float(np.linalg.norm(scalar*dv-fv)/np.linalg.norm(fv))}
(OUT/'directional_analysis.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
