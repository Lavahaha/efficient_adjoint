"""Independent 2-D PEC (Hz) scattering by a polygon, in fixed modal ports.

An outgoing cylindrical-wave least-squares expansion enforces d_n Hz=0.
Fields use exp(+j omega t), peak amplitudes and 1 W incident power per column.
Accuracy must be checked by mode/quadrature refinement and held-out residuals;
this module does not assume that a finite expansion resolves PEC corners.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.special import hankel1, hankel2, h1vp, h2vp, gammaln, jv
from shapely.geometry import Point
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als


def project_edge_density(density, weights, parameter):
    """Preserve both linear vertex moments using two equivalent Gauss values.

    This is an L2 projection on each straight edge, not pointwise interpolation.
    Trailing density dimensions may represent multiple complex S derivatives.
    """
    density, weights, parameter = map(np.asarray, (density, weights, parameter))
    if (weights.ndim != 2 or parameter.shape != weights.shape or
            density.shape[:2] != weights.shape or density.ndim < 2 or
            not all(np.isfinite(v).all() for v in (density, weights, parameter)) or
            np.any(weights <= 0) or np.any((parameter < 0) | (parameter > 1))):
        raise ValueError('Finite per-edge quadrature arrays and positive weights required')
    trailing = (1,)*(density.ndim-2)
    w = weights.reshape(*weights.shape, *trailing)
    t = parameter.reshape(*parameter.shape, *trailing)
    length = weights.sum(axis=1).reshape(weights.shape[0], *trailing)
    moment0 = (w*density).sum(axis=1)/length
    moment1 = (w*t*density).sum(axis=1)/length
    a, b = 4*moment0-6*moment1, 12*moment1-6*moment0
    gauss = np.array([.5-.5/np.sqrt(3), .5+.5/np.sqrt(3)]).reshape(1, 2, *trailing)
    return (a[:, None]+b[:, None]*gauss).reshape(-1, *density.shape[2:])


def boundary_quadrature(geometry, order, *, graded=False, with_parameter=False):
    """Exact straight edges, Gauss points away from the polygon corners (mm)."""
    if geometry.geom_type != 'Polygon' or geometry.interiors or not geometry.is_valid:
        raise ValueError('This reference requires one valid simply connected polygon')
    xy = np.asarray(orient(geometry, sign=1.).exterior.coords)
    a, tangent = xy[:-1], np.diff(xy, axis=0)
    length = np.linalg.norm(tangent, axis=1)
    if np.any(length <= 0):
        raise ValueError('Zero length edge in PEC reference')
    t, weights = leggauss(order)
    t, weights = (t+1)/2, weights/2
    if graded:
        breaks = np.r_[0., 1e-5, 1e-4, 1e-3, .01, .1, .5, .9, .99, .999, .9999, .99999, 1.]
        widths = np.diff(breaks)
        t = (breaks[:-1, None]+widths[:, None]*t).ravel()
        weights = (widths[:, None]*weights).ravel()
        order = len(t)
    p = (a[:, None, :]+t[None, :, None]*tangent[:, None, :]).reshape(-1, 2)
    normal = np.column_stack((tangent[:, 1], -tangent[:, 0]))/length[:, None]
    result = p, np.repeat(normal, order, axis=0), (length[:, None]*weights).ravel()
    return (*result, np.tile(t, len(a))) if with_parameter else result


@dataclass
class PolygonSolution:
    reference: object
    coefficients: np.ndarray
    scattering: np.ndarray
    all_outputs: np.ndarray
    residual: float
    power_error: float
    reciprocity_error: float
    condition_number: float

    def fields(self, points_mm):
        incoming = self.reference.wave(points_mm, incoming=True)
        outgoing = self.reference.wave(points_mm, incoming=False)
        hz, gx, gy = [fi@self.reference.mixer-fo@self.coefficients
                      for fi, fo in zip(incoming, outgoing)]
        # curl H = j omega eps E; Hz is the only magnetic component.
        electric = np.stack((-1j*gy/(self.reference.omega*als.EPS0),
                              1j*gx/(self.reference.omega*als.EPS0), np.zeros_like(hz)), axis=1)
        magnetic = np.stack((np.zeros_like(hz), np.zeros_like(hz), hz), axis=1)
        return electric, magnetic  # point, Cartesian component, incident channel

    def density(self, points_mm, normals, output, incident):
        return self.all_densities(points_mm, normals)[:, output, incident]

    def all_densities(self, points_mm, normals):
        electric, magnetic = self.fields(points_mm)
        result = np.empty((len(points_mm), 2, 2), complex)
        for output in range(2):
            for incident in range(2):
                result[:, output, incident] = als.s_parameter_sidewall_density(
                    normals, [self.reference.length*1e3],
                    electric[:, :, incident].T[:, :, None], magnetic[:, :, incident].T[:, :, None],
                    electric[:, :, output].T[:, :, None], magnetic[:, :, output].T[:, :, None],
                    self.reference.omega, 1., 1.)
        return result


class PolygonReference:
    def __init__(self, radius_scale_mm=10., frequency_hz=5e9, max_order=32, length_m=.01):
        self.omega, self.length = 2*np.pi*frequency_hz, length_m
        self.k = self.omega*np.sqrt(als.EPS0*als.MU0)
        self.orders = np.r_[np.arange(max_order+1), np.arange(1, max_order+1)]
        self.is_sine = np.r_[np.zeros(max_order+1, bool), np.ones(max_order, bool)]
        self.source_modes = np.array([1, 2])
        self.mixer = np.array([[1., 1.], [1., -1.]])/np.sqrt(2)
        # Each cos/sin mode carries one watt through a fixed enclosing circle.
        self.norm = np.full(len(self.orders), np.sqrt(self.omega*als.EPS0/self.length))
        self.norm[0] /= np.sqrt(2)
        self.zref = self.k*radius_scale_mm*1e-3
        self.max_order = max_order
        _, derivative = self.scaled_hankel(np.array([self.zref]), max_order)
        self.derivative_scale = abs(derivative[0])
        log_factor = np.zeros(max_order+1)
        n = np.arange(2, max_order+1)
        log_factor[n] = gammaln(n)+n*np.log(2/self.zref)
        self.inverse_scale = np.exp(-log_factor[self.orders])/(self.norm*self.k*self.derivative_scale[self.orders])

    @staticmethod
    def scaled_hankel(z, maximum):
        """H_n(z)*(z/2)^n/(n-1)! for n>=2, avoiding factorial overflow.

        Orders zero/one remain unscaled. The second result is H'_n with
        the same multiplicative scaling (not the derivative of scaled H).
        """
        z = np.asarray(z, float)
        value = np.empty((len(z), maximum+1), complex)
        derivative = np.empty_like(value)
        value[:, :2] = hankel2(np.arange(2), z[:, None])
        derivative[:, :2] = h2vp(np.arange(2), z[:, None])
        value[:, 2] = hankel2(2, z)*z*z/4
        previous = value[:, 1]*z/2
        for n in range(2, maximum):
            value[:, n+1] = value[:, n]-z*z/(4*n*(n-1))*previous
            previous = value[:, n]
        derivative[:, 2] = z/2*(value[:, 1]*z/2)-2/z*value[:, 2]
        for n in range(3, maximum+1):
            derivative[:, n] = z/(2*(n-1))*value[:, n-1]-n/z*value[:, n]
        return value, derivative

    def wave(self, points_mm, *, incoming):
        points = np.asarray(points_mm, float)*1e-3
        r = np.linalg.norm(points, axis=1)
        if np.any(r <= 0):
            raise ValueError('Cylindrical expansion cannot be evaluated at its origin')
        theta = np.arctan2(points[:, 1], points[:, 0])
        if incoming:
            orders, sine = self.source_modes, np.zeros(len(self.source_modes), bool)
            norm = self.norm[self.source_modes]
            radial = hankel1(orders[None, :], self.k*r[:, None])*norm
            dr = self.k*h1vp(orders[None, :], self.k*r[:, None])*norm
        else:
            orders, sine = self.orders, self.is_sine
            z = self.k*r
            value, derivative = self.scaled_hankel(z, self.max_order)
            ratio = (self.zref/z[:, None])**np.arange(self.max_order+1)
            ratio[:, :2] = 1.
            radial = (value*ratio/self.derivative_scale/self.k)[:, orders]
            dr = (derivative*ratio/self.derivative_scale)[:, orders]
        angle = theta[:, None]*orders
        angular = np.where(sine, np.sin(angle), np.cos(angle))
        angular_derivative = orders*np.where(sine, np.cos(angle), -np.sin(angle))
        hz = radial*angular
        gr, gt = dr*angular, radial*angular_derivative/r[:, None]
        gx = gr*np.cos(theta)[:, None]-gt*np.sin(theta)[:, None]
        gy = gr*np.sin(theta)[:, None]+gt*np.cos(theta)[:, None]
        return hz, gx, gy

    def solve(self, geometry, fit_order=8, check_order=13):
        def matrices(order):
            p, n, w = boundary_quadrature(geometry, order)
            _, ix, iy = self.wave(p, incoming=True)
            _, ox, oy = self.wave(p, incoming=False)
            incoming = (ix*n[:, :1]+iy*n[:, 1:2])@self.mixer
            outgoing = ox*n[:, :1]+oy*n[:, 1:2]
            return outgoing, incoming, np.sqrt(w)[:, None]
        a, b, weight = matrices(fit_order)
        coefficients, _, rank, singular = np.linalg.lstsq(weight*a, weight*b, rcond=None)
        if rank != len(self.orders):
            raise ValueError('PEC expansion is rank deficient')
        ac, bc, wc = matrices(check_order)
        residual = np.linalg.norm(wc*(ac@coefficients-bc))/np.linalg.norm(wc*bc)
        all_outputs = coefficients*self.inverse_scale[:, None]
        scattering = self.mixer.T@all_outputs[self.source_modes]
        power_error = np.linalg.norm(all_outputs.conj().T@all_outputs-np.eye(2))
        return PolygonSolution(self, coefficients, scattering, all_outputs, float(residual),
                               float(power_error), float(np.max(abs(scattering-scattering.T))),
                               float(singular[0]/singular[-1]))


class SourceReference(PolygonReference):
    """Outgoing point sources inside PEC, clustered toward polygon corners.

    The fixed-port expansion uses Graf's addition theorem (DLMF 10.23.7).
    Source positions are a representation of the PDE solution, not physical
    excitations in the exterior domain or additional design variables.
    """

    def __init__(self, geometry, cluster_count=6, interior_fraction=.6, cluster_ratio=.4,
                 taper_sigma=None, **kwargs):
        super().__init__(**kwargs)
        if not np.isfinite(interior_fraction) or not 0 < interior_fraction < 1:
            raise ValueError('Interior source radius fraction must lie in (0,1)')
        if not np.isfinite(cluster_ratio) or not 0 < cluster_ratio < 1:
            raise ValueError('Corner source clustering ratio must lie in (0,1)')
        if taper_sigma is not None and (not np.isfinite(taper_sigma) or taper_sigma <= 0):
            raise ValueError('Tapered source scale must be finite and positive')
        self.geometry = geometry
        xy = np.asarray(orient(geometry, sign=1.).exterior.coords)[:-1]
        lengths = np.linalg.norm(np.roll(xy, -1, axis=0)-xy, axis=1)
        local = np.minimum(lengths, np.roll(lengths, 1))
        radial = xy/np.linalg.norm(xy, axis=1)[:, None]
        tangent = np.column_stack((-radial[:, 1], radial[:, 0]))
        source = [interior_fraction*xy]
        for level in range(cluster_count):
            # Tapered logarithmic spacing: more resolution at intermediate
            # distances as N grows (Trefethen et al., Numer. Math. 2021).
            depth = (cluster_ratio**level if taper_sigma is None else
                     np.exp(-taper_sigma*(np.sqrt(cluster_count)-np.sqrt(cluster_count-level))))
            for side in (-1., 1.):
                source.append(xy+(-radial+side*.7*tangent)*(local*depth*.4)[:, None])
        self.sources_mm = np.concatenate(source)
        if not all(geometry.contains(Point(p)) for p in self.sources_mm):
            raise ValueError('Reference source construction requires a star-shaped PEC enclosing the origin')
        self.source_scale = np.ones(len(self.sources_mm))

    def wave(self, points_mm, *, incoming):
        if incoming:
            return super().wave(points_mm, incoming=True)
        delta = (np.asarray(points_mm)[:, None, :]-self.sources_mm[None, :, :])*1e-3
        radius = np.linalg.norm(delta, axis=2)
        if np.any(radius <= 0):
            raise ValueError('Cannot evaluate an auxiliary source singularity')
        hz = hankel2(0, self.k*radius)/self.source_scale
        derivative = -self.k*hankel2(1, self.k*radius)/self.source_scale
        return hz, derivative*delta[:, :, 0]/radius, derivative*delta[:, :, 1]/radius

    def output_matrix(self):
        xy = self.sources_mm*1e-3
        radius, angle = np.linalg.norm(xy, axis=1), np.arctan2(xy[:, 1], xy[:, 0])
        angular = np.where(self.is_sine[:, None], np.sin(self.orders[:, None]*angle),
                           np.cos(self.orders[:, None]*angle))
        coefficient = 2*jv(self.orders[:, None], self.k*radius)*angular
        coefficient[0] /= 2
        return coefficient/self.norm[:, None]/self.source_scale

    def solve(self, geometry=None, fit_order=4, check_order=5):
        geometry = self.geometry if geometry is None else geometry
        if not geometry.equals_exact(self.geometry, 0.):
            raise ValueError('Construct the source reference for the actual geometry')
        def matrices(order):
            p, n, w = boundary_quadrature(geometry, order, graded=True)
            _, ix, iy = self.wave(p, incoming=True)
            _, ox, oy = self.wave(p, incoming=False)
            return ox*n[:, :1]+oy*n[:, 1:2], (ix*n[:, :1]+iy*n[:, 1:2])@self.mixer, np.sqrt(w)[:, None]
        self.source_scale[:] = 1.
        a, b, weight = matrices(fit_order)
        self.source_scale = np.linalg.norm(weight*a, axis=0)
        a /= self.source_scale
        coefficients, _, rank, singular = np.linalg.lstsq(weight*a, weight*b, rcond=1e-13)
        ac, bc, wc = matrices(check_order)
        residual = np.linalg.norm(wc*(ac@coefficients-bc))/np.linalg.norm(wc*bc)
        all_outputs = self.output_matrix()@coefficients
        scattering = self.mixer.T@all_outputs[self.source_modes]
        power_error = np.linalg.norm(all_outputs.conj().T@all_outputs-np.eye(2))
        result = PolygonSolution(self, coefficients, scattering, all_outputs, float(residual),
                                 float(power_error), float(np.max(abs(scattering-scattering.T))),
                                 float(singular[0]/singular[-1]))
        result.rank, result.unknowns = int(rank), len(self.source_scale)
        points, _, _, parameter = boundary_quadrature(geometry, check_order, graded=True, with_parameter=True)
        error_energy = np.sum(abs(wc*(ac@coefficients-bc))**2, axis=1)
        incident_energy = float(np.sum(abs(wc*bc)**2))
        edge_count = len(geometry.exterior.coords)-1
        edge_energy = error_energy.reshape(edge_count, -1).sum(axis=1)
        total = float(error_energy.sum())
        result.residual_profile = dict(
            edge_squared_relative_residual=(edge_energy/incident_energy).tolist(),
            corner_fraction=float(error_energy[(parameter < .01) | (parameter > .99)].sum()/total) if total else 0.,
            max_weighted_error_point_mm=points[int(np.argmax(error_energy))].tolist())
        np.testing.assert_allclose(edge_energy.sum()/incident_energy, residual**2, rtol=1e-12)
        return result
