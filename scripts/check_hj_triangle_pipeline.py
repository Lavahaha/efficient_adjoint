"""Replay the existing HJ stages with one consistent triangular geometry map."""
import json
from pathlib import Path
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adjoint_levelset as als
from eaopt.geometry.implicit import reconstruct_triangular_levelset
from eaopt.geometry.state import CadGeometryState
import check_antenna_geometry_consistency as d


def reconstruct(phi):
    return reconstruct_triangular_levelset(d.INITIAL.xs,d.INITIAL.ys,phi)


def main():
    base_phi=d.INITIAL.phi.copy()
    base=reconstruct(base_phi)
    xx,_=d.GRID.mesh()
    nx,_=als.LevelSet(d.GRID,base_phi).normals_grid()
    speed=(xx+20)/20*nx
    vmax=float(abs(speed).max())
    records=[]
    for h in (0.,.01,-.01,.001,-.001,1e-5,-1e-5,1e-7,-1e-7):
        stages=d.stages(base_phi,speed*(1 if h>=0 else -1),abs(h)*vmax)
        raw=stages['advection']
        reinit=als.LevelSet(d.GRID,raw)
        reinit.reinit()
        stages['reinitialization_only']=reinit.phi
        matched=als.LevelSet(d.GRID,raw)
        matched.reinit(preserve_cell_fans=True)
        stages['matched_reinitialization']=matched.phi
        # Research projection for this antenna (no connectivity/gap constraint).
        # HJ remains the updater; this CAD helper supplies polygon filtering only.
        raw_geometry=reconstruct(raw)
        state=CadGeometryState(raw_geometry,d.INITIAL.xs,d.INITIAL.ys)
        filtered=state.filter_features(d.CFG.constraints.min_feature_mm)
        projected=als.LevelSet(d.GRID,raw if filtered.geometry.equals(raw_geometry) else filtered.phi)
        projected.phi[d.PINNED]=base_phi[d.PINNED]
        projected.reinit(preserve_cell_fans=True)
        stages['polygon_filter_matched_reinit']=projected.phi
        row={'h_mm':h,'stages':{}}
        advected=reconstruct(raw)
        for name,phi in stages.items():
            g=reconstruct(phi)
            row['stages'][name]=dict(area_mm2=g.area,
                area_difference_quotient_mm=None if not h else (g.area-base.area)/h,
                boundary_distance_from_base_mm=base.boundary.hausdorff_distance(g.boundary),
                extra_change_from_advection_mm2=g.symmetric_difference(advected).area,
                components=1 if g.geom_type=='Polygon' else len(g.geoms),
                holes=len(g.interiors) if g.geom_type=='Polygon' else sum(len(p.interiors) for p in g.geoms))
        records.append(row)
    # A field with curved, non-grid-aligned interfaces tests whether the
    # existing reinitialiser preserves center-to-corner crossings as well.
    g=als.Grid2D(61,61,.1,-3.,-3.)
    x,y=g.mesh()
    phi=(np.hypot(x+.037,y-.021)-1.6)*(1+.2*np.cos(x+2*y))
    first=reconstruct_triangular_levelset(g.x,g.y,phi)
    ls=als.LevelSet(g,phi)
    ls.reinit()
    last=reconstruct_triangular_levelset(g.x,g.y,ls.phi)
    matched=als.LevelSet(g,phi)
    matched.reinit(preserve_cell_fans=True)
    last_matched=reconstruct_triangular_levelset(g.x,g.y,matched.phi)
    report=dict(scope='unchanged HJ, existing constraints and reinitialization; research triangle extraction only',
                initial_area_mm2=base.area,exact_continuous_length_area_derivative_mm=20,
                records=records,reinit_curved_field=dict(
                    changed_area_mm2=first.symmetric_difference(last).area,
                    boundary_drift_mm=first.boundary.hausdorff_distance(last.boundary)),
                matched_reinit_curved_field=dict(changed_area_mm2=first.symmetric_difference(last_matched).area,
                    boundary_drift_mm=first.boundary.hausdorff_distance(last_matched.boundary)))
    out=ROOT/'results/hj_geometry_consistency_2026-11-15'
    out.mkdir(exist_ok=True)
    (out/'triangle_pipeline.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
