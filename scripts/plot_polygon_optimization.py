"""Plot committed Maxwell/CAD iterates; keep solver-refinement phases distinct."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'results/polygon_optimization_2026-11-13'


def main():
    first = json.loads((OUT/'results.json').read_text())['cases'][0]
    assert first['target'] == 'S11'
    first_history = first['history']
    phases = [('7 layers: initial check', first_history,
               [first_history[0]['f_before']]+[r['f_after'] for r in first_history])]
    status = []
    folders = [('long_S11', '9 layers'), ('long_S11_c13_i0.8_r0.5', '13 layers: refined spacing')]
    tapered = 'long_S11_c17_i0.8_r0.5_t4'
    if (OUT/tapered/'results.json').exists():
        folders.append((tapered, '17 layers: tapered spacing'))
    for directory, label in folders:
        data = json.loads((OUT/directory/'results.json').read_text())
        history, states = data['history'], data['states']
        committed = [r for r in history if r['updated']]
        assert len(states) == len(committed)+1
        np.testing.assert_allclose([s['value'] for s in states[1:]], [r['f_after'] for r in committed])
        phases.append((label, committed, [s['value'] for s in states]))
        status.append({'phase': label, 'status': data['status'], 'committed_updates': len(committed),
                       'error': data.get('error'), 'final_value': states[-1]['value']})
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3))
    offset = 0
    for label, history, values in phases:
        indices = np.arange(len(values))+offset
        axes[0].plot(indices, values, '.-', label=label)
        ratios = [r['objective_gain']/r['linearized_signed_work'] for r in history]
        axes[1].plot(indices[1:], ratios, '.-', label=label)
        offset += len(history)
    axes[0].set(xlabel='Committed CAD updates', ylabel='|S11|', title='Independent Maxwell reference')
    axes[1].axhline(1., color='gray', linewidth=.8)
    axes[1].set(xlabel='Committed CAD updates', ylabel='Actual gain / first-order prediction',
                title='Finite-step agreement')
    for ax in axes:
        ax.grid(alpha=.25)
        ax.legend(fontsize=8)
    fig.suptitle('Solver refinement phases; rejected trials excluded; convergence not certified', fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT/'committed_progress.png', dpi=160)
    plt.close(fig)
    (OUT/'plot_snapshot.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
    print(json.dumps(status), flush=True)


if __name__ == '__main__':
    main()
