"""CAD boundary degrees of freedom and geometry-aware optimization steps."""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.sparse import coo_matrix, diags
from scipy.sparse.linalg import spsolve
from scipy.optimize import minimize
from shapely.geometry import Polygon, LineString, Point, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

import adjoint_levelset as als

@dataclass
class CadBoundaryMesh:
    vertices: np.ndarray
    rings: tuple[tuple[np.ndarray, ...], ...]
    edges: np.ndarray

    @classmethod
    def from_geometry(cls, geometry, spacing):
        if (geometry.is_empty or not geometry.is_valid or
                geometry.geom_type not in ('Polygon', 'MultiPolygon') or
                not np.isfinite(spacing) or spacing <= 0):
            raise ValueError('Valid polygonal geometry and positive spacing required')
        parts = [geometry] if geometry.geom_type == 'Polygon' else geometry.geoms
        vertices, rings, edges = [], [], []
        for part in parts:
            group = []
            part = orient(part, sign=1.)
            for ring in (part.exterior, *part.interiors):
                xy = np.asarray(ring.coords)[:-1]
                pieces = []
                for a, b in zip(xy, np.roll(xy, -1, axis=0)):
                    length = np.linalg.norm(b-a)
                    if length == 0:
                        raise ValueError('Zero length boundary edge')
                    # Do not split an already compliant edge because its length
                    # is one floating-point ulp above an integer multiple.
                    count = max(1, int(np.ceil(length/spacing-1e-12)))
                    pieces.extend(a+(b-a)*np.arange(count)[:, None]/count)
                indices = np.arange(len(vertices), len(vertices)+len(pieces))
                vertices.extend(pieces)
                edges.extend(zip(indices, np.roll(indices, -1)))
                group.append(indices)
            rings.append(tuple(group))
        return cls(np.asarray(vertices), tuple(rings), np.asarray(edges, dtype=int))

    def quadrature(self):
        """Two Gauss points per edge, outward metal normals, positive lengths."""
        a, b = self.vertices[self.edges[:, 0]], self.vertices[self.edges[:, 1]]
        tangent = b-a
        length = np.linalg.norm(tangent, axis=1)
        if np.any(length <= 0):
            raise ValueError('Degenerate CAD edge')
        normal = np.column_stack((tangent[:, 1], -tangent[:, 0]))/length[:, None]
        t = np.tile((.5-.5/np.sqrt(3), .5+.5/np.sqrt(3)), len(a))
        points = np.repeat(a, 2, axis=0)+t[:, None]*np.repeat(tangent, 2, axis=0)
        return points, np.repeat(normal, 2, axis=0), np.repeat(length/2, 2), t

    def normal_velocity(self, velocity):
        _, normals, _, t = self.quadrature()
        velocity = np.asarray(velocity, dtype=float)
        if velocity.shape != self.vertices.shape or not np.isfinite(velocity).all():
            raise ValueError('Finite vector velocity required at each CAD vertex')
        a, b = velocity[self.edges[:, 0]], velocity[self.edges[:, 1]]
        sampled = (1-t[:, None])*np.repeat(a, 2, axis=0)+t[:, None]*np.repeat(b, 2, axis=0)
        return np.sum(sampled*normals, axis=1)

    def gradient_velocity(self, density, mobility=None, *, smoothing_length=0., velocity_bounds=None):
        """Positive Riesz map P (M + ell**2 L)^-1 P T^T W, P=sqrt(mobility)."""
        _, normals, weights, t = self.quadrature()
        density = np.asarray(density, dtype=float)
        if density.shape != weights.shape or not np.isfinite(density).all():
            raise ValueError('Finite shape density required at each quadrature point')
        mobility = np.ones(len(self.vertices)) if mobility is None else np.asarray(mobility, float)
        if (mobility.shape != (len(self.vertices),) or not np.isfinite(mobility).all()
                or np.any(mobility < 0) or np.any(mobility > 1)):
            raise ValueError('Vertex mobility must lie in [0, 1]')
        force, mass = np.zeros_like(self.vertices), np.zeros(len(self.vertices))
        for end, basis in ((0, 1-t), (1, t)):
            indices = np.repeat(self.edges[:, end], 2)
            np.add.at(force, indices, (weights*density*basis)[:, None]*normals)
            np.add.at(mass, indices, weights*basis)
        if not np.isfinite(smoothing_length) or smoothing_length < 0:
            raise ValueError('Smoothing length must be finite and nonnegative')
        if velocity_bounds is not None:
            lower, upper = (np.asarray(v, float) for v in velocity_bounds)
            if (lower.shape != force.shape or upper.shape != force.shape or
                    np.isnan(lower).any() or np.isnan(upper).any() or
                    np.any(lower > 0) or np.any(upper < 0)):
                raise ValueError('Velocity bounds must contain zero at every vertex')
        if smoothing_length == 0:
            velocity = mobility[:, None]*force/mass[:, None]
            return velocity if velocity_bounds is None else np.clip(velocity, lower, upper)
        a, b = self.edges.T
        length = np.linalg.norm(self.vertices[b]-self.vertices[a], axis=1)
        value = smoothing_length**2/length
        stiffness = coo_matrix((np.r_[value, value, -value, -value],
                               (np.r_[a, b, a, b], np.r_[a, b, b, a])),
                              shape=(len(mass), len(mass))).tocsr()
        root = np.sqrt(mobility)[:, None]
        metric = diags(mass)+stiffness
        rhs = root*force
        latent = spsolve(metric, rhs)
        velocity = root*latent
        if velocity_bounds is None or ((velocity >= lower).all() and (velocity <= upper).all()):
            return velocity
        # Minimize the same positive quadratic over feasible velocities. Merely
        # clipping a coupled H1 solution need not preserve the direction work.
        for axis in range(2):
            if ((velocity[:, axis] >= lower[:, axis]).all() and
                    (velocity[:, axis] <= upper[:, axis]).all()):
                continue
            scale = max(float(np.max(abs(latent[:, axis]))), 1.)
            lo = np.full(len(mass), -np.inf)
            hi = np.full(len(mass), np.inf)
            movable = root[:, 0] > 0
            lo[movable] = lower[movable, axis]/root[movable, 0]/scale
            hi[movable] = upper[movable, axis]/root[movable, 0]/scale
            target = rhs[:, axis]/scale
            def quadratic(v):
                product = metric@v
                return .5*float(v@product)-float(target@v), product-target
            result = minimize(quadratic, np.clip(latent[:, axis]/scale, lo, hi),
                              jac=True, bounds=list(zip(lo, hi)), method='L-BFGS-B',
                              options={'ftol': 1e-15, 'gtol': 1e-10, 'maxiter': 10000})
            if not result.success:
                raise ValueError(f'CAD constrained velocity solve failed: {result.message}')
            velocity[:, axis] = root[:, 0]*result.x*scale
        # Remove only solver arithmetic roundoff at active bounds.
        velocity = np.clip(velocity, lower, upper)
        if float(np.sum(force*velocity)) < -1e-12:
            raise ValueError('CAD constrained velocity has negative direction work')
        return velocity

    def check_swept_boundary(self, displacement):
        """Reject segment contacts anywhere on the linear vertex trajectories.

        Collinearity of three linearly moving points is quadratic in normalized
        time. Contact can only begin at one of those roots (or at an endpoint
        coincidence for segments that stay collinear). This is a numerical
        collision gate, not a topology continuation/repair algorithm.
        """
        xy, delta = self.vertices, np.asarray(displacement)
        final = xy+delta
        scale = max(1., np.ptp(xy, axis=0).max(), np.max(abs(delta)))
        tolerance = 1e-11*scale
        a, b = self.edges.T
        old_edge, change = xy[b]-xy[a], delta[b]-delta[a]
        squared = np.sum(change*change, axis=1)
        t = np.clip(-np.sum(old_edge*change, axis=1)/np.where(squared > 0, squared, 1.), 0., 1.)
        if np.any(np.linalg.norm(old_edge+t[:, None]*change, axis=1) <= tolerance):
            raise ValueError('CAD topology event: an edge collapses during the step')
        swept = np.stack((xy[a], xy[b], final[a], final[b]), axis=1)
        low, high = swept.min(axis=1), swept.max(axis=1)
        overlaps = np.all(low[:, None] <= high[None, :]+tolerance, axis=2)
        overlaps &= np.all(low[None, :] <= high[:, None]+tolerance, axis=2)
        pairs = np.argwhere(np.triu(overlaps, 1))

        def cross(u, v):
            return u[0]*v[1]-u[1]*v[0]

        def roots(i, j, k):
            u, v = xy[j]-xy[i], xy[k]-xy[i]
            du, dv = delta[j]-delta[i], delta[k]-delta[i]
            coefficients = np.array([cross(du, dv), cross(du, v)+cross(u, dv), cross(u, v)])
            norm = max(abs(coefficients))
            if norm == 0:
                return []
            coefficients = np.trim_zeros(np.where(abs(coefficients) <= 1e-14*norm, 0., coefficients), 'f')
            coefficients /= norm
            if len(coefficients) == 1:
                return []
            if len(coefficients) == 2:
                values = [-coefficients[1]/coefficients[0]]
            else:
                a, b, c = coefficients
                discriminant = b*b-4*a*c
                error = 64*np.finfo(float).eps*(b*b+4*abs(a*c))
                if discriminant < -error:
                    return []
                root = np.sqrt(max(discriminant, 0.))
                q = -.5*(b+np.copysign(root, b))
                values = [q/a, c/q] if q != 0 else [-b/(2*a)]
            return [float(t) for t in values if -1e-12 <= t <= 1+1e-12]

        for first, second in pairs:
            i, j = self.edges[first]
            k, l = self.edges[second]
            shared = set((i, j)) & set((k, l))
            if shared:
                center = next(iter(shared))
                ends = list((set((i, j)) | set((k, l)))-shared)
                for t in roots(center, *ends):
                    q = xy+np.clip(t, 0, 1)*delta
                    if np.dot(q[ends[0]]-q[center], q[ends[1]]-q[center]) > tolerance**2:
                        raise ValueError('CAD topology event: adjacent edges overlap during the step')
                continue
            times = [0., 1.]+roots(i, j, k)+roots(i, j, l)+roots(k, l, i)+roots(k, l, j)
            for u in (i, j):
                for v in (k, l):
                    relative = delta[u]-delta[v]
                    axis = int(np.argmax(abs(relative)))
                    if relative[axis] != 0:
                        t = (xy[v, axis]-xy[u, axis])/relative[axis]
                        if 0 <= t <= 1:
                            times.append(float(t))
            for t in times:
                t = np.clip(t, 0., 1.)
                q = xy+t*delta
                if LineString(q[[i, j]]).distance(LineString(q[[k, l]])) <= tolerance:
                    raise ValueError('CAD topology event: distinct edges touch during the step')

    def moved(self, velocity, step):
        # This gate is intentional: silently repairing a topology event would
        # change the displacement whose derivative is being checked.
        self.normal_velocity(velocity)
        if not np.isfinite(step):
            raise ValueError('Finite step required')
        self.check_swept_boundary(step*np.asarray(velocity))
        xy = self.vertices+step*np.asarray(velocity)
        polygons = [Polygon(xy[group[0]], [xy[ring] for ring in group[1:]]) for group in self.rings]
        if any(not p.is_valid or p.is_empty or p.area <= 0 for p in polygons):
            raise ValueError('Transport creates an invalid ring or topology event')
        for polygon in polygons:
            if not polygon.exterior.is_ccw or any(ring.is_ccw for ring in polygon.interiors):
                raise ValueError('Transport reverses a boundary orientation')
        result = unary_union(polygons)
        count = 1 if result.geom_type == 'Polygon' else len(result.geoms)
        if count != len(polygons):
            raise ValueError('Transport merges distinct components')
        return result


