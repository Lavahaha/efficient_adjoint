"""Compare eigenmode voltage orientation using one common strip/ground path."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/adjoint_checks_2026-11-01'
report={}
for case in ('divider','coupler'):
    cfg=CaseConfig.from_yaml(ROOT/f'configs/{case}.yaml')
    setup,_=load_case(cfg)
    report[case]={}
    for variant in ('modes2','fd_fine/base'):
        folder=OUT/case/variant/'fwd'
        rows={}
        for port in setup.ports:
            path=folder/f'Port{port}_e1.npz'
            if not path.exists():
                continue
            with np.load(path) as d:
                axes=[d[k] for k in ('x','y','z')]
                horizontal=next(j for j in (0,1) if len(axes[j])>1)
                u=axes[horizontal]
                i=int(np.argmin(abs(u-.5*(u[0]+u[-1]))))
                Ez=(d['data'][i,0,:,2] if horizontal==0 else d['data'][0,i,:,2])
                z=axes[2]
                zi=np.r_[-.762,z[(z>-.762)&(z<0)],0.]
                ez=np.interp(zi,z,Ez.real)+1j*np.interp(zi,z,Ez.imag)
                voltage=-np.trapezoid(ez,zi)*1e-3
                assert abs(voltage)>1e-10
                rows[str(port)]={'path_axis':('x','y')[horizontal],
                    'path_horizontal_mm':float(u[i]),'voltage_real':float(voltage.real),
                    'voltage_imag':float(voltage.imag),'phase_deg':float(np.angle(voltage,deg=True))}
        if rows:
            ref=complex(rows['1']['voltage_real'],rows['1']['voltage_imag'])
            for value in rows.values():
                voltage=complex(value['voltage_real'],value['voltage_imag'])
                value['phase_relative_port1_deg']=float(np.angle(voltage/ref,deg=True))
            report[case][variant]=rows
(OUT/'port_voltage_references.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
