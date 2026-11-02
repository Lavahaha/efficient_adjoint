"""Analytic HJ checks and exact replay of saved, real CST field-driven updates."""
import copy
import json
from pathlib import Path
import sys

import numpy as np
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adjoint_levelset as a
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import LevelSet2D, make_level_set
from eaopt.geometry.design import compose_metal_regions, connection_boundary_mask
from eaopt.optimize.constraints import (ConnectivityGuard,apply_min_gap,
    apply_min_feature,build_velocity_mask)
from eaopt.runner import _movement_constraints
from eaopt.solver.case import load_case

OUT=ROOT/"results/adjoint_checks_2026-11-01/algorithm"

def write(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")

def load_bundle(folder,tag=None):
    grids=[]
    for kind in ("e","h"):
        path=(folder/f"fields_{tag}_{kind}.npz" if tag else
              folder/("Efield.npz" if kind=="e" else "Hfield.npz"))
        with np.load(path) as data:
            axes=[data["origin"][j]+data["spacing"][j]*np.arange(data["data"].shape[j]) for j in range(3)]
            iz=int(np.argmin(abs(axes[2])))
            grids.append(np.moveaxis(data["data"][:,:,iz,:],-1,0))
    return a.FieldBundle(axes[0],axes[1],grids[0],grids[1])

def sensitivity(case,ls,mask,fwd,adj):
    cfg=CaseConfig.from_yaml(ROOT/f"configs/{case}.yaml")
    setup,_=load_case(cfg)
    boundary=a.extract_boundary(ls,mask)
    offset=setup.resolve_export_steps(cfg.field_export_step_mm)[0]
    g=np.real(a.shape_derivative_complex(boundary,fwd,adj,
        -2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w,offset,
        a.EPS0*setup.eps_r,a.MU0))
    return boundary,g

def analytic_checks():
    checks=[]
    for dx in (.1,.05):
        grid=a.Grid2D(int(10/dx)+1,int(10/dx)+1,dx,-5,-5)
        X,Y=grid.mesh()
        for axis,coord in (("x",X),("y",Y)):
            for speed in (-.7,.7):
                phi=coord-.137
                result=a.advect_levelset(phi,np.full(phi.shape,speed),.3,dx,.5)
                strip=result[:,grid.ny//2] if axis=="x" else result[grid.nx//2,:]
                location=float(np.interp(0,strip,grid.x))
                error=abs(location-(.137+speed*.3))
                checks.append({"test":"plane","axis":axis,"dx":dx,"speed":speed,
                    "error_mm":error,"pass":bool(error<1e-10)})
        phi=np.hypot(X,Y)-2
        result=a.advect_levelset(phi,np.full(phi.shape,.5),.3,dx,.5)
        ls=a.LevelSet(grid,result)
        b=a.extract_boundary(ls)
        radius=np.linalg.norm(b.pts,axis=1)
        checks.append({"test":"circle","dx":dx,"radius_error_mm":float(np.mean(abs(radius-2.15))),
            "normal_radial_dot_min":float(np.min(np.sum(b.normals*b.pts/radius[:,None],axis=1))),
            "pass":bool(np.mean(abs(radius-2.15))<.02*dx)})
        half=a.advect_levelset(phi,np.full(phi.shape,.5),.3,dx,.25)
        bh=a.extract_boundary(a.LevelSet(grid,half))
        diff=LineString(np.vstack([b.pts,b.pts[0]])).hausdorff_distance(LineString(np.vstack([bh.pts,bh.pts[0]])))
        checks.append({"test":"same_outer_time_CFL","dx":dx,"hausdorff_mm":diff,"pass":bool(diff<.01*dx)})
        initial=LineString(a.extract_boundary(a.LevelSet(grid,phi)).pts)
        ls=a.LevelSet(grid,phi)
        drift=[]
        for k in range(50):
            ls.reinit()
            if k in (0,9,49):
                drift.append({"cycles":k+1,"hausdorff_mm":initial.hausdorff_distance(LineString(a.extract_boundary(ls).pts))})
        checks.append({"test":"reinit_only","dx":dx,"drift":drift,
            "pass":bool(drift[-1]["hausdorff_mm"]<.1*dx)})
    grid=np.linspace(-1,1,5)
    E=np.zeros((3,5,5),complex); E[0]=1j
    H=np.zeros_like(E)
    bundle=a.FieldBundle(grid,grid,E,H)
    boundary=a.Boundary(np.array([[0.,0.]]),np.array([[1.,0.]]),np.array([1.]),np.array([True]))
    value=a.shape_derivative_complex(boundary,bundle,bundle,1,.1,eps=2,mu=3)[0]
    assert abs(value+2)<1e-12
    checks.append({"test":"Eq25_bilinear_normal_projection","pass":True})
    return checks

def geom(cfg,phi):
    return unary_union([Polygon(r.outer,r.holes) for r in compose_metal_regions(cfg,phi)])

def replay(case):
    source=ROOT/f"results/{case}_local_serial_smoke"
    cfg=CaseConfig.from_yaml(ROOT/f"configs/{case}.yaml")
    _,model=load_case(cfg)
    original=make_level_set(cfg)
    grid=a.Grid2D(original.nx,original.ny,original.dx,original.xs[0],original.ys[0])
    mask,force_metal,force_air,anchor=_movement_constraints(original,cfg)
    pinned=connection_boundary_mask(original,cfg,model.fixed_metal_polygons())
    mask[pinned]=0
    guard=ConnectivityGuard(original,cfg,log=lambda _:None) if cfg.constraints.require_connected else None
    candidate=LevelSet2D(original.box,original.dx)
    rows=[]
    for state in (0,1):
        before=np.load(source/f"phi_before_iter_{state:03d}.npz")["phi"]
        expected=np.load(source/f"phi_after_iter_{state:03d}.npz")["phi"]
        ls=a.LevelSet(grid,before)
        fwd=load_bundle(source/f"iter_{state:03d}","fwd")
        adj=load_bundle(source/f"iter_{state:03d}","bwd")
        b,g=sensitivity(case,ls,mask,fwd,adj)
        V=a.boundary_to_grid_velocity(ls,b,g,mask,
            cfg.level_set.extension_band_mm/grid.dx,smooth_sigma_cells=1.5)
        sampled=RegularGridInterpolator((grid.x,grid.y),V,bounds_error=True)(b.pts)
        active=b.active & (abs(g)>np.max(abs(g))*1e-4)
        row={"state":state,"g_dot_V":float(np.sum(g*sampled*b.weights)),
            "weighted_sign_disagreement":float(np.sum(b.weights[active & (g*sampled<0)])/np.sum(b.weights[active])),
            "stages":[],"repair_log":[]}
        guard_before=copy.deepcopy(guard)
        previous_shape=geom(cfg,before)
        def capture(name,phi):
            nonlocal previous_shape
            path=OUT/f"{case}_{state}_{name}.npz"
            np.savez_compressed(path,phi=phi,x=grid.x,y=grid.y)
            shape=geom(cfg,phi)
            row["stages"].append({"stage":name,"metal_nodes":int(np.count_nonzero(phi<0)),
                "components_4":int(ndimage.label(phi<0)[1]),"cad_area_mm2":shape.area,
                "symmetric_difference_previous_mm2":shape.symmetric_difference(previous_shape).area,
                "hausdorff_previous_mm":shape.boundary.hausdorff_distance(previous_shape.boundary),
                "snapshot":path.name})
            previous_shape=shape
        trial=ls.copy()
        trial.phi=a.advect_levelset(trial.phi,V,cfg.optimizer.pseudo_time_step,grid.dx,cfg.optimizer.cfl)
        capture("HJ",trial.phi)
        reinit=a.LevelSet.reinit
        def traced_reinit(obj):
            capture("pre_reinit1",obj.phi)
            reinit(obj)
            capture("post_reinit1",obj.phi)
        a.LevelSet.reinit=traced_reinit
        try:
            a.apply_constraints(trial,force_metal,force_air,anchor)
        finally:
            a.LevelSet.reinit=reinit
        candidate.phi=trial.phi.copy()
        candidate.phi[pinned]=original.phi[pinned]
        apply_min_gap(candidate,cfg)
        apply_min_feature(candidate,cfg.constraints.min_feature_mm,force_air=force_air,force_metal=force_metal)
        candidate.phi[pinned]=original.phi[pinned]
        capture("pre_bridge",candidate.phi)
        if guard:
            assert guard.apply(candidate,log=row["repair_log"].append)
        capture("post_bridge",candidate.phi)
        trial.phi=candidate.phi.copy()
        trial.reinit()
        trial.phi[pinned]=original.phi[pinned]
        capture("post_reinit2",trial.phi)
        if guard:
            mask[:]=build_velocity_mask(candidate,cfg,taper_mm=1,frozen=guard.frozen)
            mask[pinned]=0
        row["saved_phi_max_error"]=float(np.max(abs(trial.phi-expected)))
        row["replay_exact"]=bool(np.allclose(trial.phi,expected,rtol=0,atol=1e-10))
        if guard:
            row["frozen_nodes_after"]=int(guard.frozen.sum())
        if case=="divider" and state==1:
            alternative=ls.copy()
            sub_candidate=LevelSet2D(original.box,original.dx)
            count=int(np.ceil(cfg.optimizer.pseudo_time_step*np.max(abs(V))/(cfg.optimizer.cfl*grid.dx)))
            repair_log=[]
            for _ in range(count):
                velocity=V*(~guard_before.frozen)
                alternative.phi=a.advect_levelset(alternative.phi,velocity,
                    cfg.optimizer.pseudo_time_step/count,grid.dx,cfg.optimizer.cfl)
                sub_candidate.phi=alternative.phi.copy()
                sub_candidate.phi[pinned]=original.phi[pinned]
                assert guard_before.apply(sub_candidate,log=repair_log.append)
                alternative.phi=sub_candidate.phi.copy()
            a.apply_constraints(alternative,force_metal,force_air,anchor)
            sub_candidate.phi=alternative.phi.copy()
            sub_candidate.phi[pinned]=original.phi[pinned]
            apply_min_gap(sub_candidate,cfg)
            apply_min_feature(sub_candidate,cfg.constraints.min_feature_mm,force_air=force_air,force_metal=force_metal)
            assert guard_before.apply(sub_candidate,log=repair_log.append)
            alternative.phi=sub_candidate.phi.copy()
            alternative.reinit()
            alternative.phi[pinned]=original.phi[pinned]
            np.savez_compressed(OUT/"divider_substep_guard.npz",phi=alternative.phi,
                frozen=guard_before.frozen,x=grid.x,y=grid.y)
            alt_shape=geom(cfg,alternative.phi)
            row["same_dt_substep_guard_diagnostic"]={"substeps":count,"repair_log":repair_log,
                "frozen_nodes":int(guard_before.frozen.sum()),"area_mm2":alt_shape.area,
                "symmetric_difference_actual_mm2":alt_shape.symmetric_difference(geom(cfg,expected)).area,
                "note":"offline diagnostic only; no CST objective evaluated"}
        rows.append(row)
    return rows

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    report={"analytic":analytic_checks(),"replay":{case:replay(case) for case in ("divider","coupler")}}
    assert all(r["replay_exact"] for rows in report["replay"].values() for r in rows),report
    write("offline_checks.json",report)
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
