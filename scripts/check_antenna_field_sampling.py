"""Read saved SLIM meshes and scan existing CST fields without new solves."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

import check_antenna_geometry_consistency as d

PREVIOUS = d.ROOT / 'results/antenna_mesh_attribution_2026-11-11'
OUT = d.ROOT / 'results/antenna_isolation_2026-11-11'


def read_slim(path):
    """Read only the four fixed-record blocks, validating their declared schema."""
    raw = Path(path).read_bytes()
    end = raw.index(b'\nend\n') + 5
    header = raw[:end].decode('ascii').splitlines()
    assert header[0] == 'SLIM' and header[1].startswith('format binary_little_endian 1.4:')
    types = dict(float64='<f8', int32='<i4', uint32='<u4')
    blocks, offset = {}, end
    for name in ('node', 'edge', 'triangle', 'tetrahedron'):
        index = next(i for i, line in enumerate(header) if line.startswith(f'element {name} '))
        count = int(header[index].split()[-1])
        fields = []
        for line in header[index+1:]:
            if not line.startswith('property '):
                break
            _, kind, field = line.split()
            fields.append((field, types[kind]))
        dtype = np.dtype(fields)
        assert offset + count*dtype.itemsize <= len(raw)
        blocks[name] = np.frombuffer(raw, dtype=dtype, count=count, offset=offset)
        offset += count*dtype.itemsize
    nodes = blocks['node']
    assert nodes.dtype.names == ('x', 'y', 'z', 'uid')
    xyz = np.column_stack([nodes[axis] for axis in ('x', 'y', 'z')])
    tet = blocks['tetrahedron']
    assert tet.dtype.names == ('node_index1', 'node_index2', 'node_index3', 'node_index4', 'uid')
    indices = np.column_stack([tet[f'node_index{i}'] for i in range(1, 5)])
    assert indices.min() >= 0 and indices.max() < len(xyz)
    assert np.isfinite(xyz).all() and np.all(np.diff(np.sort(indices, axis=1), axis=1) > 0)
    return xyz, indices, dict(sha256=hashlib.sha256(raw).hexdigest(),
        nodes=len(xyz), tetrahedra=len(indices),
        topology_sha256=hashlib.sha256(xyz.tobytes()+indices.tobytes()).hexdigest())


def containing_cells(xyz, indices, points):
    # shortcut: linear vertex hulls ignore curved faces; report misses, never extrapolate.
    vertices = xyz[indices]
    matrix = np.moveaxis(vertices[:, 1:]-vertices[:, :1], 1, 2)
    determinant = np.linalg.det(matrix)
    assert np.all(abs(determinant) > 1e-16), 'Degenerate linear tetrahedron'
    inverse = np.linalg.inv(matrix)
    tree = cKDTree(vertices.mean(axis=1))
    candidates = tree.query(points, k=min(64, len(vertices)))[1].reshape(len(points), -1)
    found = np.full(len(points), -1, dtype=int)
    for i, (point, candidate) in enumerate(zip(points, candidates)):
        def inside(ids):
            bary = np.einsum('nij,nj->ni', inverse[ids], point-vertices[ids, 0])
            return np.all(bary >= -1e-8, axis=1) & (bary.sum(axis=1) <= 1+1e-8)
        valid = candidate[inside(candidate)]
        if not len(valid):
            candidate = np.flatnonzero(np.all(point >= vertices.min(axis=1)-1e-9, axis=1)
                & np.all(point <= vertices.max(axis=1)+1e-9, axis=1))
            valid = candidate[inside(candidate)]
        if len(valid):
            found[i] = valid[0]
    edges = np.stack([np.linalg.norm(vertices[:, a]-vertices[:, b], axis=1)
        for a in range(4) for b in range(a+1, 4)], axis=1)
    volume = abs(determinant)/6
    # Mean-ratio quality, one for a regular tetrahedron.
    quality = 12*(3*volume)**(2/3)/np.sum(edges**2, axis=1)
    assert np.all((quality > 0) & (quality <= 1+1e-8))
    return found, edges, quality, volume


def cases():
    for row in json.loads((PREVIOUS/'controlled_refinement/results.json').read_text())['evaluations']:
        yield row['name'], Path(row['project_path']), complex(row['s_real'], row['s_imag'])
    for name in ('rectangle', 'four_edges'):
        data = json.loads((PREVIOUS/f'adaptive_{name}_strict/results.json').read_text())
        row = data['evaluations'][0]
        yield 'strict_'+name, Path(data['project_path']), complex(row['s_real'], row['s_imag'])


def predictions(e, h, s, boundary, normals, weights):
    bnd = d.als.Boundary(boundary, normals, weights, np.ones(len(boundary), bool))
    ep, hp = d.als._boundary_field_products(bnd, e, h, e, h)
    phase = np.conj(s)/abs(s)
    factor = -1j*2*np.pi*7.5e9
    electric, magnetic = [np.real(phase*factor*q) for q in (d.als.EPS0*ep, d.als.MU0*hp)]
    result = []
    for direction in ('length', 'width', 'local'):
        vn = np.sum(d.displacement(boundary, direction)*normals, axis=1)
        a, b = electric*vn*weights, magnetic*vn*weights
        result.append(dict(direction=direction, electric=float(a.sum()), magnetic=float(b.sum()),
            total=float((a+b).sum()), cancellation=float((abs(a)+abs(b)).sum()/max(abs((a+b).sum()), 1e-30))))
    return result


def mesh_only():
    out = OUT/'mesh_audit_v2'; out.mkdir(parents=True, exist_ok=False)
    with np.load(PREVIOUS/'regional_fields/densities.npz') as data:
        xy, normals = [data[k].copy() for k in ('boundary', 'normals')]
    sample = np.column_stack((xy+.05*normals, np.full(len(xy), .009)))
    rows = []
    for name, path, _ in cases():
        xyz, indices, identity = read_slim(path.with_suffix('')/'Result/3d.slim')
        found, edges, quality, volume = containing_cells(xyz, indices, sample)
        assert np.all(found >= 0), (name, int((found < 0).sum()))
        # PEC/metal volumes are excluded; curved probe surfaces use linear hulls here.
        domain_volume = float(np.prod(np.ptp(xyz, axis=0)))
        bodies = json.loads((PREVIOUS/'geometry_audit_retry/results.json').read_text())['cases'][0]['bodies']
        excluded = sum(body['volume_mm3'] for body in bodies.values()
            if body['material'] in ('PEC', 'Copper (annealed)'))
        assert abs(domain_volume-volume.sum()-excluded) < .01*excluded, (volume.sum(), domain_volume, excluded)
        regions = dict(right=normals[:, 0] > .5, top=normals[:, 1] > .5,
            bottom=normals[:, 1] < -.5, left=normals[:, 0] < -.5)
        row = dict(name=name, mesh=identity, domain_volume_mm3=domain_volume,
            sum_linear_cell_volume_mm3=float(volume.sum()), regions={})
        for region, mask in regions.items():
            ids = found[mask]
            row['regions'][region] = dict(points=int(mask.sum()), unique_cells=len(np.unique(ids)),
                min_edge_mm=np.quantile(edges[ids].min(axis=1), [0, .5, 1]).tolist(),
                max_edge_mm=np.quantile(edges[ids].max(axis=1), [0, .5, 1]).tolist(),
                quality=np.quantile(quality[ids], [0, .1, .5, 1]).tolist(),
                fraction_max_edge_gt_offset=float(np.mean(edges[ids].max(axis=1) > .05)))
        np.savez_compressed(out/f'{name}.npz', points=sample, containing_cell=found,
            vertices=xyz, tetrahedra=indices, edge_lengths=edges, quality=quality)
        rows.append(row)
        print(name, json.dumps(row['regions']), flush=True)
    # Verify the quality formula independently on a regular tetrahedron.
    regular = np.array([[0, 0, 0], [1, 0, 0], [.5, np.sqrt(3)/2, 0],
        [.5, np.sqrt(3)/6, np.sqrt(2/3)]])
    found, _, quality, volume = containing_cells(regular, np.array([[0, 1, 2, 3]]), regular.mean(axis=0)[None])
    assert found[0] == 0 and abs(quality[0]-1) < 1e-12 and abs(volume[0]-np.sqrt(2)/12) < 1e-12
    (out/'results.json').write_text(json.dumps(dict(completed=True, cases=rows,
        scope='linear tetrahedron containment; curved faces not reconstructed'), indent=2), encoding='utf-8')


def main():
    import cst.interface
    from eaopt.solver import cst_results as R
    OUT.mkdir(exist_ok=True)
    near = '--near-interface' in sys.argv
    out = OUT/('sampling_near' if near else 'sampling'); out.mkdir(exist_ok=False)
    with np.load(PREVIOUS/'regional_fields/densities.npz') as data:
        xy, normals, weights = [data[k].copy() for k in ('boundary', 'normals', 'weights')]
    scans = ([(offset, .009) for offset in (.003125, .00625, .0125, .025, .05)] if near else
        [(offset, z) for offset in (.025, .05, .1, .2) for z in (.003804, .009, .014196)])
    points = np.concatenate([np.column_stack((xy+offset*normals, np.full(len(xy), z)))
        for offset, z in scans])
    # Every requested point is outside the patch and outside the fixed feed pad.
    assert np.all((points[:, 0] < -20) | (points[:, 0] > 0)
        | (points[:, 1] < -10) | (points[:, 1] > 10))
    assert not np.any((points[:, 0] >= -25) & (points[:, 0] <= -20) & (abs(points[:, 1]) <= 2.5))
    pid = json.loads((OUT/'connection.json').read_text())['environment_pid']
    de = cst.interface.DesignEnvironment.connect(pid)
    quiet = de.in_quiet_mode(); de.set_quiet_mode(True)
    report = dict(cases=[], electromagnetic_solves=0, offsets_and_heights_mm=scans,
        mesh_scope='linear vertex tetrahedra read from self-described SLIM; curvature not reconstructed')
    try:
        for name, path, s in cases():
            print('Sampling', name, flush=True)
            xyz, indices, identity = read_slim(path.with_suffix('')/'Result/3d.slim')
            found, edges, quality, volume = containing_cells(xyz, indices, points)
            hit = found >= 0
            row = dict(name=name, mesh=identity, linear_containment_missing=int((~hit).sum()), scans=[])
            cells = found[hit]
            row['sampled_cells'] = dict(unique_cells=len(np.unique(cells)),
                min_edge_mm=np.quantile(edges[cells].min(axis=1), [0, .5, 1]).tolist(),
                max_edge_mm=np.quantile(edges[cells].max(axis=1), [0, .5, 1]).tolist(),
                quality=np.quantile(quality[cells], [0, .1, .5, 1]).tolist())
            np.savez_compressed(out/f'{name}_mesh.npz', points=points, containing_cell=found,
                vertices=xyz, tetrahedra=indices, quality=quality, edge_lengths=edges)
            project = de.open_project(str(path))
            try:
                e, h = [R.export_field_points(project.model3d, kind, 7.5, points,
                    out/f'{name}_{kind}.txt', log=lambda _: None, stimulus_port=1).T
                    for kind in ('Efield', 'Hfield')]
            finally:
                project.close()
            np.savez_compressed(out/f'{name}_fields.npz', points=points, E=e, H=h, S=s,
                boundary=xy, normals=normals, weights=weights)
            for k, (offset, z) in enumerate(scans):
                selection = slice(k*len(xy), (k+1)*len(xy))
                row['scans'].append(dict(offset_mm=offset, z_mm=z,
                    predictions=predictions(e[:, selection], h[:, selection], s, xy, normals, weights)))
            # The original scan must reproduce the previous saved point fields.
            original = (PREVIOUS/'strict_fields'/('rectangle.npz' if name == 'strict_rectangle' else 'four_edges.npz')
                if name.startswith('strict_') else PREVIOUS/'controlled_refinement'/f'{name}.npz')
            k = scans.index((.05, .009)); selection = slice(k*len(xy), (k+1)*len(xy))
            with np.load(original) as old:
                np.testing.assert_allclose(points[selection], old['points'], atol=1e-10, rtol=0)
                for key, new in [('E', e[:, selection]), ('H', h[:, selection])]:
                    np.testing.assert_allclose(new, old[key], rtol=2e-5, atol=1e-7)
            report['cases'].append(row)
            (out/'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['completed'] = True
    except Exception as error:
        report.update(completed=False, error=repr(error)); raise
    finally:
        de.set_quiet_mode(quiet)
        report['remaining_open_projects_count'] = len(de.list_open_projects())
        (out/'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    mesh_only() if '--mesh-only' in sys.argv else main()
