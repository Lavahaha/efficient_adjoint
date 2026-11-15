"""Retained CAD with a sampled auxiliary level set and explicit Boolean edits."""
from __future__ import annotations

import numpy as np
from shapely import contains_xy, distance, points
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import unary_union


class CadGeometryState:
    """CAD is authoritative; a phi-only update cannot silently replace it.

    This state supports polygonal holes/components and Boolean topology edits.
    It does not define a time-stepping or electromagnetic gradient algorithm.
    """

    def __init__(self, geometry, x, y):
        if (geometry.geom_type not in ('Polygon', 'MultiPolygon') or not geometry.is_valid
                or (not geometry.is_empty and not np.isfinite(geometry.bounds).all())):
            raise ValueError('CAD must be a valid finite Polygon or MultiPolygon')
        self._geometry = geometry
        self.x, self.y = (np.asarray(axis, dtype=float).copy() for axis in (x, y))
        for axis in (self.x, self.y):
            if (axis.ndim != 1 or len(axis) < 2 or not np.isfinite(axis).all()
                    or not np.all(np.diff(axis) > 0)
                    or not np.allclose(np.diff(axis), axis[1]-axis[0], rtol=1e-10, atol=1e-12)):
                raise ValueError('CAD auxiliary grid axes must be finite, increasing and uniform')
            axis.flags.writeable = False
        xx, yy = np.meshgrid(self.x, self.y, indexing='ij')
        if geometry.is_empty:
            # A finite positive field also represents the empty design.
            self._phi = np.full(xx.shape, np.hypot(np.ptp(self.x), np.ptp(self.y)))
        else:
            dist = distance(geometry.boundary, points(xx, yy))
            inside = contains_xy(geometry, xx, yy) | (dist <= 1e-9)
            self._phi = np.where(inside, -dist, dist)
        self._phi.flags.writeable = False

    @property
    def geometry(self):
        return self._geometry

    @property
    def phi(self):
        return self._phi.copy()

    @property
    def area_in_grid(self):
        return float(self.geometry.intersection(box(self.x[0], self.y[0], self.x[-1], self.y[-1])).area)

    def copy(self):
        # Shapely geometries are immutable; arrays remain private/read-only.
        return CadGeometryState(self.geometry, self.x, self.y)

    def with_geometry(self, geometry):
        return CadGeometryState(geometry, self.x, self.y)

    def validate_phi(self, phi):
        """Allow re-distancing that preserves signs, zeros and cut-edge ratios.

        Nodal samples cannot certify sub-cell CAD features. Retained CAD supplies
        that information; this check only rejects incompatible auxiliary edits.
        """
        phi = np.asarray(phi, dtype=float)
        expected = self._phi
        if phi.shape != expected.shape or not np.isfinite(phi).all():
            raise ValueError('Auxiliary phi must be finite and match the CAD grid')
        if not np.array_equal(np.sign(phi), np.sign(expected)):
            raise ValueError('Auxiliary phi material signs/zero nodes changed without a CAD update')
        for axis in (0, 1):
            first, second = [slice(None)]*2, [slice(None)]*2
            first[axis], second[axis] = slice(None, -1), slice(1, None)
            a, b = expected[tuple(first)], expected[tuple(second)]
            c, d = phi[tuple(first)], phi[tuple(second)]
            cut = (a < 0) & (b > 0) | (a > 0) & (b < 0)
            # Ratios normalized by max magnitude avoid unnecessary overflow.
            def fraction(left, right):
                scale = np.maximum(abs(left), abs(right))
                return (left/scale)/(left/scale-right/scale)
            if not np.allclose(fraction(a[cut], b[cut]), fraction(c[cut], d[cut]), rtol=0, atol=1e-9):
                raise ValueError('Auxiliary phi cut edges moved without a CAD update')

    def apply_delta(self, *, added=None, removed=None):
        """Apply explicit geometric edits and report actual, disjoint changes.

        Removal takes precedence when requested sets overlap. No global contour
        reconstruction or automatic invalid-geometry repair is performed.
        """
        added = Polygon() if added is None else added
        removed = Polygon() if removed is None else removed
        for edit in (added, removed):
            if (edit.geom_type not in ('Polygon', 'MultiPolygon') or not edit.is_valid
                    or not edit.is_empty and not np.isfinite(edit.bounds).all()):
                raise ValueError('CAD additions/removals must be valid finite polygonal regions')
        geometry = self.geometry.union(added).difference(removed)
        state = self.with_geometry(geometry)
        additions = geometry.difference(self.geometry)
        removals = self.geometry.difference(geometry)
        return state, dict(added=additions, removed=removals,
                          added_area_mm2=float(additions.area), removed_area_mm2=float(removals.area))

    def apply_node_edits(self, add_nodes, remove_nodes):
        """Explicit grid-mask repair: add/remove clipped dual cells of nodes.

        This defines the sub-cell geometry of a mask repair; it is not an inverse
        of arbitrary phi changes and does not claim a continuous min-gap bound.
        Metal outside the auxiliary grid is retained exactly.
        """
        masks = [np.asarray(mask) for mask in (add_nodes, remove_nodes)]
        if any(mask.shape != self._phi.shape or mask.dtype != np.bool_ for mask in masks):
            raise ValueError('CAD node edits require two boolean masks matching the grid')
        if np.any(masks[0] & masks[1]):
            raise ValueError('A node cannot request both metal and air')
        domain = box(self.x[0], self.y[0], self.x[-1], self.y[-1])
        # Adjacent cells must use bit-identical shared edges; computing each
        # center +/- dx/2 separately can leave microscopic disconnected slits.
        edges_x = np.r_[self.x[0], (self.x[:-1]+self.x[1:])/2, self.x[-1]]
        edges_y = np.r_[self.y[0], (self.y[:-1]+self.y[1:])/2, self.y[-1]]
        def cells(mask):
            pieces = [box(edges_x[i], edges_y[j], edges_x[i+1], edges_y[j+1]).intersection(domain)
                      for i, j in np.argwhere(mask)]
            return unary_union(pieces) if pieces else Polygon()
        state, changes = self.apply_delta(added=cells(masks[0]), removed=cells(masks[1]))
        # A boundary-node request can conflict with fixed metal outside D.
        if np.any(state._phi[masks[0]] >= 0) or np.any(state._phi[masks[1]] <= 0):
            raise ValueError('Node repair conflicts with a retained boundary or outside metal')
        return state, changes

    def filter_features(self, width_mm):
        """Polygon-offset opening/closing without rasterizing the boundary.

        Mitred joins preserve resolved polygon corners. This is an explicit
        offset filter, not a guarantee of minimum width after frozen-region or
        other subsequent projections. Outside-domain metal remains unchanged.
        """
        width = float(width_mm)
        if not np.isfinite(width) or width < 0:
            raise ValueError('CAD feature width must be finite and nonnegative')
        if width == 0 or self.geometry.is_empty:
            return self.copy()
        radius = width/2
        opened = self.geometry.buffer(-radius, join_style=2).buffer(radius, join_style=2)
        filtered = opened.buffer(radius, join_style=2).buffer(-radius, join_style=2)
        domain = box(self.x[0], self.y[0], self.x[-1], self.y[-1])
        filtered = filtered.intersection(domain).union(self.geometry.difference(domain))
        if filtered.is_empty:
            raise ValueError('CAD minimum-feature filter removed all metal')
        # Avoid accumulating offset arithmetic noise on already resolved CAD.
        if (filtered.hausdorff_distance(self.geometry) <= 1e-12 and
                filtered.symmetric_difference(self.geometry).area <= 1e-12):
            return self.copy()
        return self.with_geometry(filtered)

    def to_dict(self):
        return dict(unit='mm', x=self.x.tolist(), y=self.y.tolist(), geometry=mapping(self.geometry))

    @classmethod
    def from_dict(cls, data):
        if data['unit'] != 'mm':
            raise ValueError('CAD geometry state requires mm coordinates')
        return cls(shape(data['geometry']), data['x'], data['y'])
