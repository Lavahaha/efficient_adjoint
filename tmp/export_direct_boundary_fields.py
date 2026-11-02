"""Export CST fields at exact boundary sample points, bypassing grid resampling."""
from pathlib import Path
import json,sys
import numpy as np
from scipy.spatial import cKDTree
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import adjoint_levelset as a
import run_qtem_checks as em
from run_directional_fd_checks import boundary_and_directions
from check_iteration_algorithm import load_bundle
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst import _connect,_open_or_reuse
from eaopt.solver import cst_results as R

def main():
    report={}
    de=_connect(False,True,em.log,quiet=True)
    try:
        for case in ('divider','coupler'):
            cfg=CaseConfig.from_yaml(em.ROOT/f'configs/{case}.yaml')
            setup,_=load_case(cfg)
            boundary,directions=boundary_and_directions(cfg)
            offset=setup.resolve_export_steps(cfg.field_export_step_mm)[0]
            xy=boundary.pts+offset*boundary.normals
            active=np.maximum.reduce(list(directions.values()))>1e-4
            heights=(-.1,0.,.0175,.035,.1)
            direct_points=np.vstack([np.column_stack([xy,np.full(len(xy),z)]) for z in heights])
            root=em.OUT/case/'fd_fine'
            grid=load_bundle(root/'base/fwd')
            ix=np.searchsorted(grid.x,xy[:,0],side='right')-1
            iy=np.searchsorted(grid.y,xy[:,1],side='right')-1
            fx=(xy[:,0]-grid.x[ix])/(grid.x[ix+1]-grid.x[ix])
            fy=(xy[:,1]-grid.y[iy])/(grid.y[iy+1]-grid.y[iy])
            # These raw z planes are recorded by this run's regular-grid exports.
            raw_z=(-.0485,.0515)
            corner_points=np.stack([np.column_stack([grid.x[ix+i],grid.y[iy+j],np.full(len(xy),raw_z[k])])
                for i in (0,1) for j in (0,1) for k in (0,1)],axis=1)
            weights=np.stack([(fx if i else 1-fx)*(fy if j else 1-fy)*(.485 if k else .515)
                for i in (0,1) for j in (0,1) for k in (0,1)],axis=1)
            assert np.allclose(weights.sum(axis=1),1)
            points,inverse=np.unique(np.round(np.vstack([direct_points,corner_points.reshape(-1,3)]),9),axis=0,return_inverse=True)
            pointfile=root/'boundary_points_with_corners.txt'
            np.savetxt(pointfile,points,fmt='%.12g')
            values={}; comparisons={}
            for tag in ('fwd','bwd'):
                folder=root/'base'/tag
                project=_open_or_reuse(de,folder/'model.cst',em.log)
                try:
                    tree=json.loads((folder/'field_tree.json').read_text(encoding='utf-8'))
                    arrays=[]
                    for kind,branch in [('E','E-Field'),('H','H-Field')]:
                        out=folder/f'direct_boundary_corners_{kind}.txt'
                        if not out.exists():
                            item=next(v for v in tree if f'\\{branch}\\' in v and '(f=5)' in v)
                            R._select_item(project.model3d,item)
                            code=f'''Sub Main
With ASCIIExport
 .Reset
 .Mode "FixedWidth"
 .UseSubvolume False
 .FileName "{out}"
 .SetPointFile "{pointfile}"
 .ExportCoordinatesInMeter "False"
 .Execute
End With
End Sub'''
                            project.model3d._execute_vba_code(code)
                        table=np.loadtxt(out,skiprows=2)
                        assert table.shape==(len(points),9), (out,table.shape)
                        distance,index=cKDTree(points).query(table[:,:3])
                        assert distance.max()<1e-4 and len(np.unique(index))==len(points)
                        data=np.empty((len(points),3),complex)
                        data[index]=table[:,3::2]+1j*table[:,4::2]
                        assert np.isfinite(data).all()
                        arrays.append(data[inverse])
                    corner_values=[v[len(direct_points):].reshape(len(xy),8,3) for v in arrays]
                    reconstructed=[np.sum(v*weights[:,:,None],axis=1) for v in corner_values]
                    arrays=[v[:len(direct_points)].reshape(len(heights),len(xy),3) for v in arrays]
                    values[tag]=arrays
                    regular=load_bundle(folder)
                    e,h=regular.sample(xy)
                    comparisons[tag]={'E_z0_direction_support_relative':float(np.linalg.norm(arrays[0][1,active]-e.T[active])/np.linalg.norm(e.T[active])),
                        'H_z0_direction_support_relative':float(np.linalg.norm(arrays[1][1,active]-h.T[active])/np.linalg.norm(h.T[active])),
                        'native_corners_reconstructed_E_relative':float(np.linalg.norm(reconstructed[0][active]-e.T[active])/np.linalg.norm(e.T[active])),
                        'native_corners_reconstructed_H_relative':float(np.linalg.norm(reconstructed[1][active]-h.T[active])/np.linalg.norm(h.T[active]))}
                finally:
                    project.close()
            n=np.column_stack([boundary.normals,np.zeros(len(xy))])
            dot=lambda x,y:np.sum(x*y,axis=1)
            rows=[]
            for iz,z in enumerate(heights):
                ef,hf=(v[iz] for v in values['fwd']); eb,hb=(v[iz] for v in values['bwd'])
                g=np.real(-2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w*(
                    a.EPS0*setup.eps_r*dot(ef,n)*dot(eb,n)+a.MU0*(dot(hf,hb)-dot(hf,n)*dot(hb,n))))
                rows.append({'z_mm':z,'directional':{name:float(np.sum(g*u*boundary.weights)) for name,u in directions.items()}})
            report[case]={'comparison_to_regular_grid_at_same_points':comparisons,'direct_predictions':rows}
            em.write(em.OUT/'direct_boundary_checks.json',report)
            em.log(f'DIRECT_BOUNDARY {case} {report[case]}')
    finally:
        de.close()

if __name__=='__main__':
    main()
