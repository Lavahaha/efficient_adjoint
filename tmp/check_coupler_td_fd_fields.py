"""Read native TD/FD eigenmode references and fields at identical physical points."""
from pathlib import Path
import json,sys,traceback
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eaopt.solver.cst import _connect,_open_or_reuse
from eaopt.solver import cst_results as R

root=Path('results/fd_single_2026-11-02')
out=root/'coupler_td_fd_field_reference'
out.mkdir(parents=True,exist_ok=True)
points=np.load(root/'coupler_planar_smoke/iter_000/boundary_fields.npz')['points_mm']
x,y=np.meshgrid(np.linspace(1,11,15),np.linspace(-2,0,7),indexing='ij')
air=np.column_stack([x.ravel(),y.ravel(),np.full(x.size,.5)])
de=_connect(True,False,print,quiet=True)
sources=[('fd_fwd',root/'coupler_probe/run_20261102T091050988480Z/cst/coupler_fwd.cst',1),
         ('td_fwd',Path('results/adjoint_checks_2026-11-01/coupler/modes2/fwd/model.cst'),1),
         ('td_bwd',Path('results/adjoint_checks_2026-11-01/coupler/modes2/bwd/model.cst'),3)]
report={}
try:
    for name,path,stim in sources:
        path=path.resolve()
        folder=out/name;folder.mkdir(exist_ok=True)
        project=_open_or_reuse(de,path,print)
        try:
            model=project.model3d
            tree=R._tree_items(model)
            (folder/'tree.json').write_text(json.dumps(tree,indent=2),encoding='utf-8')
            for label,xyz in [('boundary',points),('air',air)]:
                if (folder/f'{label}.npz').exists():
                    continue
                fields=[R.export_field_points(model,kind,5.,xyz,folder/f'{label}_{kind}.txt',stimulus_port=stim)
                        for kind in ('Efield','Hfield')]
                np.savez_compressed(folder/f'{label}.npz',points_mm=xyz,E=fields[0],H=fields[1])
            if name.startswith('fd'):
                sp=R.read_s_params(path,stim,log=print,exact_frequency=True)
            else:
                # The QTEM audit stores multimode curve names; use its native snapshot.
                saved=json.loads((path.parent/'s_params.json').read_text(encoding='utf-8'))
                sp={(int(r[0]),int(r[2])):complex(r[4],r[5]) for r in saved['coefficients']
                    if r[1]==r[3]==1 and r[2]==stim}
            values={}
            if name!='td_bwd':
                for port,center in [(1,(-5.6,1.8)),(3,(-.8,-7.))]:
                    items=[t for t in tree if '\\Port Modes\\' in t and t.endswith(f'\\Port{port}\\e1')]
                    if len(items)!=1:
                        raise ValueError(f'Port {port} paths: {items}')
                    scratch=(folder/f'port_{port}.txt').resolve()
                    pointfile=scratch.with_suffix('.points.txt')
                    z=np.linspace(-.762+1e-5,-1e-5,1001)
                    xyz=np.column_stack([np.full(len(z),center[0]),np.full(len(z),center[1]),z])
                    np.savetxt(pointfile,xyz,fmt='%.12g')
                    R._select_item(model,items[0])
                    model._execute_vba_code(f'''Sub Main
With ASCIIExport
 .Reset
 .Mode "FixedWidth"
 .UseSubvolume False
 .FileName "{scratch}"
 .SetPointFile "{pointfile}"
 .ExportCoordinatesInMeter False
 .Execute
End With
End Sub''')
                    table=np.atleast_2d(np.loadtxt(scratch,skiprows=2))
                    assert table.shape==(1001,9) and np.max(abs(table[:,:3]-xyz))<1e-5
                    voltage=-np.trapezoid(table[:,7]+1j*table[:,8],table[:,2])*1e-3
                    values[str(port)]=[float(voltage.real),float(voltage.imag)]
            report[name]=dict(source=str(path),s_params={f'S{i}{j}':[v.real,v.imag] for (i,j),v in sp.items()},port_voltage=values)
            (out/'status.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        finally:
            project.close()
    (out/'completion.json').write_text(json.dumps({'status':'complete','new_native_solver_jobs':0,
        'note':'All point exports completed; earlier reader failures remain in failure.json as attempt history.'},indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
except BaseException:
    (out/'failure.json').write_text(json.dumps({'traceback':traceback.format_exc()},indent=2),encoding='utf-8')
    raise
finally:
    de.set_quiet_mode(False)
