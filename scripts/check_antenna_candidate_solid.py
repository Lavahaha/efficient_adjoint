"""Audit a rejected antenna contour in CST without an optimization update."""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace
from unittest.mock import patch
import numpy as np
from shapely.geometry import Polygon

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eaopt.config import CaseConfig
from eaopt.geometry.design import compose_metal_regions,region_rings
from eaopt.solver.case import load_case
from eaopt.solver.cst import CstSolver,_save
from eaopt.cli import make_log,safe_console


def main(source):
    safe_console()
    source=Path(source).resolve()
    phi=np.load(source)['phi']
    cfg=CaseConfig.from_yaml(ROOT/'configs/antenna.yaml')
    with patch('eaopt.geometry.design.contour_point_limit',return_value=np.inf):
        regions=compose_metal_regions(cfg,phi)
    if len(regions)!=1 or regions[0].holes:
        raise ValueError('This diagnostic audits one solid contour without holes')
    rings=region_rings(regions)
    expected=Polygon(regions[0].outer)
    out=ROOT/'results'/('antenna_solid_audit_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    out.mkdir()
    cfg.output.dir=str(out)
    setup,model=load_case(cfg)
    setup=replace(setup,solver_type='fd')
    log=make_log(out/'run.log')
    solver=CstSolver(cfg,setup=setup,model=model,force_new=True,auto_init=True,point_sampling=True,log=log)
    report=dict(source=str(source),contour_vertices=len(rings[0]),scope='native entity audit only; local point-budget bypass; no optimization or field solve',status='running')
    def save(): (out/'results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        solver.begin_iteration(0)
        with patch('eaopt.solver.cst.contour_point_limit',return_value=np.inf):
            solver.build_model(rings)
        project=solver.projects['fwd']
        native=out/'native_solid.txt'
        code=('Sub Main\nDim a As Double,b As Double,c As Double,d As Double,e As Double,f As Double\n'
            'Dim ok As Boolean\nOpen "'+str(native)+'" For Output As #1\n'
            'Print #1, Solid.GetVolume("design_region:antenna_metal_0")\n'
            'Print #1, Solid.GetNumberOfPoints("design_region:antenna_metal_0")\n'
            'ok=Solid.GetLooseBoundingBoxOfShape("design_region:antenna_metal_0",a,b,c,d,e,f)\n'
            'If Not ok Then Err.Raise 1, , "Bounding box unavailable"\n'
            'Print #1,a\nPrint #1,b\nPrint #1,c\nPrint #1,d\nPrint #1,e\nPrint #1,f\nClose #1\nEnd Sub')
        project.model3d._execute_vba_code(code)
        data=list(map(float,native.read_text().split()))
        volume=expected.area*setup.metal_thickness_mm
        xmin,ymin,xmax,ymax=expected.bounds
        bounds=[xmin,xmax,ymin,ymax,0.,setup.metal_thickness_mm]
        np.testing.assert_allclose(data[0],volume,rtol=1e-8,atol=1e-9)
        np.testing.assert_allclose(data[2:],bounds,rtol=0,atol=1e-9)
        _save(project,solver.project_path('fwd'),log)
        report.update(status='passed',native_volume_mm3=data[0],expected_volume_mm3=volume,
            native_vertices=int(data[1]),native_bounds_mm=data[2:],expected_bounds_mm=bounds)
    except BaseException as error:
        report.update(status='failed',error=repr(error))
        raise
    finally:
        save()
        solver.close()
    print(out)


if __name__=='__main__': main(sys.argv[1])
