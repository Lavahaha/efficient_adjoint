"""Compare real fields, boundary sensitivities, noise and port eigenfields."""
import json
from pathlib import Path
import sys

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/"tmp"))
import adjoint_levelset as a
from check_iteration_algorithm import load_bundle,sensitivity
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import make_level_set
from eaopt.geometry.design import connection_boundary_mask
from eaopt.runner import _movement_constraints
from eaopt.solver.case import load_case
from eaopt.solver import cst_results as R

OUT=ROOT/"results/adjoint_checks_2026-11-01"

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def coefficients(folder):
    return {tuple(r[:4]):complex(*r[4:]) for r in read(folder/"s_params.json")["coefficients"]}

def relative(x,y):
    return float(np.linalg.norm(y-x)/np.linalg.norm(x))

def compare(case):
    root=OUT/case
    cfg=CaseConfig.from_yaml(ROOT/f"configs/{case}.yaml")
    setup,model=load_case(cfg)
    original=make_level_set(cfg)
    ls=a.LevelSet(a.Grid2D(original.nx,original.ny,original.dx,original.xs[0],original.ys[0]),original.phi)
    mask,*_=_movement_constraints(original,cfg)
    pinned=connection_boundary_mask(original,cfg,model.fixed_metal_polygons())
    mask[pinned]=0
    bundles={mode:{tag:load_bundle(root/f"modes{mode}"/tag) for tag in ("fwd","bwd")} for mode in (1,2)}
    result={"field_errors":{},"s_changes":{},"mesh_equal":{},"mode_reference":{},"CST_balance":{}}
    for tag in ("fwd","bwd"):
        f1=root/"modes1"/tag; f2=root/"modes2"/tag
        result["mesh_equal"][tag]=(f1/"Model.msh").read_bytes()==(f2/"Model.msh").read_bytes()
        s1=coefficients(f1); s2=coefficients(f2)
        result["s_changes"][tag]={str(key):{"relative_complex":float(abs(s2[key]-s)/abs(s)),
            "db_difference":float(20*np.log10(abs(s2[key])/abs(s)))} for key,s in s1.items()}
        result["field_errors"][tag]={}
        for component,kind in (("E","Efield"),("H","Hfield")):
            with np.load(f1/f"{kind}.npz") as x,np.load(f2/f"{kind}.npz") as y:
                assert np.array_equal(x["origin"],y["origin"]) and np.array_equal(x["spacing"],y["spacing"])
                result["field_errors"][tag][component+"_3D_relative"]=relative(x["data"],y["data"])
        for count,folder in ((1,f1),(2,f2)):
            rm=R._result_module(folder/"model.cst",False)
            for item in rm.get_tree_items():
                if "\\Balance\\" in item:
                    value=R._read_curve(rm,item,5,[],lambda _:None)
                    result["CST_balance"][f"modes{count}_{tag}:{item}"]=[value.real,value.imag]
    gradients={}
    velocities={}
    for mode in (1,2):
        boundary,g=sensitivity(case,ls,mask,bundles[mode]["fwd"],bundles[mode]["bwd"])
        gradients[mode]=g
        velocities[mode]=a.boundary_to_grid_velocity(ls,boundary,g,mask,
            cfg.level_set.extension_band_mm/original.dx,smooth_sigma_cells=1.5)
    g1,g2=gradients[1],gradients[2]
    important=boundary.active & (abs(g1)>max(abs(g1))*1e-3)
    result["gradient"]={"relative_L2":relative(g1,g2),
        "cosine":float(np.dot(g1,g2)/(np.linalg.norm(g1)*np.linalg.norm(g2))),
        "important_sign_disagreements":int(np.count_nonzero(g1[important]*g2[important]<0)),
        "important_points":int(important.sum()),"velocity_relative_L2":relative(velocities[1],velocities[2])}
    repeat=load_bundle(root/"repeat1/fwd")
    _,gr=sensitivity(case,ls,mask,repeat,bundles[1]["bwd"])
    s1=coefficients(root/"modes1/fwd"); sr=coefficients(root/"repeat1/fwd")
    target=(cfg.objective.to_port,1,cfg.objective.from_port,1)
    result["repeat_noise"]={"target_relative":float(abs(s1[target]-sr[target])/abs(s1[target])),
        "E_plane_relative":relative(bundles[1]["fwd"].E,repeat.E),
        "H_plane_relative":relative(bundles[1]["fwd"].H,repeat.H),"gradient_relative":relative(g1,gr)}
    for port in setup.ports:
        p1=root/f"modes1/fwd/Port{port}_e1.npz"
        p2=root/f"modes2/fwd/Port{port}_e1.npz"
        with np.load(p1) as x,np.load(p2) as y:
            assert all(np.array_equal(x[k],y[k]) for k in ("x","y","z"))
            e=x["data"].ravel(); f=y["data"].ravel()
            overlap=np.vdot(e,f)/(np.linalg.norm(e)*np.linalg.norm(f))
            result["mode_reference"][str(port)]={"overlap_abs":float(abs(overlap)),
                "phase_deg":float(np.angle(overlap,deg=True)),"norm_ratio":float(np.linalg.norm(f)/np.linalg.norm(e))}
    np.savez_compressed(root/"qtem_boundary_comparison.npz",points=boundary.pts,g1=g1,g2=g2,
        repeat_g=gr,V1=velocities[1],V2=velocities[2])
    return result

def main():
    report={case:compare(case) for case in ("divider","coupler")}
    (OUT/"qtem_analysis.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
