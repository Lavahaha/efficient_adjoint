"""Export the actual port eigenfields for physical mode/reference matching."""
import json
from pathlib import Path
import sys

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eaopt.solver.cst import _connect,_open_or_reuse
from eaopt.solver import cst_results as R

OUT=ROOT/"results/adjoint_checks_2026-11-01"

def log(s):
    print(s,flush=True)
    with (OUT/"port_export.log").open("a",encoding="utf-8") as f:
        f.write(str(s)+"\n")

def main():
    de=None
    try:
        de=_connect(False,True,log,quiet=True)
        for case in ("divider","coupler"):
            for variant in (("fd_fine/base",) if "--fine" in sys.argv else ("modes1","modes2")):
                folder=OUT/case/variant/"fwd"
                tree=json.loads((folder/"field_tree.json").read_text(encoding="utf-8"))
                items=[v for v in tree if "\\Port Modes\\" in v and v.rsplit("\\",1)[-1] in ("e1","e2","h1","h2")]
                assert items
                project=_open_or_reuse(de,folder/"model.cst",log)
                try:
                    for item in items:
                        parts=item.split("\\")
                        dest=folder/f"{parts[-2]}_{parts[-1]}.npz"
                        if dest.exists():
                            continue
                        scratch=dest.with_suffix(".txt")
                        if not scratch.exists():
                            R._select_item(project.model3d,item)
                            R._configure_and_run(project.model3d.ASCIIExport,scratch,.1,"port field",item,step_z_mm=.1)
                        table=np.loadtxt(scratch,skiprows=2)
                        assert table.ndim==2 and table.shape[1]==9
                        axes=[np.unique(table[:,j]) for j in range(3)]
                        assert np.prod([len(v) for v in axes])==len(table)
                        index=tuple(np.searchsorted(axes[j],table[:,j]) for j in range(3))
                        data=np.zeros(tuple(len(v) for v in axes)+(3,),complex)
                        data[index]=table[:,3::2]+1j*table[:,4::2]
                        assert np.isfinite(data).all()
                        np.savez_compressed(dest,data=data,x=axes[0],y=axes[1],z=axes[2])
                        scratch.unlink()
                        log(f"EXPORTED {dest.relative_to(OUT)} {data.shape}")
                finally:
                    project.close()
    finally:
        if de is not None:
            de.close()

if __name__=="__main__":
    main()
