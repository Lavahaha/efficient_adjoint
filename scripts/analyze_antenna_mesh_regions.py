"""Attribute stored antenna field-integral changes to disjoint boundary regions."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from check_antenna_geometry_consistency import displacement

SOURCE = ROOT / 'results/antenna_corner_analysis_2026-11-10'
OUT = ROOT / 'results/antenna_mesh_attribution_2026-11-11/regional_fields'


def partition(xy):
    """Keep corner/feed neighborhoods separate from the remaining straight edges."""
    sides = np.full(len(xy), '', dtype='<U12')
    for name, axis, value, extra in (
        ('bottom', 1, -10., np.ones(len(xy), bool)),
        ('right', 0, 0., np.ones(len(xy), bool)),
        ('top', 1, 10., np.ones(len(xy), bool)),
        ('left_upper', 0, -20., xy[:, 1] > 2.5),
        ('left_lower', 0, -20., xy[:, 1] < -2.5)):
        mask = np.isclose(xy[:, axis], value, rtol=0, atol=1e-10) & extra
        assert not np.any(sides[mask] != '')
        sides[mask] = name
    assert np.all(sides != '')
    corners = np.array([[-20., -10.], [0., -10.], [0., 10.], [-20., 10.]])
    contacts = np.array([[-20., -2.5], [-20., 2.5]])
    near_corner = np.min(np.linalg.norm(xy[:, None] - corners, axis=2), axis=1) < 1.
    near_feed = np.min(np.linalg.norm(xy[:, None] - contacts, axis=2), axis=1) < 1.
    assert not np.any(near_corner & near_feed)
    regions = sides.copy()
    regions[near_corner] = 'corner_1mm'
    regions[near_feed] = 'feed_1mm'
    coordinate = np.where(np.isin(sides, ['top', 'bottom']), xy[:, 0]+20., xy[:, 1]+10.)
    bins = np.array([f'{side}:{int(np.floor(t))}' for side, t in zip(sides, coordinate)])
    return sides, regions, bins


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    cases = []
    for folder in ('mesh_field_comparison', 'adaptive_four_edge_fields'):
        metadata = json.loads((SOURCE / folder / 'results.json').read_text())
        assert metadata['completed']
        for row in metadata['evaluations']:
            cases.append((SOURCE / folder / f"{row['name']}.npz", row))
    ref = next(row for _, row in cases if row['name'] == 'adaptive_rectangle')
    reference_s = complex(ref['s_real'], ref['s_imag'])
    reference_weight = np.conj(reference_s) / abs(reference_s)
    records, arrays = [], {}
    common = None
    for path, row in cases:
        with np.load(path) as data:
            values = {key: data[key].copy() for key in data.files}
        xy, normals, weights = [values[key] for key in ('boundary', 'normals', 'weights')]
        if common is None:
            common = values
        for key in ('boundary', 'normals', 'weights', 'points'):
            np.testing.assert_array_equal(values[key], common[key])
        assert abs(weights.sum() - 75.) < 1e-10
        s = complex(values['S'])
        np.testing.assert_allclose(s, complex(row['s_real'], row['s_imag']), rtol=0, atol=1e-14)
        bnd = als.Boundary(xy, normals, weights, np.ones(len(xy), bool))
        ep, hp = als._boundary_field_products(bnd, values['E'], values['H'], values['E'], values['H'])
        qe = -1j * 2*np.pi*7.5e9 * als.EPS0 * ep
        qh = -1j * 2*np.pi*7.5e9 * als.MU0 * hp
        c = np.conj(s) / abs(s)
        ge, gh = np.real(c*qe), np.real(c*qh)
        g = ge + gh
        sides, regions, bins = partition(xy)
        masks = {name: regions == name for name in np.unique(regions)}
        assert np.all(np.sum(list(masks.values()), axis=0) == 1)
        predictions = []
        for direction in ('length', 'width', 'local'):
            vn = np.sum(displacement(xy, direction) * normals, axis=1)
            area_slope = float(np.sum(vn*weights))
            np.testing.assert_allclose(area_slope, 20. if direction != 'local' else 40./3,
                                       atol=2e-4, rtol=0)
            density = g*vn*weights
            expected = next(p['objective_slope'] for p in row['predictions']
                            if p['direction'] == direction)
            np.testing.assert_allclose(density.sum(), expected, rtol=1e-12, atol=1e-6)
            contributions = []
            for name, mask in masks.items():
                electric = float(np.sum(ge[mask]*vn[mask]*weights[mask]))
                magnetic = float(np.sum(gh[mask]*vn[mask]*weights[mask]))
                contributions.append(dict(region=name, arc_mm=float(weights[mask].sum()),
                    electric=electric, magnetic=magnetic, net=electric+magnetic))
            np.testing.assert_allclose(sum(p['net'] for p in contributions), expected,
                                       rtol=1e-12, atol=1e-6)
            predictions.append(dict(direction=direction, total=float(density.sum()),
                regions=contributions, electric_magnetic_cancellation=
                float(np.sum((np.abs(ge*vn)+np.abs(gh*vn))*weights)/max(abs(expected), 1e-30))))
        raw_regions = [dict(region=name, arc_mm=float(weights[mask].sum()),
            kernel_l1=float(np.sum(np.abs(g[mask])*weights[mask])),
            kernel_l2_squared=float(np.sum(g[mask]**2*weights[mask]))) for name, mask in masks.items()]
        records.append(dict(name=row['name'], mesh_cells=row['mesh_cells'],
                             predictions=predictions, raw_kernel_regions=raw_regions))
        arrays[row['name']] = dict(g=g, ge=ge, gh=gh,
                                   fixed_weight_g=np.real(reference_weight*(qe+qh)))

    comparisons = []
    for first, second in (('local1_subdivided', 'local0.75_subdivided'),
                          ('default_subdivided', 'local2_subdivided'),
                          ('adaptive_rectangle', 'adaptive_four_edges')):
        delta = arrays[second]['g'] - arrays[first]['g']
        directions = []
        for direction in ('length', 'width', 'local'):
            vn = np.sum(displacement(xy, direction)*normals, axis=1)
            weighted = delta*vn*weights
            bin_rows = [dict(bin=name, delta=float(weighted[bins == name].sum()))
                        for name in np.unique(bins)]
            bin_rows.sort(key=lambda p: abs(p['delta']), reverse=True)
            region_rows = [dict(region=name, delta=float(weighted[mask].sum()))
                           for name, mask in masks.items()]
            fixed_delta = arrays[second]['fixed_weight_g'] - arrays[first]['fixed_weight_g']
            directions.append(dict(direction=direction, delta=float(weighted.sum()),
                fixed_s_weight_delta=float(np.sum(fixed_delta*vn*weights)), regions=region_rows,
                top_1mm_bins=bin_rows[:8], top8_fraction_of_bin_absolute_change=
                sum(abs(p['delta']) for p in bin_rows[:8])/max(sum(abs(p['delta']) for p in bin_rows), 1e-30)))
        comparisons.append(dict(first=first, second=second, directions=directions))

    report = dict(scope='stored fields only; uncalibrated code units; no electromagnetic solves',
                  cases=records, comparisons=comparisons, disjoint_region_arc_mm={
                      name: float(weights[mask].sum()) for name, mask in masks.items()},
                  area_directional_self_checks_passed=True)
    (OUT / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    np.savez_compressed(OUT / 'densities.npz', boundary=xy, normals=normals, weights=weights,
        sides=sides, regions=regions, **{f'{name}__{key}': value
        for name, data in arrays.items() for key, value in data.items()})
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4), layout='constrained')
    for ax, side in zip(axes[:2], ('right', 'top')):
        mask = sides == side
        coord = xy[mask, 1 if side == 'right' else 0]
        order = np.argsort(coord)
        for name in ('local1_subdivided', 'local0.75_subdivided', 'adaptive_rectangle'):
            ax.plot(coord[order], arrays[name]['g'][mask][order]/1e6, label=name)
        ax.axhline(0, color='gray', ls=':')
        ax.set(xlabel='y (mm)' if side == 'right' else 'x (mm)', ylabel='Code kernel / 1e6',
               title=f'{side.capitalize()} boundary: identical sampling points')
        ax.legend(fontsize=7)
    names = [r['name'] for r in records]
    for direction in ('length', 'width', 'local'):
        axes[2].plot(range(len(names)), [next(p['electric_magnetic_cancellation']
            for p in r['predictions'] if p['direction'] == direction) for r in records], '-o', label=direction)
    axes[2].set_xticks(range(len(names)), ['default', '2 mm', '1 mm', '.75 mm', 'adapt 85', 'adapt 4'], rotation=20)
    axes[2].set(yscale='log', ylabel='Absolute E/H contributions / absolute net', title='Cancellation amplifies field errors')
    axes[2].legend(fontsize=8)
    for ax in axes:
        ax.grid(alpha=.25)
    fig.savefig(OUT / 'regional_attribution.png', dpi=170)
    plt.close(fig)
    print('Analyzed', len(records), 'stored solutions; partition and area self-checks passed.')
    for pair in comparisons:
        print(pair['first'], '->', pair['second'])
        for direction in pair['directions']:
            print(direction['direction'], 'delta=', direction['delta'],
                  'regions=', direction['regions'])


if __name__ == '__main__':
    main()
