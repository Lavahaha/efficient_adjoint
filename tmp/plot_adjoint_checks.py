"""Scientific figures from saved diagnostic measurements; no solver calls."""
import json
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eaopt.config import CaseConfig
from eaopt.geometry.design import compose_metal_regions

OUT=ROOT/"results/adjoint_checks_2026-11-01"
q=json.loads((OUT/"qtem_analysis.json").read_text(encoding="utf-8"))
fig,ax=plt.subplots(figsize=(8,4),layout="constrained")
labels=["Forward E","Backward E","Eq.25 gradient","Repeat gradient"]
for j,case in enumerate(("divider","coupler")):
    d=q[case]
    values=[d["field_errors"]["fwd"]["E_3D_relative"],d["field_errors"]["bwd"]["E_3D_relative"],
        d["gradient"]["relative_L2"],d["repeat_noise"]["gradient_relative"]]
    ax.bar(np.arange(4)+(j-.5)*.35,values,width=.35,label=case)
ax.set_xticks(np.arange(4),labels)
ax.set_yscale("log"); ax.set_ylim(1e-8,1e-4)
ax.set_ylabel("Relative L2 difference")
ax.set_title("One-mode vs two-mode changes and repeat-solve noise")
ax.legend(); ax.grid(axis="y",alpha=.25)
fig.savefig(OUT/"qtem_vs_noise.png",dpi=180); plt.close(fig)

fig,axes=plt.subplots(2,2,figsize=(10,7),layout='constrained',sharey=True)
for row,case in enumerate(('divider','coupler')):
    for col,mode in enumerate((1,2)):
        with np.load(OUT/case/f'modes2/fwd/Port1_e{mode}.npz') as d:
            E=d['data'][0]
            u,z=d['y'],d['z']
            magnitude=np.linalg.norm(E,axis=-1)
            normalized=magnitude/np.max(magnitude)
        ax=axes[row,col]
        panel=ax.pcolormesh(u,z,np.log10(normalized.T+1e-8),vmin=-3,vmax=0,cmap='viridis',shading='auto')
        ax.axhline(0,color='white',ls='--',lw=.7)
        ax.axhline(-.762,color='white',ls=':',lw=.7)
        ax.set_title(f'{case}: port 1, mode {mode}')
        ax.set_xlabel('y (mm)'); ax.set_ylabel('z (mm)')
fig.colorbar(panel,ax=axes.ravel().tolist(),label='log10(|E| / maximum of each panel)')
fig.suptitle('Actual CST port eigenfields; separate magnitude normalization per panel')
fig.savefig(OUT/'qtem_port_modes.png',dpi=180); plt.close(fig)

cfg=CaseConfig.from_yaml(ROOT/"configs/divider.yaml")
source=ROOT/"results/divider_local_serial_smoke"
with np.load(source/"phi_before_iter_001.npz") as d:
    before=d["phi"]
stages=[("Before update 2",before),
    ("After HJ evolution",np.load(OUT/"algorithm/divider_1_HJ.npz")["phi"]),
    ("Before connectivity repair",np.load(OUT/"algorithm/divider_1_pre_bridge.npz")["phi"]),
    ("After repair and reinitialization",np.load(OUT/"algorithm/divider_1_post_reinit2.npz")["phi"])]
fig,axes=plt.subplots(2,2,figsize=(11,7),layout="constrained",sharex=True,sharey=True)
for ax,(title,phi) in zip(axes.ravel(),stages):
    for region in compose_metal_regions(cfg,phi):
        p=np.asarray(region.outer)
        ax.fill(p[:,0],p[:,1],color="0.25")
        for hole in region.holes:
            h=np.asarray(hole); ax.fill(h[:,0],h[:,1],color="white")
    ax.set_title(title)
    ax.set_xlim(5.5,34.2); ax.set_ylim(-9,9); ax.set_aspect("equal")
    ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm)"); ax.grid(alpha=.15)
fig.suptitle("Divider: exact replay of real-field update, fixed outer dt = 1e-8")
fig.savefig(OUT/"divider_update_stages.png",dpi=180); plt.close(fig)

drift=json.loads((OUT/'algorithm/reinit_actual_shapes.json').read_text(encoding='utf-8'))
fig,ax=plt.subplots(figsize=(7,4),layout='constrained')
for case in ('divider','coupler'):
    rows=drift[case]['after_update2']
    ax.plot([0]+[r['cycles'] for r in rows],[0]+[r['hausdorff_mm'] for r in rows],marker='o',label=case)
ax.set_xlabel('Reinitialization cycles (no HJ or EM solve)')
ax.set_ylabel('Contour Hausdorff difference from saved shape (mm)')
ax.set_title('Reinitialization alone changes real saved shapes')
ax.legend(); ax.grid(alpha=.25)
fig.savefig(OUT/'reinit_actual_shapes.png',dpi=180); plt.close(fig)

paths=[OUT/case/"fd_fine/directional_checks.json" for case in ("divider","coupler")]
if all(p.exists() and len(json.loads(p.read_text(encoding="utf-8"))["rows"])==4 for p in paths):
    fig,axes=plt.subplots(1,2,figsize=(10,4),layout="constrained")
    for ax,case,path in zip(axes,("divider","coupler"),paths):
        d=json.loads(path.read_text(encoding="utf-8"))
        names=list(dict.fromkeys(r["direction"] for r in d["rows"]))
        raw=np.array([next(r["adjoint_raw"] for r in d["rows"] if r["direction"]==name) for name in names])
        raw/=np.linalg.norm(raw)
        ax.bar(np.arange(2)-.3,raw,width=.2,label="Eq.25: grid sampling")
        direct=json.loads((OUT/'direct_boundary_checks.json').read_text(encoding='utf-8'))[case]
        dg=next(r['directional'] for r in direct['direct_predictions'] if r['z_mm']==0)
        dv=np.array([dg[name] for name in names]); dv/=np.linalg.norm(dv)
        ax.bar(np.arange(2)-.1,dv,width=.2,label="Eq.25: exact points")
        for j,h in enumerate((.04,.08)):
            fd=np.array([next(r["finite_difference_per_mm"] for r in d["rows"] if r["direction"]==name and r["h_mm"]==h) for name in names])
            fd/=np.linalg.norm(fd)
            ax.bar(np.arange(2)+.1+j*.2,fd,width=.2,label=f"FD h={h} mm")
        ax.set_xticks(np.arange(2),names); ax.set_ylim(-1.1,1.1); ax.axhline(0,color="black",lw=.8)
        ax.set_title(case); ax.set_ylabel("Directional coefficients / vector norm"); ax.legend(fontsize=7)
    fig.savefig(OUT/"directional_fd_comparison.png",dpi=180); plt.close(fig)

assert (OUT/"qtem_vs_noise.png").is_file() and (OUT/"divider_update_stages.png").is_file()
print("Diagnostic figures saved")
