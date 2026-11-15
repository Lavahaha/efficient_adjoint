"""Test sign transfer from boundary extension through the actual HJ RHS."""
import json
import sys
from pathlib import Path
import numpy as np
from scipy.interpolate import RegularGridInterpolator
from shapely.geometry.polygon import orient
from shapely.geometry import box

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adjoint_levelset as als
from eaopt.geometry.implicit import reconstruct_triangular_levelset
from eaopt.geometry.evolution import CadBoundaryMesh


def moments(geometry):
    result=np.zeros(6)
    parts=[geometry] if geometry.geom_type=='Polygon' else geometry.geoms
    for p in parts:
        p=orient(p,sign=1.)
        for ring in (p.exterior,*p.interiors):
            xy=np.asarray(ring.coords)
            x,y=xy[:-1].T;u,v=xy[1:].T
            cross=x*v-u*y
            result+=np.array([np.sum(cross)/2,np.sum((x+u)*cross)/6,np.sum((y+v)*cross)/6,
                np.sum((x*x+x*u+u*u)*cross)/12,np.sum((2*x*y+x*v+u*y+2*u*v)*cross)/24,
                np.sum((y*y+y*v+v*v)*cross)/12])
    return result


def trace_map(grid,phi,points,normals):
    """Nodal RHS -> exact normal interface rate of this triangular field."""
    nodes=[]; maps=[]; gradients=[]
    for p,normal in zip(points,normals):
        i,j=np.floor((p-[grid.x0,grid.y0])/grid.dx).astype(int)
        i,j=np.clip(i,0,grid.nx-2),np.clip(j,0,grid.ny-2)
        corner=np.array([[i,j],[i+1,j],[i+1,j+1],[i,j+1]])
        xy=np.column_stack((grid.x[corner[:,0]],grid.y[corner[:,1]]))
        center=xy.mean(axis=0)
        delta=p-center
        k=(2 if delta[1]>=0 else 0) if abs(delta[1])>=abs(delta[0]) else (1 if delta[0]>=0 else 3)
        matrix=np.column_stack((np.vstack((xy[k],xy[(k+1)%4],center)),np.ones(3)))
        values=phi[corner[:,0],corner[:,1]]
        gradient=np.linalg.solve(matrix,[values[k],values[(k+1)%4],values.mean()])[:2]
        magnitude=np.linalg.norm(gradient)
        np.testing.assert_allclose(gradient/magnitude,normal,rtol=0,atol=1e-8)
        barycentric=np.linalg.solve(matrix.T,[*p,1.])
        weight=np.full(4,barycentric[2]/4)
        weight[k]+=barycentric[0];weight[(k+1)%4]+=barycentric[1]
        nodes.append(corner);maps.append(weight);gradients.append(magnitude)
    return np.asarray(nodes),np.asarray(maps),np.asarray(gradients)


def main():
    np.testing.assert_allclose(moments(box(-1,-1,1,1)),[4,0,0,4/3,0,4/3],atol=1e-12)
    grid=als.Grid2D(41,41,.15,-3,-3)
    x,y=grid.mesh()
    basic=((x+.037)/1.4)**2+((y-.021)/.9)**2-1
    rng=np.random.default_rng(1801)
    report=dict(scope='known quadratic volume objectives; research triangular interpolant and unchanged HJ RHS; no CST or optimizer step changes',cases=[],counterexample_found=False)
    out=ROOT/'results/hj_geometry_consistency_2026-11-15'
    out.mkdir(exist_ok=True)
    for factor in (np.ones_like(x),np.exp(2*np.cos(3*x+2*y))):
        ls=als.LevelSet(grid,basic*factor)
        base=reconstruct_triangular_levelset(grid.x,grid.y,ls.phi)
        # Retained-CAD helper supplies quadrature only, not a geometry updater.
        mesh=CadBoundaryMesh.from_geometry(base,2*grid.dx)
        points,normals,weights,_=mesh.quadrature()
        boundary=als.Boundary(points,normals,weights,np.ones(len(points),bool))
        nodes,trace_weights,gradient_norm=trace_map(grid,ls.phi,points,normals)
        qx,qy=points.T
        basis=np.array([np.ones(len(points)),qx,qy,qx*qx,qx*qy,qy*qy]).T
        for index in range(40):
            coefficients=rng.normal(size=6)
            density=basis@coefficients
            velocity=als.boundary_to_grid_velocity(ls,boundary,density,np.ones_like(x),4,smooth_sigma_cells=1.5)
            sampled=RegularGridInterpolator((grid.x,grid.y),velocity)(points)
            predicted=float(np.sum(weights*density*sampled))
            if predicted < -1e-10: raise AssertionError('Boundary extension sign gate failed')
            rhs=als._hj_rhs(ls.phi,velocity,grid.dx)
            normal_rate=-np.sum(trace_weights*rhs[nodes[:,:,0],nodes[:,:,1]],axis=1)/gradient_norm
            consistent_prediction=float(np.sum(weights*density*normal_rate))
            h=1e-6/max(float(abs(velocity).max()),1.)
            plus=reconstruct_triangular_levelset(grid.x,grid.y,ls.phi+h*rhs)
            minus=reconstruct_triangular_levelset(grid.x,grid.y,ls.phi-h*rhs)
            fd=float((moments(plus)-moments(minus))@coefficients/(2*h))
            evolved=reconstruct_triangular_levelset(grid.x,grid.y,als.advect_levelset(ls.phi,velocity,h,grid.dx,cfl=.4))
            actual=float((moments(evolved)-moments(base))@coefficients/h)
            row=dict(nonuniform_phi=bool(not np.all(factor==1)),index=index,coefficients=coefficients.tolist(),
                boundary_prediction=predicted,interpolant_consistent_prediction=consistent_prediction,
                discrete_central_slope=fd,actual_hj_forward_slope=actual,
                consistent_fd_relative_error=abs(consistent_prediction-fd)/max(abs(fd),1e-14))
            report['cases'].append(row)
            if predicted>0 and fd<0 and actual<0:
                report['counterexample_found']=True
                np.savez_compressed(out/'direction_transfer_counterexample.npz',phi=ls.phi,velocity=velocity,
                    density=density,points=points,normals=normals,weights=weights,coefficients=coefficients,h=h)
                break
        if report['counterexample_found']: break
    report['maximum_consistent_fd_relative_error']=max(r['consistent_fd_relative_error'] for r in report['cases'])
    if report['maximum_consistent_fd_relative_error']>1e-4:
        raise AssertionError('Interpolant-consistent prediction did not match the known objective')
    (out/'direction_transfer.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'counterexample_found':report['counterexample_found'],'tested_directions':len(report['cases']),
                      'last':report['cases'][-1]},indent=2))


if __name__=='__main__': main()
