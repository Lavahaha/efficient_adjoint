"""Frozen-CAD one/two-mode comparisons using the real CST API."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import traceback

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst import _apply_blocks, _connect, _open_or_reuse, _run_solver, _save
from eaopt.solver import cst_results as R

OUT = ROOT / "results/adjoint_checks_2026-11-01"
OUT.mkdir(parents=True, exist_ok=True)

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def log(message):
    print(message, flush=True)
    with (OUT / "qtem.log").open("a", encoding="utf-8") as stream:
        stream.write(str(message)+"\n")

def s_indices(leaf):
    match = re.fullmatch(r"S(\d+)(?:\((\d+)\))?,(\d+)(?:\((\d+)\))?", leaf)
    return tuple(int(v or 1) for v in match.groups()) if match else None

assert s_indices("S2,1") == (2,1,1,1)
assert s_indices("S2(2),1(1)") == (2,2,1,1)

def read_results(path, stimulus):
    rm = R._result_module(path, True)
    tree = rm.get_tree_items()
    values, info = [], {}
    for item in tree:
        leaf = item.rsplit("\\",1)[-1]
        indices = s_indices(leaf) if "\\S-Parameters\\" in item else None
        if indices and indices[2:] == (stimulus,1):
            value = R._read_curve(rm, item, 5.0, [], log)
            values.append([*indices, value.real, value.imag])
        if any(f"\\{name}\\" in item for name in (
                "Line Impedance", "Wave Impedance", "Effective Dielectric Constant", "Gamma")):
            curve=rm.get_result_item(item)
            x=np.asarray(curve.get_xdata(),dtype=float).ravel()
            y=np.asarray(curve.get_ydata(),dtype=complex).ravel()
            assert len(x)==len(y) and len(x)>0
            assert x.min()<=5.0<=x.max() or (len(x)==1 and abs(x[0]-5.0)<1e-9)
            value=complex(np.interp(5.0,x,y.real),np.interp(5.0,x,y.imag))
            info[item] = [value.real,value.imag]
    assert values, tree
    return {"coefficients":values,"port_info":info,"tree":tree}

def run_project(de, cfg, setup, model, folder, mode_count, tag,
                *, polygons=None, export_fields=True, extra_blocks=(), clone_from=None):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "model.cst"
    stim = setup.stimulus(tag,cfg.objective)
    initial = unary_union([Polygon(p.vertices) for p in cfg.initial_metal])
    assert initial.is_valid and initial.geom_type == "Polygon" and not initial.interiors
    polys = [np.asarray(initial.exterior.coords)] if polygons is None else polygons
    assert all(Polygon(p).is_valid and Polygon(p).area>0 for p in polys)
    blocks = model.template_blocks(cfg.name,stim)
    changed = [(title,code.replace('.NumberOfModes "1"',f'.NumberOfModes "{mode_count}"'))
        for title,code in blocks]
    assert all(a==b for a,b in zip(blocks,changed) if not a[0].startswith("Port "))
    changed.append(("frozen initial YAML union" if polygons is None else "diagnostic CAD",model.design_region_update(polys,
        setup.metal_thickness_mm,material="PEC")))
    changed.extend(extra_blocks)
    metadata={"config":asdict(cfg),"setup":asdict(setup),"mode_count":mode_count,
        "stimulus_port":stim,"stimulus_mode":1,"blocks":changed,
        "shape":[p.tolist() for p in polys]}
    metadata=json.loads(json.dumps(metadata,ensure_ascii=False))
    config_path=folder/"config.json"
    if config_path.exists():
        assert json.loads(config_path.read_text(encoding="utf-8"))==metadata
    else:
        write(config_path,metadata)
    if (folder/"complete.json").exists():
        log(f"REUSE complete {folder.relative_to(OUT)}")
        return
    project = None
    try:
        if path.exists():
            project=_open_or_reuse(de,path,log)
        elif clone_from is not None:
            clone_from=Path(clone_from).resolve()
            source=json.loads((clone_from.parent/"config.json").read_text(encoding="utf-8"))
            for key in ("config","setup","mode_count","stimulus_port","stimulus_mode"):
                assert source[key]==metadata[key], ("Clone settings differ",key)
            assert polygons is not None
            project=_open_or_reuse(de,clone_from,log)
            _save(project,path,log,what="Clone solved baseline")
            update=model.design_region_update(polys,setup.metal_thickness_mm,material="PEC")
            _apply_blocks(project.model3d,[("FD CAD perturbation",update)]+list(extra_blocks),what="FD clone update",log=log)
            _save(project,path,log)
            write(folder/"clone_origin.json",{"source":str(clone_from),
                "source_mesh_sha256":hashlib.sha256((clone_from.parent/"Model.msh").read_bytes()).hexdigest(),
                "applied_update":update})
        else:
            project=de.new_mws()
            _apply_blocks(project.model3d,changed,what=str(folder.relative_to(OUT)),log=log)
            _save(project,path,log)
        if not (folder/"solve_done.json").exists():
            seconds=_run_solver(project.model3d,log)
            _save(project,path,log)
            write(folder/"solve_done.json",{"seconds":seconds})
        result=read_results(path,stim)
        assert {v[0] for v in result["coefficients"] if v[1]==1}==set(setup.ports)
        write(folder/"s_params.json",result)
        if export_fields:
            live_tree=R._tree_items(project.model3d)
            write(folder/"field_tree.json",live_tree)
        step_xy,step_z=setup.resolve_export_steps(cfg.field_export_step_mm)
        for kind in ("Efield","Hfield"):
            if not export_fields:
                continue
            dest=folder/f"{kind}.npz"
            if dest.exists():
                continue
            scratch=folder/f"{kind}.txt"
            field=R.export_field_cropped(project.model3d,kind,setup.frequency_ghz,
                step_xy,scratch,cfg.design_region.box,1.0,step_z_mm=step_z,log=log)
            assert np.isfinite(field.data).all()
            np.savez_compressed(dest,origin=field.origin,spacing=field.spacing,data=field.data)
            scratch.unlink()
        result_dir=path.with_suffix("")/"Result"
        hashes={}
        for name in ("Model.log","MCalc.log","Model.msh","simulation_overview.json"):
            source=result_dir/name
            if source.exists():
                shutil.copy2(source,folder/name)
                hashes[name]=hashlib.sha256(source.read_bytes()).hexdigest()
        for source in result_dir.glob("Port*.prt"):
            shutil.copy2(source,folder/source.name)
        write(folder/"complete.json",{"status":"complete","hashes":hashes})
        log(f"COMPLETE {folder.relative_to(OUT)} S={result['coefficients']}")
    finally:
        if project is not None:
            project.close()

def main():
    de=None
    try:
        de=_connect(False,True,log,quiet=True)
        for case in ("divider","coupler"):
            cfg=CaseConfig.from_yaml(ROOT/f"configs/{case}.yaml")
            setup,model=load_case(cfg)
            for modes in (1,2):
                for tag in ("fwd","bwd"):
                    run_project(de,cfg,setup,model,OUT/case/f"modes{modes}"/tag,modes,tag)
            run_project(de,cfg,setup,model,OUT/case/"repeat1"/"fwd",1,"fwd")
        write(OUT/"qtem_status.json",{"status":"complete","solves":10})
    except BaseException:
        write(OUT/"qtem_status.json",{"status":"failed","traceback":traceback.format_exc()})
        raise
    finally:
        if de is not None:
            de.close()

if __name__=="__main__":
    main()