@dataclass
class CadStep:
    base: als.LevelSet
    mesh: CadBoundaryMesh | None
    velocity: np.ndarray
    signed_work: float

    @property
    def vmax(self):
        return float(np.max(np.linalg.norm(self.velocity, axis=1), initial=0.))

    def advance(self, step):
        if self.mesh is None:
            return self.base.copy()
        geometry = self.mesh.moved(self.velocity, step)
        state = self.base.geometry_state.with_geometry(geometry)
        return als.LevelSet(self.base.grid, state.phi, geometry_state=state)


class CadEvolution:
    """Geometry-aware steps in the shared optimizer; topology contacts stop.

    Two Gauss points per edge with vertex spacing 2*dx give roughly the existing
    boundary sampling density. No point-count simplification is performed.
    """

    def prepare(self, ls, problem, fwd, design_mask, cfg):
        ls.geometry_state.validate_phi(ls.phi)
        if ls.geometry_state.geometry.is_empty:
            return CadStep(ls, None, np.empty((0, 2)), 0.)
        mesh = CadBoundaryMesh.from_geometry(ls.geometry_state.geometry, 2*ls.grid.dx)
        points, normals, weights, _ = mesh.quadrature()
        mask = np.asarray(design_mask, float)
        if mask.shape != ls.phi.shape or not np.isfinite(mask).all() or np.any((mask < 0) | (mask > 1)):
            raise ValueError('CAD design mobility must be finite and lie in [0, 1]')
        sample = RegularGridInterpolator((ls.grid.x, ls.grid.y), mask, bounds_error=False, fill_value=0.)
        boundary = als.Boundary(points, normals, weights, sample(points) > .5)
        gradient = np.asarray(als.problem_gradient(problem, ls, fwd, boundary), float)
        if gradient.shape != weights.shape or not np.isfinite(gradient).all():
            raise ValueError('CAD shape density must be finite and match its boundary quadrature')
        signed = np.where(boundary.active, gradient, 0.)*(1 if cfg.maximize else -1)
        # Convex interpolation is in [0, 1] analytically; remove roundoff such
        # as 1+2e-16 after validating the original mask above.
        mobility = np.clip(sample(mesh.vertices), 0., 1.)
        # An inactive edge must remain fixed even when the neighboring edge has
        # a nonzero density. Freeze both endpoints, including crossing edges.
        outside = ((mesh.vertices < [ls.grid.x[0], ls.grid.y[0]]) |
                   (mesh.vertices > [ls.grid.x[-1], ls.grid.y[-1]])).any(axis=1)
        frozen_edges = ~boundary.active.reshape(-1, 2).all(axis=1) | outside[mesh.edges].any(axis=1)
        mobility[np.unique(mesh.edges[frozen_edges])] = 0.
        dt = cfg.pseudo_time_step
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError('CAD pseudo time step must be finite and positive')
        lower = (np.array([ls.grid.x[0], ls.grid.y[0]])-mesh.vertices)/dt
        upper = (np.array([ls.grid.x[-1], ls.grid.y[-1]])-mesh.vertices)/dt
        lower[outside], upper[outside] = 0., 0.
        velocity = mesh.gradient_velocity(signed, mobility,
                                         smoothing_length=cfg.smooth_sigma_cells*ls.grid.dx,
                                         velocity_bounds=(lower, upper))
        work = float(np.sum(weights*signed*mesh.normal_velocity(velocity)))
        if not np.isfinite(work) or work < -1e-12:
            raise ValueError('CAD velocity failed its discrete direction check')
        return CadStep(ls, mesh, velocity, work)

    def apply_constraints(self, trial, force_metal=None, force_air=None, anchor=None):
        state = trial.geometry_state
        for mask in (force_metal, force_air, anchor):
            if mask is not None and np.asarray(mask).shape != trial.phi.shape:
                raise ValueError('CAD constraint masks must match the grid')
        add, remove = np.zeros(trial.phi.shape, bool), np.zeros(trial.phi.shape, bool)
        if force_air is not None:
            remove = np.asarray(force_air, bool) & (trial.phi <= 0)
        if force_metal is not None:
            metal = np.asarray(force_metal, bool)
            add = metal & (trial.phi > 0)
            remove &= ~metal
        if add.any() or remove.any():
            state, _ = state.apply_node_edits(add, remove)
        if anchor is not None and not state.geometry.is_empty:
            mask = np.asarray(anchor, bool)
            anchor_points = [Point(state.x[i], state.y[j]) for i, j in np.argwhere(mask)]
            parts = ([state.geometry] if state.geometry.geom_type == 'Polygon' else state.geometry.geoms)
            kept = [part for part in parts if any(part.covers(p) for p in anchor_points)]
            geometry = unary_union(kept) if kept else Polygon()
            domain = box(state.x[0], state.y[0], state.x[-1], state.y[-1])
            geometry = geometry.intersection(domain).union(state.geometry.difference(domain))
            state = state.with_geometry(geometry)
        trial.geometry_state, trial.phi = state, state.phi

