"""Plot measured sign checks and external interpolation studies."""
from pathlib import Path
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

out = Path('results/adjoint_checks_2026-11-01/divider/sign_sampling_2026-11-02')
audit = json.loads((out/'root_cause_audit.json').read_text())['sampling'][0]['directional']
names = ['upper', 'lower', 'velocity', 'input_width']
fd = np.array([.0239746772, -.0508437084, -.1400203285, -.0141088429])
raw = np.array([audit[n]['bare_source_prediction'] for n in names])
source = np.array([audit[n]['objective_source_prediction'] for n in names])
signs = np.sign(np.vstack([raw, -raw, source, fd]))
agree = signs == np.sign(fd)[None, :]
fig, ax = plt.subplots(figsize=(8, 3.8))
ax.imshow(agree, cmap=matplotlib.colors.ListedColormap(['#f4b6af', '#b9dfcb']), vmin=0, vmax=1)
for row in range(4):
    for col in range(4):
        ax.text(col, row, '+' if signs[row, col]>0 else '-', ha='center', va='center', fontsize=20)
ax.set_xticks(range(4), ['Upper\ncontrol', 'Lower\ncontrol', 'Old\nvelocity', 'Input\nholdout'], fontsize=11)
ax.set_yticks(range(4), ['Bare reverse source', 'Global sign flip', 'Candidate phase factor', 'Measured FD'])
ax.set_title('Divider: direction signs (green agrees with measured FD)')
fig.tight_layout()
fig.savefig(out/'sign_root_cause.png', dpi=180)
plt.close(fig)

fig, ax = plt.subplots(figsize=(8, 4.2))
for prefix, label in [('', 'z=0 interface'), ('substrate_', 'z=-0.1 substrate'), ('air_', 'z=+0.1 air')]:
    p = out/f'{prefix}sampling_convergence.json'
    if not p.exists():
        continue
    rows = json.loads(p.read_text())['rows']
    ax.plot(range(len(rows)), [100*r['complex_sensitivity_relative'] for r in rows], 'o-', label=label)
ax.set_xticks(range(6), ['XYZ .2/.1', 'XY only .2', 'Z only .1', 'XYZ .1/.05', 'XYZ .05/.025', 'XYZ .025/.0125'], rotation=20)
ax.set_ylabel('Complex Q relative difference (%)')
ax.set_title('External interpolation vs native same-point values; same EM mesh')
ax.grid(alpha=.25)
ax.legend()
fig.tight_layout()
fig.savefig(out/'sampling_convergence.png', dpi=180)
plt.close(fig)
print('Wrote sign_root_cause.png and sampling_convergence.png')
