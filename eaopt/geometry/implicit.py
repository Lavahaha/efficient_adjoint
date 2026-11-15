"""Geometry of a continuous, piecewise-linear implicit field on cell fans."""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union


def reconstruct_triangular_levelset(x, y, phi):
    """Reconstruct closure(phi<0) using four triangles per Cartesian cell.

    The cell-center value is the arithmetic mean of its corner values. Zero
    plateaux without negative values contribute no material. This defines one
    interpolant for every state, including the initial state; it does not retain
    arbitrary sub-grid CAD corners or promise a differentiable map at zero nodes.
    """
    x, y, phi = np.asarray(x, float), np.asarray(y, float), np.asarray(phi, float)
    for axis in (x, y):
        if axis.ndim != 1 or len(axis) < 2 or not np.isfinite(axis).all() or np.any(np.diff(axis) <= 0):
            raise ValueError('Implicit geometry requires finite increasing grid axes')
    if phi.shape != (len(x), len(y)) or not np.isfinite(phi).all():
        raise ValueError('Implicit field must be finite and match its grid')
    values = np.stack((phi[:-1,:-1],phi[1:,:-1],phi[1:,1:],phi[:-1,1:]),axis=-1)
    full = (values <= 0).all(axis=-1) & (values < 0).any(axis=-1)
    cut = (values < 0).any(axis=-1) & (values > 0).any(axis=-1)
    pieces = []
    # Coalesce interior cells; only cut cells need triangle clipping.
    for i in range(len(x)-1):
        endpoints = np.flatnonzero(np.diff(np.r_[False, full[i], False]))
        pieces.extend(box(x[i],y[a],x[i+1],y[b]) for a,b in endpoints.reshape(-1,2))
    intersections = {}
    ny, nnode = len(y), phi.size
    for i,j in np.argwhere(cut):
        xy = np.array([[x[i],y[j]],[x[i+1],y[j]],[x[i+1],y[j+1]],
                       [x[i],y[j+1]],[(x[i]+x[i+1])/2,(y[j]+y[j+1])/2]])
        v = np.r_[values[i,j], np.sum(values[i,j]*.25)]
        ids = [i*ny+j,(i+1)*ny+j,(i+1)*ny+j+1,i*ny+j+1,nnode+i*(ny-1)+j]

        def crossing(a,b):
            if v[a] == 0: return xy[a]
            if v[b] == 0: return xy[b]
            # Both incident triangles must use bit-identical shared crossings.
            if ids[a] > ids[b]: a,b = b,a
            key = (ids[a],ids[b])
            if key not in intersections:
                scale = max(abs(v[a]),abs(v[b]))
                ratio = (v[a]/scale)/(v[a]/scale-v[b]/scale)
                intersections[key] = xy[a]+ratio*(xy[b]-xy[a])
            return intersections[key]

        for k in range(4):
            indices = [k,(k+1)%4,4]
            if not np.any(v[indices] < 0): continue
            clipped = []
            for a,b in zip(indices,indices[1:]+indices[:1]):
                inside_a,inside_b = v[a] <= 0,v[b] <= 0
                if inside_a != inside_b: clipped.append(crossing(a,b))
                if inside_b: clipped.append(xy[b])
            if len(clipped) >= 3:
                polygon = Polygon(clipped)
                if polygon.area > 0: pieces.append(polygon)
    geometry = unary_union(pieces) if pieces else Polygon()
    if not geometry.is_valid or geometry.geom_type not in ('Polygon','MultiPolygon'):
        raise ValueError('Implicit reconstruction produced invalid polygonal geometry')
    return geometry.simplify(0.,preserve_topology=True)
