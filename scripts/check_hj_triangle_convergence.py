"""Grid refinement and zero-motion projection checks of the HJ prototype."""
import json
import sys
from pathlib import Path
import numpy as np
from shapely.affinity import rotate, translate
from shapely.geometry import box, Polygon

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adjoint_levelset as als
from eaopt.geometry.state import CadGeometryState
from eaopt.geometry.implicit import reconstruct_triangular_levelset


def geometry(grid,phi):
    return reconstruct_triangular_levelset(grid.x,grid.y,phi)


def project(grid,phi):
    original=geometry(grid,phi)
    state=CadGeometryState(original,grid.x,grid.y)
    filtered=state.filter_features(.4)
    result=als.LevelSet(grid,phi if filtered.geometry.equals(original) else filtered.phi)
    result.reinit(preserve_cell_fans=True)
    return result.phi


def main():
    shapes={'aligned_rectangle':box(-1.5,-1,1.5,1),
        'rotated_rectangle':translate(rotate(box(-1.5,-1,1.5,1),17),xoff=.037,yoff=.021),
        'frame':Polygon([[-1.7,-1.7],[1.7,-1.7],[1.7,1.7],[-1.7,1.7]],
                        [[[-.7,-.7],[-.7,.7],[.7,.7],[.7,-.7]]])}
    report=dict(status='running',scope='research HJ dilation, triangular extraction, polygon feature projection and matched reinitialization; no CST',cases=[])
    out=ROOT/'results/hj_geometry_consistency_2026-11-15'
    out.mkdir(exist_ok=True)
    path=out/'triangle_convergence.json'
    def save(): path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        for name,shape in shapes.items():
            for dx in (.2,.1,.05):
                grid=als.Grid2D(round(8/dx)+1,round(8/dx)+1,dx,-4,-4)
                state=CadGeometryState(shape,grid.x,grid.y)
                initial=state.phi
                ls=als.LevelSet(grid,initial)
                x,y=grid.mesh()
                nx,ny=ls.normals_grid()
                speed=x*nx+y*ny
                base=geometry(grid,initial)
                zero_phi=project(grid,initial)
                zero=geometry(grid,zero_phi)
                repeated_phi=project(grid,zero_phi)
                repeated=geometry(grid,repeated_phi)
                row=dict(shape=name,dx_mm=dx,exact_area_mm2=shape.area,
                    continuous_dilation_area_derivative_mm2=2*shape.area,
                    initial_representation_error_mm2=base.symmetric_difference(shape).area,
                    first_zero_projection_change_mm2=zero.symmetric_difference(base).area,
                    repeated_zero_projection_change_mm2=repeated.symmetric_difference(zero).area,
                    scans=[])
                for h in (.001,-.001,1e-5,-1e-5):
                    moved=als.advect_levelset(initial,speed*np.sign(h),abs(h),dx,cfl=.4)
                    raw=geometry(grid,moved)
                    final=geometry(grid,project(grid,moved))
                    row['scans'].append(dict(h_dimensionless=h,
                        raw_area_quotient_mm2=(raw.area-base.area)/h,
                        projected_quotient_from_initial_mm2=(final.area-base.area)/h,
                        projected_quotient_from_zero_projection_mm2=(final.area-zero.area)/h,
                        final_projection_change_mm2=final.symmetric_difference(raw).area))
                report['cases'].append(row)
                save()
                print(name,dx,'zero-motion changes',row['first_zero_projection_change_mm2'],row['repeated_zero_projection_change_mm2'],flush=True)
        report['status']='complete'
    except BaseException as error:
        report.update(status='failed',error=repr(error))
        raise
    finally: save()


if __name__=='__main__': main()
