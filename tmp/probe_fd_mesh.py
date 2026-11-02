"""Build meshes without time-domain solves before accepting an FD setup."""
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run_qtem_checks as em
from run_directional_fd_checks import boundary_and_directions
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst import _connect,_apply_blocks,_save

BLOCK='''With MeshSettings
 .SetMeshType "Hex"
 .Set "Version", 1
 .Set "UseMaxNumFacesForSnapping", "1"
 .Set "MaxNumFacesForSnapping", "1"
End With
With Mesh
 .Automesh "False"
 .MinimumStepNumber "120"
 .LinesPerWavelength "20"
End With
Solid.SetMeshStepWidth "component1:substrate", "0.2", "0.2", "0.1"
Solid.SetMeshStepWidth "component1:air", "0.4", "0.4", "0.4"
'''

def main():
    de=_connect(False,True,em.log,quiet=True)
    try:
        for case in ('divider','coupler'):
            cfg=CaseConfig.from_yaml(em.ROOT/f'configs/{case}.yaml')
            setup,model=load_case(cfg)
            b,ds=boundary_and_directions(cfg)
            meshes=[]
            for tag,h in [('base',0),('plus',.04)]:
                folder=em.OUT/case/'mesh_probe_fine'/tag
                folder.mkdir(parents=True,exist_ok=True)
                p=de.new_mws()
                try:
                    pts=b.pts+h*next(iter(ds.values()))[:,None]*b.normals
                    blocks=model.template_blocks(cfg.name,1)
                    blocks.append(('diagnostic CAD',model.design_region_update([np.vstack([pts,pts[0]])],setup.metal_thickness_mm,material='PEC')))
                    blocks.append(('mesh preflight',BLOCK))
                    _apply_blocks(p.model3d,blocks,what=tag,log=em.log)
                    _save(p,folder/'model.cst',em.log)
                    p.model3d.add_to_history('generate mesh only','Mesh.Update')
                    _save(p,folder/'model.cst',em.log)
                    found=list((folder/'model').rglob('Model.msh'))
                    em.log(f'MESH_FILES {found}')
                    if found:
                        data=found[0].read_bytes()
                        (folder/'Model.msh').write_bytes(data)
                        meshes.append(data)
                        em.log(f'MESH {case} {tag} bytes={len(data)} counts={np.frombuffer(data,dtype="<i4",count=5).tolist()}')
                finally:
                    p.close()
            em.log(f'MESH_EQUAL {case} {len(meshes)==2 and meshes[0]==meshes[1]}')
    finally:
        de.close()

if __name__=='__main__':
    main()
