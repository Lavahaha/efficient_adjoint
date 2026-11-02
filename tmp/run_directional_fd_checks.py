"""Eq. (25) directional checks with the same CAD and a geometry-independent mesh."""
import json
from pathlib import Path
import sys
import traceback

import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"tmp"))
import adjoint_levelset as a
import run_qtem_checks as em
from check_iteration_algorithm import load_bundle
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst import _connect

def boundary_and_directions(cfg,spacing=.2):
    original=orient(unary_union([Polygon(p.vertices) for p in cfg.initial_metal]),sign=1)
    vertices=np.asarray(original.exterior.coords)
    points=[]
    for p,q in zip(vertices[:-1],vertices[1:]):
        middle=.5*(p+q)
        dense=(middle[0]>26 if cfg.name=="divider" else abs(q[0]-p[0])>2)
        n=max(1,int(np.ceil(np.linalg.norm(q-p)/spacing))) if dense else 1
        points.extend(p+(q-p)*t/n for t in range(n))
    points=np.array(points)
    assert len(points)<2400
    assert Polygon(points).symmetric_difference(original).area<1e-8
    previous=points-np.roll(points,1,axis=0)
    following=np.roll(points,-1,axis=0)-points
    length_prev=np.linalg.norm(previous,axis=1)
    length_next=np.linalg.norm(following,axis=1)
    tangent=previous/length_prev[:,None]+following/length_next[:,None]
    normals=np.column_stack([tangent[:,1],-tangent[:,0]])
    normals/=np.linalg.norm(normals,axis=1)[:,None]
    boundary=a.Boundary(points,normals,.5*(length_prev+length_next),np.ones(len(points),bool))
    directions={}
    centers=((("upper",29.5,2.4),("lower",29.5,-2.4)) if cfg.name=="divider" else
             (("left",4.,-.8),("right",8.,-.8)))
    for name,x0,y0 in centers:
        x=(points[:,0]-x0)/(2 if cfg.name=="divider" else 1.7)
        y=(points[:,1]-y0)/(1.5 if cfg.name=="divider" else 1.4)
        u=np.where((abs(x)<1)&(abs(y)<1),np.cos(np.pi*x/2)**2*np.cos(np.pi*y/2)**2,0)
        assert u.max()>.1
        u/=u.max()
        for edge in cfg.design_region.box.x:
            assert not u[abs(points[:,0]-edge)<1e-8].any()
        directions[name]=u
    return boundary,directions

def goal(folder,cfg):
    data=json.loads((folder/"s_params.json").read_text(encoding="utf-8"))
    row=next(r for r in data["coefficients"] if r[:4]==[cfg.objective.to_port,1,1,1])
    return abs(complex(*row[4:]))

def run_case(case):
    OUT=em.OUT/case/"fd_fine"
    OUT.mkdir(parents=True,exist_ok=True)
    cfg=CaseConfig.from_yaml(ROOT/f"configs/{case}.yaml")
    setup,model=load_case(cfg)
    boundary,directions=boundary_and_directions(cfg)
    np.savez_compressed(OUT/"directions.npz",pts=boundary.pts,normals=boundary.normals,
        weights=boundary.weights,**directions)
    # The installed CST snapping macro supplies these version-specific keys.
    blocks=[("FD fixed mesh controls",
        'With MeshSettings\n'
        ' .SetMeshType "Hex"\n'
        ' .Set "Version", 1\n'
        ' .Set "UseMaxNumFacesForSnapping", "1"\n'
        ' .Set "MaxNumFacesForSnapping", "1"\n'
        'End With\n'
        'With Mesh\n'
        ' .Automesh "True"\n'
        ' .MinimumStepNumber "120"\n'
        ' .LinesPerWavelength "20"\n'
        'End With\n'
        'Solid.SetMeshStepWidth "component1:substrate", "0.2", "0.2", "0.1"\n'
        'Solid.SetMeshStepWidth "component1:air", "0.4", "0.4", "0.4"\n')]
    de=None
    try:
        de=_connect(False,True,em.log,quiet=True)
        for tag in ("fwd","bwd"):
            em.run_project(de,cfg,setup,model,OUT/"base"/tag,2,tag,
                polygons=[np.vstack([boundary.pts,boundary.pts[0]])],extra_blocks=blocks)
        fwd=load_bundle(OUT/"base/fwd")
        adj=load_bundle(OUT/"base/bwd")
        prefactor=-2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w
        offset=setup.resolve_export_steps(cfg.field_export_step_mm)[0]
        g=np.real(a.shape_derivative_complex(boundary,fwd,adj,prefactor,offset,a.EPS0*setup.eps_r,a.MU0))
        report={"base_objective":goal(OUT/"base/fwd",cfg),"rows":[],"definition":"Eq25 real sensitivity, no additional phase weights"}
        base_mesh=(OUT/"base/fwd/Model.msh").read_bytes()
        assert (OUT/"base/bwd/Model.msh").read_bytes()==base_mesh, "Forward/backward spatial meshes differ"
        counts=np.frombuffer(base_mesh,dtype='<i4',count=5)[2:]
        assert counts[0]>(200 if case=="divider" else 100), ("Fine solver mesh not applied",counts)
        for name,u in directions.items():
            prediction=float(np.sum(g*u*boundary.weights))
            for h in (.04,.08):
                values={}
                for sign in (1,-1):
                    points=boundary.pts+sign*h*u[:,None]*boundary.normals
                    shape=Polygon(points)
                    assert shape.is_valid and shape.geom_type=="Polygon"
                    folder=OUT/f"{name}_{h:.2f}_{'plus' if sign>0 else 'minus'}"
                    em.run_project(de,cfg,setup,model,folder,2,"fwd",
                        polygons=[np.vstack([points,points[0]])],export_fields=False,extra_blocks=blocks,
                        clone_from=OUT/"base/fwd/model.cst")
                    assert (folder/"Model.msh").read_bytes()==base_mesh, "FD spatial mesh changed"
                    values[sign]=goal(folder,cfg)
                fd=(values[1]-values[-1])/(2*h)
                row={"direction":name,"h_mm":h,"adjoint_raw":prediction,"finite_difference_per_mm":fd,
                    "plus":values[1],"minus":values[-1],"ratio_raw":prediction/fd if fd else None}
                report["rows"].append(row)
                em.write(OUT/"directional_checks.json",report)
                em.log(f"FD_RESULT {row}")
        em.write(OUT/"status.json",{"status":"complete","solves":10})
    except BaseException:
        em.write(OUT/"status.json",{"status":"failed","traceback":traceback.format_exc()})
        raise
    finally:
        if de is not None:
            de.close()

if __name__=="__main__":
    for case in ("divider","coupler"):
        run_case(case)
