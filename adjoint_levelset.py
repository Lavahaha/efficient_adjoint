"""
adjoint_levelset.py
===================
Solver-agnostic implementation of the *optimization half* of

    S. Ji et al., "Efficient Adjoint-Based Shape Optimization Method for the
    Inverse Design of Microwave Components", IEEE TMTT 73(1), 2025.

What this module does (paper section in brackets)
-------------------------------------------------
1. Shape derivative on every boundary point from forward + adjoint ("backward")
   field samples                                                   [II-B, Eq. 20/25]
2. Chain rule from a complex S-parameter to a real objective       [extension]
3. Velocity construction: boundary -> grid extension + smoothing   [extension]
4. Level-set evolution  phi_t + V|grad phi| = 0 (upwind HJ, RK2)    [II-C, Eq. 26-31]
5. Signed-distance reinitialisation, design-region / keep-metal /
   keep-air masks, connectivity constraint                         [III-A/B constraints]
6. The outer loop with a fixed pseudo-time step

What it does NOT do: run the Maxwell solver. You plug that in through the small
`Problem` interface (forward(), adjoint()). See
`scripts/run_coupler_adjoint_levelset.py` for the CST adapter and the notes below.

Conventions (read these once, they matter)
------------------------------------------
* Arrays are indexed [ix, iy]  (indexing="ij").
* phi < 0  : metal,   phi > 0 : air,   phi = 0 : boundary.
* Outward normal n = grad(phi)/|grad(phi)| points from metal INTO air.
* V > 0 means the metal GROWS along n (phi_t = -V|grad phi| decreases phi).
* The shape gradient is a boundary density: dF = integral(g * Vn dA).
  A discrete design-variable derivative additionally includes quadrature
  weights and the geometry basis. It is not a point displacement by itself.
* Time convention e^{+j w t}, as in the paper (curl E = -j w mu H).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Tuple

import numpy as np
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator
from scipy.spatial import cKDTree
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from skimage import measure
from eaopt.geometry.contour import dedupe_vertices

MU0 = 4e-7 * np.pi
EPS0 = 8.8541878128e-12


# ----------------------------------------------------------------------------
# Grid and level-set container
# ----------------------------------------------------------------------------
@dataclass
class Grid2D:
    nx: int
    ny: int
    dx: float
    x0: float = 0.0
    y0: float = 0.0

    @property
    def x(self):
        return self.x0 + self.dx * np.arange(self.nx)

    @property
    def y(self):
        return self.y0 + self.dx * np.arange(self.ny)

    def mesh(self):
        return np.meshgrid(self.x, self.y, indexing="ij")


class LevelSet:
    """phi on a uniform grid. phi<0 metal."""

    def __init__(self, grid: Grid2D, phi: np.ndarray, *, geometry_state=None):
        self.grid = grid
        self.phi = np.asarray(phi, dtype=float).copy()
        self.geometry_state = geometry_state
        if geometry_state is not None:
            if not (np.allclose(geometry_state.x, grid.x, rtol=0, atol=1e-12) and
                    np.allclose(geometry_state.y, grid.y, rtol=0, atol=1e-12)):
                raise ValueError('CAD state and level-set grid coordinates differ')
            geometry_state.validate_phi(self.phi)

    # -- constructors ------------------------------------------------------
    @classmethod
    def from_mask(cls, grid: Grid2D, metal_mask: np.ndarray) -> "LevelSet":
        """Binary mask -> signed distance (sub-cell accurate if skfmm present)."""
        phi = np.where(metal_mask, -1.0, 1.0)
        # smooth the staircase a little so the zero contour is sub-cell
        phi = ndimage.gaussian_filter(phi, 1.0)
        ls = cls(grid, phi)
        ls.reinit()
        return ls

    def copy(self) -> "LevelSet":
        state = self.geometry_state.copy() if self.geometry_state is not None else None
        return LevelSet(self.grid, self.phi, geometry_state=state)

    # -- geometry helpers --------------------------------------------------
    def reinit(self, *, preserve_cell_fans: bool = False) -> None:
        """Re-distance away from the interface, preserving interpolated edge zeros.

        Cut-edge endpoints share one positive scale per connected component;
        their ratios (and hence the marching-squares contour) stay unchanged.
        preserve_cell_fans also couples all corners of each mixed-sign cell,
        preserving crossings of the mean-center triangular interpolant.
        Exact signed distance and all sub-cell edge zeros cannot generally both
        be represented by the same nodal values.
        """
        phi = self.phi
        if not (phi.min() < 0 < phi.max()):
            return
        try:
            import skfmm

            distance = np.asarray(skfmm.distance(phi, dx=self.grid.dx))
        except ImportError:  # coarse fallback, pixel-accurate only
            dx = self.grid.dx
            inside = phi < 0
            distance = (
                ndimage.distance_transform_edt(~inside)
                - ndimage.distance_transform_edt(inside)
            ) * dx
        nodes = np.arange(phi.size).reshape(phi.shape)
        edges = []
        for axis in (0, 1):
            a, b = [slice(None)] * 2, [slice(None)] * 2
            a[axis], b[axis] = slice(None, -1), slice(1, None)
            a, b = tuple(a), tuple(b)
            cut = ((phi[a] < 0) & (phi[b] > 0)) | ((phi[a] > 0) & (phi[b] < 0))
            edges.append(np.column_stack((nodes[a][cut], nodes[b][cut])))
        if preserve_cell_fans:
            corners = np.stack((phi[:-1,:-1],phi[1:,:-1],phi[1:,1:],phi[:-1,1:]),axis=-1)
            mixed = (corners < 0).any(axis=-1) & (corners > 0).any(axis=-1)
            first = nodes[:-1,:-1][mixed]
            for neighbor in (nodes[1:,:-1],nodes[1:,1:],nodes[:-1,1:]):
                edges.append(np.column_stack((first,neighbor[mixed])))
        edges = np.concatenate(edges)
        if len(edges):
            indices, inverse = np.unique(edges, return_inverse=True)
            links = inverse.reshape(-1, 2)
            graph = coo_matrix((np.ones(len(links)), links.T),
                               shape=(len(indices), len(indices)))
            count, labels = connected_components(graph, directed=False)
            old = phi.ravel()[indices]
            target = distance.flat[indices]
            scale = (np.bincount(labels, weights=old * target, minlength=count)
                     / np.bincount(labels, weights=old ** 2, minlength=count))
            distance.flat[indices] = old * scale[labels]
        distance[phi == 0] = 0.0
        self.phi = distance

    def metal_mask(self) -> np.ndarray:
        return self.phi < 0

    def area(self) -> float:
        if self.geometry_state is not None:
            self.geometry_state.validate_phi(self.phi)
            return self.geometry_state.area_in_grid
        return float(self.metal_mask().sum() * self.grid.dx ** 2)

    def normals_grid(self) -> Tuple[np.ndarray, np.ndarray]:
        gx, gy = np.gradient(self.phi, self.grid.dx, self.grid.dx)
        nrm = np.hypot(gx, gy) + 1e-30
        return gx / nrm, gy / nrm


# ----------------------------------------------------------------------------
# Boundary extraction
# ----------------------------------------------------------------------------
@dataclass
class Boundary:
    pts: np.ndarray       # (N,2) physical coordinates on phi=0
    normals: np.ndarray   # (N,2) outward (metal -> air) unit normals
    weights: np.ndarray   # (N,)  arc-length weights, sum = perimeter
    active: np.ndarray    # (N,)  bool, inside the design region


def extract_boundary(ls: LevelSet, design_mask: Optional[np.ndarray] = None) -> Boundary:
    g = ls.grid
    pts_l, w_l = [], []
    for c in measure.find_contours(ls.phi, 0.0):
        xy = np.column_stack([g.x0 + c[:, 0] * g.dx, g.y0 + c[:, 1] * g.dx])
        seg = np.hypot(*np.diff(xy, axis=0).T)
        if np.allclose(xy[0], xy[-1]):
            p = xy[:-1]
            w = 0.5 * (seg + np.roll(seg, 1))
        else:
            p = xy
            w = np.zeros(len(p))
            w[:-1] += 0.5 * seg
            w[1:] += 0.5 * seg
        pts_l.append(p)
        w_l.append(w)
    if not pts_l:
        z = np.zeros((0, 2))
        return Boundary(z, z, np.zeros(0), np.zeros(0, bool))
    pts, w = np.vstack(pts_l), np.concatenate(w_l)

    nxg, nyg = ls.normals_grid()
    kw = dict(bounds_error=False, fill_value=0.0)
    fx = RegularGridInterpolator((g.x, g.y), nxg, **kw)
    fy = RegularGridInterpolator((g.x, g.y), nyg, **kw)
    n = np.column_stack([fx(pts), fy(pts)])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-30

    if design_mask is None:
        active = np.ones(len(pts), bool)
    else:
        fm = RegularGridInterpolator(
            (g.x, g.y), design_mask.astype(float), method="nearest",
            bounds_error=False, fill_value=0.0)
        active = fm(pts) > 0.5
    return Boundary(pts, n, w, active)


def boundary_polygons(ls: LevelSet, tol: Optional[float] = None) -> List[np.ndarray]:
    """Zero level set as simplified polygons (physical coordinates) -- feed these
    to the EM solver's geometry builder (CST VBA / Python API, gdspy, ...)."""
    g = ls.grid
    tol = 0.2 * g.dx if tol is None else tol
    out = []
    for i, c in enumerate(measure.find_contours(ls.phi, 0.0)):
        c = dedupe_vertices(np.asarray(c, dtype=float))
        c = measure.approximate_polygon(c, tolerance=tol / g.dx)
        c = dedupe_vertices(np.asarray(c, dtype=float))
        if len(c) < 2:
            raise ValueError(
                f"第 {i} 条水平集轮廓去重后不足两个点，无法转换为 CST 几何。"
            )
        out.append(np.column_stack([g.x0 + c[:, 0] * g.dx, g.y0 + c[:, 1] * g.dx]))
    return out


# ----------------------------------------------------------------------------
# Field container + shape derivative  (paper Eq. 20 / 25)
# ----------------------------------------------------------------------------
@dataclass
class FieldBundle:
    """Complex phasor fields on a regular xy grid (one z-slice, e.g. mid-metal
    plane, or a z-average over the metal thickness).  E, H: (3, nx, ny)."""
    x: np.ndarray
    y: np.ndarray
    E: np.ndarray
    H: np.ndarray
    _interp: Optional[RegularGridInterpolator] = field(default=None, repr=False)

    def sample(self, pts: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        if self._interp is None:
            vals = np.concatenate([self.E, self.H], axis=0)       # (6,nx,ny)
            vals = np.moveaxis(vals, 0, -1)                       # (nx,ny,6)
            self._interp = RegularGridInterpolator(
                (self.x, self.y), vals, bounds_error=False, fill_value=0.0)
        v = self._interp(pts)                                     # (N,6)
        return v[:, :3].T, v[:, 3:].T


def shape_derivative_complex(
    boundary: Boundary,
    fwd: FieldBundle,
    adj: FieldBundle,
    prefactor: complex,
    offset: float,
    eps: float = EPS0,
    mu: float = MU0,
) -> np.ndarray:
    """
    Complex sensitivity of the FoM (e.g. an S-parameter) to a unit outward
    displacement of every boundary point, Eq. (25) before taking Re[]:

        g_i = prefactor * ( eps * E_perp . Eb_perp  +  mu * H_par . Hb_par )

    prefactor : depends on the port and adjoint definitions. Derive its
                normalization from those definitions and verify independently;
                do not fit a scalar to an unresolved finite difference.
    offset    : distance (>0 into air, <0 into metal) from the boundary at which the
                fields are sampled. Paper: samples slightly inside the metal for
                lossy/finite-conductivity metal. For a PEC-like export where the
                metal interior is zero, sample just outside, ~0.5-1.5 cells.
    The products are bilinear (no complex conjugate), as in Eq. (20)/(25).
    """
    ps = boundary.pts + offset * boundary.normals
    Ef, Hf = fwd.sample(ps)
    Eb, Hb = adj.sample(ps)
    return shape_derivative_from_fields(boundary, Ef, Hf, Eb, Hb, prefactor, eps, mu)


def shape_derivative_from_fields(boundary: Boundary, Ef: np.ndarray, Hf: np.ndarray,
                                 Eb: np.ndarray, Hb: np.ndarray, prefactor: complex,
                                 eps: float = EPS0, mu: float = MU0) -> np.ndarray:
    """Eq. (25) kernel for E/H evaluated at the same boundary sample points."""
    e_perp, h_par = _boundary_field_products(boundary, Ef, Hf, Eb, Hb)
    return prefactor * (eps * e_perp + mu * h_par)


def shape_derivative_mine(boundary: Boundary, Ef: np.ndarray, Hf: np.ndarray,
                          Eb: np.ndarray, Hb: np.ndarray, omega: float,
                          eps: float = EPS0, mu: float = MU0) -> np.ndarray:
    """Real boundary sensitivity Re[eps E_perp . Eb_perp + mu H_par . Hb_par].

    Fields have shape (3, N) at the same sample points.
    omega (rad/s) is retained for call compatibility, not used in this kernel.
    Products are bilinear (no conjugation). Any objective source weight must
    already be included in Eb/Hb. No Eq. (25) prefactor or port power is applied.
    """
    if not np.isfinite(omega) or omega <= 0:
        raise ValueError("omega must be finite and positive (rad/s)")
    e_perp, h_par = _boundary_field_products(boundary, Ef, Hf, Eb, Hb)
    return np.real(eps * e_perp + mu * h_par)


def _boundary_field_products(boundary, Ef, Hf, Eb, Hb):
    """Bilinear normal E and tangential H products shared by both kernels."""
    if len(boundary.normals) != len(boundary.pts):
        raise ValueError("Boundary points and normals must have equal lengths")
    return _normal_field_products(boundary.normals, Ef, Hf, Eb, Hb)


def _normal_field_products(normals, Ef, Hf, Eb, Hb):
    normals = np.asarray(normals, dtype=float)
    if normals.ndim != 2 or normals.shape[1] not in (2, 3) or not np.isfinite(normals).all():
        raise ValueError("Normals must be finite arrays of shape (N, 2) or (N, 3)")
    count = len(normals)
    for field in (Ef, Hf, Eb, Hb):
        if field.shape != (3, count) or not np.isfinite(field).all():
            raise ValueError("Boundary E/H must be finite arrays of shape (3, N)")
    n3 = normals.T if normals.shape[1] == 3 else np.vstack([normals.T, np.zeros(count)])
    dot = lambda a, b: np.sum(a * b, axis=0)
    e_perp = dot(Ef, n3) * dot(Eb, n3)                           # E_perp . Eb_perp
    h_par = dot(Hf, Hb) - dot(Hf, n3) * dot(Hb, n3)              # H_par . Hb_par
    return e_perp, h_par


def s_parameter_shape_density(
    normals, Ef, Hf, Eb, Hb,
    omega: float, incident_amplitude_fwd: complex, incident_amplitude_adj: complex,
    eps=EPS0, mu=MU0,
) -> np.ndarray:
    """Complex PEC surface density q, with delta S = integral q*delta_n*dA (SI).

    Fields (3,N) are the unweighted forward (port n) and adjoint (port m)
    solutions. Incident amplitudes are complex power-wave amplitudes in sqrt(W),
    with peak phasors, exp(+jwt), and reciprocal lossless port modes. Their
    squared moduli are average powers; their phases must match the field solves.
    The PEC normal points into the dielectric. Ports/material interfaces are
    fixed. eps/mu are finite scalars or one value per quadrature point.
    See docs/pec_smatrix_derivation_2026-11-12.md for the derivation and limits.
    """
    normals = np.asarray(normals, dtype=float)
    ep, hp = _normal_field_products(normals, Ef, Hf, Eb, Hb)
    if not np.allclose(np.linalg.norm(normals, axis=1), 1., rtol=1e-10, atol=1e-12):
        raise ValueError("Physical shape variations require unit normals")
    if not np.isfinite(omega) or omega <= 0:
        raise ValueError("omega must be finite and positive (rad/s)")
    amplitudes = np.asarray([incident_amplitude_fwd, incident_amplitude_adj], complex)
    if not np.isfinite(amplitudes).all() or np.any(abs(amplitudes) == 0):
        raise ValueError("Incident power-wave amplitudes must be finite and nonzero")
    materials = [np.asarray(v, complex) for v in (eps, mu)]
    if any(v.shape not in ((), (len(normals),)) or not np.isfinite(v).all() for v in materials):
        raise ValueError("eps/mu must be finite scalars or arrays of shape (N,)")
    kernel = materials[0]*ep + materials[1]*hp
    return -1j*omega/(4*amplitudes[0]*amplitudes[1]) * kernel


def s_parameter_shape_variation(
    normals, area_weights_m2, normal_displacement_m, Ef, Hf, Eb, Hb,
    omega: float, incident_amplitude_fwd: complex, incident_amplitude_adj: complex,
    eps=EPS0, mu=MU0,
) -> complex:
    """Integrate the normalized PEC density over an actual SI surface displacement."""
    density = s_parameter_shape_density(normals, Ef, Hf, Eb, Hb, omega,
                                        incident_amplitude_fwd, incident_amplitude_adj, eps, mu)
    area = np.asarray(area_weights_m2, dtype=float)
    displacement = np.asarray(normal_displacement_m, dtype=float)
    if any(v.shape != density.shape or not np.isfinite(v).all() for v in (area, displacement)):
        raise ValueError("Surface weights and displacements must be finite arrays of shape (N,)")
    if np.any(area < 0):
        raise ValueError("Surface area quadrature weights must be nonnegative")
    return complex(np.sum(density*area*displacement))


def s_parameter_sidewall_density(
    normals_xy, z_weights_mm, Ef, Hf, Eb, Hb, omega: float,
    incident_amplitude_fwd: complex, incident_amplitude_adj: complex, eps=EPS0, mu=MU0,
) -> np.ndarray:
    """Extruded PEC: delta S = sum_i g_i*ds_i_mm*delta_n_i_mm.

    Fields are unweighted peak phasors of shape (3, N_boundary, N_z).
    Heights are integrated explicitly; their positive weights are in mm.
    eps/mu are scalar or (N_boundary,N_z). The in-plane displacement is
    independent of z; top/bottom faces have zero normal velocity.
    """
    normals = np.asarray(normals_xy, float)
    wz = np.asarray(z_weights_mm, float)
    if normals.ndim != 2 or normals.shape[1] != 2:
        raise ValueError('Sidewall normals must have shape (N_boundary,2)')
    if wz.ndim != 1 or not len(wz) or not np.isfinite(wz).all() or np.any(wz <= 0):
        raise ValueError('Thickness quadrature requires finite positive mm weights')
    expected = (3, len(normals), len(wz))
    fields = [np.asarray(v) for v in (Ef, Hf, Eb, Hb)]
    if any(v.shape != expected for v in fields):
        raise ValueError('Sidewall fields must have shape (3,N_boundary,N_z)')
    materials = [np.asarray(v) for v in (eps, mu)]
    if any(v.shape not in ((), expected[1:]) for v in materials):
        raise ValueError('Sidewall materials must be scalar or (N_boundary,N_z)')
    materials = [v if v.ndim == 0 else v.ravel() for v in materials]
    density = s_parameter_shape_density(np.repeat(normals, len(wz), axis=0),
        *(v.reshape(3, -1) for v in fields), omega, incident_amplitude_fwd,
        incident_amplitude_adj, *materials)
    # ds_mm * dz_mm * delta_n_mm -> m^3, once and only once.
    return 1e-9*(density.reshape(len(normals), len(wz))@wz)


def fom_value_and_weight(s: complex, kind: str = "abs2") -> Tuple[float, complex]:
    """Real objective f(s) and weight c such that  df = Re( conj(c) * ds ).
    Lets you optimise |S|^2, |S| or S in dB from the *complex* sensitivity."""
    if not np.isfinite(s):
        raise ValueError("S must be finite")
    a = abs(s)
    if kind == "abs2":
        return a ** 2, 2 * s
    if kind == "abs":
        if a == 0:
            raise ValueError("|S| has no unique classical derivative at S=0")
        return a, s / a
    if kind == "db":
        if a == 0:
            raise ValueError("S in dB is undefined at S=0")
        return 20 * np.log10(a), (20 / np.log(10)) * (s / a) / a
    if kind == "real":
        return float(np.real(s)), 1.0 + 0j
    if kind == "imag":
        return float(np.imag(s)), 1j
    raise ValueError(kind)


def real_gradient(g_complex: np.ndarray, c: complex) -> np.ndarray:
    return np.real(np.conj(c) * g_complex)


# ----------------------------------------------------------------------------
# Velocity field: boundary values -> grid, smoothed, step-normalised
# ----------------------------------------------------------------------------
def boundary_to_grid_velocity(
    ls: LevelSet,
    boundary: Boundary,
    g_real: np.ndarray,
    design_mask: Optional[np.ndarray] = None,
    band_cells: float = 4.0,
    k_nearest: int = 4,
    smooth_sigma_cells: float = 1.5,
) -> np.ndarray:
    """Extend boundary sensitivities into a narrow band (constant along the
    normal ~ nearest-point extension), then smooth with a Gaussian using
    normalised convolution. If that changes the ascent direction, use a
    positive semidefinite extension in the same boundary quadrature metric.
    This changes neither the time step nor the displacement acceptance rule.
    """
    g = ls.grid
    X, Y = g.mesh()
    band = np.abs(ls.phi) < band_cells * g.dx
    V = np.zeros_like(ls.phi)
    if len(boundary.pts) == 0:
        return V
    v_b = np.where(boundary.active, g_real, 0.0)
    tree = cKDTree(boundary.pts)
    q = np.column_stack([X[band], Y[band]])
    k = min(k_nearest, len(boundary.pts))
    d, i = tree.query(q, k=k)
    d = d.reshape(len(q), -1)
    i = i.reshape(len(q), -1)
    w = 1.0 / (d + 0.25 * g.dx) ** 2
    V[band] = np.sum(w * v_b[i], axis=1) / np.sum(w, axis=1)

    s = smooth_sigma_cells
    if s > 0:
        num = ndimage.gaussian_filter(V * band, s)
        den = ndimage.gaussian_filter(band.astype(float), s) + 1e-12
        V = num / den
    if design_mask is not None:
        V = V * design_mask
    sampled = RegularGridInterpolator((g.x, g.y), V, bounds_error=False, fill_value=0.)(boundary.pts)
    if np.sum(boundary.weights*v_b*sampled) < 0:
        V = _positive_boundary_velocity(ls, boundary, v_b, design_mask, band, s)
    return V


def _positive_boundary_velocity(ls, boundary, gradient, design_mask, band, sigma):
    """V=D G G^T D R^T W g / dx, so g^T W R V = ||G^T D R^T W g||²/dx.

    R is bilinear grid-to-boundary sampling, W the boundary arc quadrature,
    and D the band/design mask. Reflect Gaussian convolution is symmetric;
    two passes make the construction positive even for a truncated kernel.
    """
    grid = ls.grid
    coordinates = (boundary.pts-np.array([grid.x0, grid.y0]))/grid.dx
    valid = np.all((coordinates >= 0) & (coordinates <= [grid.nx-1, grid.ny-1]), axis=1)
    xy = coordinates[valid]
    lower = np.minimum(np.floor(xy).astype(int), [grid.nx-2, grid.ny-2])
    fraction = xy-lower
    source = gradient[valid]*boundary.weights[valid]/grid.dx
    scattered = np.zeros_like(ls.phi)
    for ix in (0, 1):
        for iy in (0, 1):
            weight = (fraction[:, 0] if ix else 1-fraction[:, 0])*(fraction[:, 1] if iy else 1-fraction[:, 1])
            np.add.at(scattered, (lower[:, 0]+ix, lower[:, 1]+iy), source*weight)
    mask = band.astype(float)
    if design_mask is not None:
        mask *= design_mask
    velocity = scattered*mask
    if sigma > 0:
        for _ in range(2):
            velocity = ndimage.gaussian_filter(velocity, sigma/np.sqrt(2), mode="reflect")
    return velocity*mask


# ----------------------------------------------------------------------------
# Level-set evolution:  phi_t + V |grad phi| = 0   (Eq. 28), Osher-Sethian upwind
# ----------------------------------------------------------------------------
def _hj_rhs(phi: np.ndarray, V: np.ndarray, dx: float) -> np.ndarray:
    p = np.pad(phi, 1, mode="edge")
    dmx = (p[1:-1, 1:-1] - p[:-2, 1:-1]) / dx
    dpx = (p[2:, 1:-1] - p[1:-1, 1:-1]) / dx
    dmy = (p[1:-1, 1:-1] - p[1:-1, :-2]) / dx
    dpy = (p[1:-1, 2:] - p[1:-1, 1:-1]) / dx
    grad_plus = np.sqrt(np.maximum(dmx, 0) ** 2 + np.minimum(dpx, 0) ** 2
                        + np.maximum(dmy, 0) ** 2 + np.minimum(dpy, 0) ** 2)
    grad_minus = np.sqrt(np.minimum(dmx, 0) ** 2 + np.maximum(dpx, 0) ** 2
                         + np.minimum(dmy, 0) ** 2 + np.maximum(dpy, 0) ** 2)
    return -(np.maximum(V, 0) * grad_plus + np.minimum(V, 0) * grad_minus)


def advect_levelset(phi: np.ndarray, V: np.ndarray, dt_total: float, dx: float,
                    cfl: float = 0.4) -> np.ndarray:
    """Evolve phi for pseudo-time dt_total with Heun (RK2) and CFL sub-stepping."""
    vmax = np.abs(V).max()
    if vmax == 0 or dt_total == 0:
        return phi
    n = int(np.ceil(abs(dt_total) * vmax / (cfl * dx)))
    dt = dt_total / n
    for _ in range(n):
        p1 = phi + dt * _hj_rhs(phi, V, dx)
        p2 = p1 + dt * _hj_rhs(p1, V, dx)
        phi = 0.5 * (phi + p2)
    return phi


# ----------------------------------------------------------------------------
# Constraints
# ----------------------------------------------------------------------------
def apply_constraints(
    ls: LevelSet,
    force_metal: Optional[np.ndarray] = None,
    force_air: Optional[np.ndarray] = None,
    anchor: Optional[np.ndarray] = None,
) -> None:
    """force_metal : e.g. feed lines / ports (must stay metal)
       force_air   : e.g. keep-out zones, the 'upper edge may not pass the through
                     line + 0.1 mm spacing' rule of the coupler example
       anchor      : metal not connected to this mask is deleted (keeps microstrip
                     branches connected -> in-phase outputs in the divider example)"""
    dx = ls.grid.dx
    if force_air is not None:
        ls.phi = np.where(force_air, np.maximum(ls.phi, dx), ls.phi)
    if force_metal is not None:
        ls.phi = np.where(force_metal, np.minimum(ls.phi, -dx), ls.phi)
    if anchor is not None:
        lab, n = ndimage.label(ls.phi < 0)
        keep = np.unique(lab[anchor & (ls.phi < 0)])
        keep = keep[keep > 0]
        orphan = (lab > 0) & ~np.isin(lab, keep)
        ls.phi = np.where(orphan, np.abs(ls.phi), ls.phi)
    ls.reinit()


# ----------------------------------------------------------------------------
# Problem interface and optimiser (paper Fig. 4)
# ----------------------------------------------------------------------------
@dataclass
class ForwardResult:
    s: complex                  # complex FoM quantity, e.g. S31 at the design frequency
    fields: FieldBundle         # forward E,H on the xy grid
    extra: object = None        # anything the solver wants to cache (LU factors, ...)


class Problem(Protocol):
    fom_kind: str               # 'abs2' | 'abs' | 'db' | 'real'
    prefactor: complex          # derived from the field/port normalization
    sample_offset: float        # field sampling offset from the boundary
    eps: float                  # permittivity at the sampling points (EPS0 for air)
    mu: float                   # permeability (MU0)

    def forward(self, ls: LevelSet) -> ForwardResult: ...
    def adjoint(self, ls: LevelSet, fwd: ForwardResult) -> FieldBundle: ...


@dataclass
class OptimizerConfig:
    maximize: bool = True
    pseudo_time_step: float = 1e-8
    cfl: float = 0.4
    smooth_sigma_cells: float = 1.5
    band_cells: float = 4.0
    max_step_mm: Optional[float] = None


class AdjointLevelSetOptimizer:
    def __init__(self, problem: Problem, design_mask: np.ndarray,
                 cfg: OptimizerConfig = OptimizerConfig(),
                 force_metal=None, force_air=None, anchor=None,
                 geometry_projector: Optional[Callable[[LevelSet], None]] = None,
                 geometry_evolution=None):
        self.p, self.design_mask, self.cfg = problem, design_mask, cfg
        self.force_metal, self.force_air, self.anchor = force_metal, force_air, anchor
        self.geometry_projector = geometry_projector
        self.geometry_evolution = geometry_evolution
        self.history: List[dict] = []
        self.stop_reason = "not_started"

    # one boundary velocity from one forward + one adjoint solve
    def velocity(self, ls: LevelSet, fwd: ForwardResult):
        if ls.geometry_state is not None:
            raise ValueError('Retained CAD uses geometry_evolution.prepare, not a scalar grid velocity')
        bnd = problem_boundary(self.p, ls, fwd, self.design_mask)
        g = problem_gradient(self.p, ls, fwd, bnd)
        f = problem_value(self.p, fwd)
        if not self.cfg.maximize:
            g = -g
        V = boundary_to_grid_velocity(ls, bnd, g, self.design_mask,
                                      self.cfg.band_cells,
                                      smooth_sigma_cells=self.cfg.smooth_sigma_cells)
        return V, f, bnd, g

    def run(self, ls: LevelSet, n_iter: int = 30,
            callback: Optional[Callable[[int, LevelSet, LevelSet, dict], None]] = None,
            state_callback: Optional[Callable[[int, LevelSet, ForwardResult], None]] = None,
            evaluation_callback: Optional[Callable[[LevelSet, ForwardResult, dict], None]] = None):
        """Commit each valid update, optionally capping its predicted displacement.

        The configured time step is independent of the current velocity
        magnitude by default, so a small shape derivative produces a proportionally small
        geometry update. If max_step_mm is set, each iteration uses the smaller
        of that fixed time and max_step_mm / max(abs(V)). CFL sub-stepping remains in
        place for numerical stability. FoM changes are recorded and do not
        control geometry acceptance or the time step. ``state_callback`` receives
        state 0 and every committed state. ``evaluation_callback`` receives each
        solve before another solve starts.
        """
        cfg, dx = self.cfg, ls.grid.dx
        if ls.geometry_state is not None and self.geometry_evolution is None:
            raise ValueError('Retained CAD requires a geometry-aware update; the current HJ loop updates phi only')
        if ls.geometry_state is None and self.geometry_evolution is not None:
            raise ValueError('Geometry-aware evolution requires a retained CAD state')
        if not np.isfinite(cfg.pseudo_time_step) or cfg.pseudo_time_step <= 0.0:
            raise ValueError("pseudo_time_step must be finite and positive")
        if cfg.max_step_mm is not None:
            if not np.isfinite(cfg.max_step_mm) or cfg.max_step_mm <= 0:
                raise ValueError("max_step_mm must be finite and positive")
            if ls.geometry_state is not None:
                raise ValueError("The displacement-cap experiment supports HJ updates only")
        displacement_tolerance = 1e-3 * dx
        fwd = self.p.forward(ls)
        f_cur = problem_value(self.p, fwd)
        if not np.isfinite(f_cur):
            raise RuntimeError("Initial geometry produced a non-finite objective")
        if evaluation_callback:
            evaluation_callback(ls, fwd, dict(
                phase="initial", it=None, accepted=None, reason="initial"))
        if state_callback:
            state_callback(0, ls, fwd)
        pseudo_time_step = cfg.pseudo_time_step
        self.stop_reason = "max_iterations"
        for it in range(n_iter):
            pseudo_time_step = cfg.pseudo_time_step
            cad_step = None
            if self.geometry_evolution is not None:
                cad_step = self.geometry_evolution.prepare(ls, self.p, fwd, self.design_mask, cfg)
                vmax = cad_step.vmax
            else:
                V, f, bnd, g = self.velocity(ls, fwd)
                vmax = float(np.max(np.abs(V)))
            if vmax == 0:
                rec = dict(it=it, f_before=f_cur, f_after=f_cur, delta_f=0.0,
                           relative_gain=0.0,
                           pseudo_time_step_used=pseudo_time_step,
                           pseudo_time_step_next=pseudo_time_step,
                           predicted_displacement=0.0, updated=False,
                           area=ls.area(), vmax=0.0, backtracks=0,
                           reason="converged_zero_velocity",
                           eval_id_before=fwd.extra.get("eval_id") if isinstance(fwd.extra, dict) else None,
                           eval_id_after=fwd.extra.get("eval_id") if isinstance(fwd.extra, dict) else None)
                self.history.append(rec)
                if callback:
                    callback(it, ls, ls, rec)
                self.stop_reason = "converged_zero_velocity"
                break

            if not np.isfinite(vmax):
                raise RuntimeError(f"Iteration {it} produced a non-finite velocity")
            if cfg.max_step_mm is not None:
                pseudo_time_step = min(pseudo_time_step, cfg.max_step_mm / vmax)
            predicted_displacement = pseudo_time_step * vmax
            if predicted_displacement <= displacement_tolerance:
                rec = dict(it=it, f_before=f_cur, f_after=f_cur, delta_f=0.0,
                           relative_gain=0.0,
                           pseudo_time_step_used=pseudo_time_step,
                           pseudo_time_step_next=pseudo_time_step,
                           predicted_displacement=predicted_displacement,
                           updated=False, area=ls.area(), vmax=vmax,
                           backtracks=0, reason="converged_small_displacement",
                           eval_id_before=fwd.extra.get("eval_id") if isinstance(fwd.extra, dict) else None,
                           eval_id_after=fwd.extra.get("eval_id") if isinstance(fwd.extra, dict) else None)
                self.history.append(rec)
                if callback:
                    callback(it, ls, ls, rec)
                self.stop_reason = "converged_small_displacement"
                break

            ls_before, fwd_before, f_before = ls, fwd, f_cur
            eval_id_before = (fwd_before.extra.get("eval_id")
                              if isinstance(fwd_before.extra, dict) else None)
            update_details = {}
            if cad_step is None:
                trial = ls.copy()
                trial.phi = advect_levelset(
                    trial.phi, V, pseudo_time_step, dx, cfl=cfg.cfl)
                apply_constraints(trial, self.force_metal, self.force_air, self.anchor)
            else:
                trial = cad_step.advance(pseudo_time_step)
                before_projection = trial.geometry_state.geometry
                self.geometry_evolution.apply_constraints(
                    trial, self.force_metal, self.force_air, self.anchor)
            if self.geometry_projector is not None:
                self.geometry_projector(trial)
            if cad_step is not None:
                trial.geometry_state.validate_phi(trial.phi)
                final_geometry = trial.geometry_state.geometry
                update_details = dict(
                    geometry_update="cad",
                    linearized_signed_work=pseudo_time_step*cad_step.signed_work,
                    linearization_matches_final_geometry=before_projection.equals(final_geometry),
                    projection_added_area=float(final_geometry.difference(before_projection).area),
                    projection_removed_area=float(before_projection.difference(final_geometry).area))
            fwd = self.p.forward(trial)
            f_cur = problem_value(self.p, fwd)
            if not np.isfinite(f_cur):
                raise RuntimeError(f"Iteration {it} produced a non-finite objective")
            delta_f = f_cur - f_before
            objective_gain = delta_f if cfg.maximize else -delta_f
            relative_gain = objective_gain / max(abs(f_before), 1e-12)
            if evaluation_callback:
                evaluation_callback(trial, fwd, dict(
                    phase="trial", it=it, backtracks=0,
                    pseudo_time_step_used=pseudo_time_step,
                    predicted_displacement=predicted_displacement,
                    eval_id_before=eval_id_before, f_before=f_before,
                    delta_f=delta_f, objective_gain=objective_gain,
                    accepted=True, reason="committed", **update_details))
            eval_id_after = (fwd.extra.get("eval_id")
                             if isinstance(fwd.extra, dict) else None)
            rec = dict(
                it=it, f_before=f_before, f_after=f_cur, delta_f=delta_f,
                objective_gain=objective_gain,
                relative_gain=relative_gain,
                pseudo_time_step_used=pseudo_time_step,
                pseudo_time_step_next=pseudo_time_step,
                predicted_displacement=pseudo_time_step * vmax,
                updated=True, area=trial.area(), vmax=vmax,
                backtracks=0,
                eval_id_before=eval_id_before, eval_id_after=eval_id_after,
                reason="committed", **update_details,
            )
            self.history.append(rec)
            if callback:
                callback(it, ls_before, trial, rec)
            ls = trial
            if state_callback:
                state_callback(it + 1, ls, fwd)
        return ls


# ----------------------------------------------------------------------------
# Gradient verification (do this FIRST on any new solver adapter)
# ----------------------------------------------------------------------------
def problem_value(problem: Problem, fwd: ForwardResult) -> float:
    """Evaluate the same objective for initial and updated geometries."""
    if hasattr(problem, "objective_value"):
        return float(problem.objective_value(fwd))
    return fom_value_and_weight(fwd.s, problem.fom_kind)[0]


def problem_gradient(problem: Problem, ls: LevelSet, fwd: ForwardResult,
                     boundary: Boundary) -> np.ndarray:
    if hasattr(problem, "boundary_gradient"):
        return problem.boundary_gradient(ls, fwd, boundary)
    adj = problem.adjoint(ls, fwd)
    return np.real(shape_derivative_complex(
        boundary, fwd.fields, adj, problem.prefactor, problem.sample_offset,
        getattr(problem, "eps", EPS0), getattr(problem, "mu", MU0)))


def problem_boundary(problem: Problem, ls: LevelSet, fwd: ForwardResult,
                     design_mask=None) -> Boundary:
    if hasattr(problem, "boundary"):
        return problem.boundary(ls, fwd, design_mask)
    return extract_boundary(ls, design_mask)


def gradient_check(problem: Problem, ls: LevelSet, design_mask=None,
                   deltas=(0.05, 0.1, 0.2), evaluation_callback=None) -> List[dict]:
    """Compare  sum_i g_i * w_i * delta  (adjoint prediction for a uniform outward
    shift of the whole boundary by delta) with a central finite difference of the
    objective obtained by solving with phi -/+ delta. The interface must be a
    signed-distance representation, the exported geometry must realize that
    displacement to first order, and g must have physical normalization.
    Otherwise the ratio cannot certify the derivative or calibrate a prefactor.
    """
    if ls.geometry_state is not None:
        raise ValueError('Retained CAD finite differences require explicit CAD perturbations, not phi +/- delta')
    dx = ls.grid.dx
    fwd = problem.forward(ls)
    if evaluation_callback:
        evaluation_callback(ls, fwd, dict(
            phase="gradient_base", it=None, accepted=None, reason="gradient_check"))
    bnd = problem_boundary(problem, ls, fwd, design_mask)
    g = problem_gradient(problem, ls, fwd, bnd)
    if design_mask is not None:
        g = np.where(bnd.active, g, 0.0)
    slope_adj = float(np.sum(g * bnd.weights))            # d f / d(delta)
    rows = []
    for d in deltas:
        dd = d * dx
        lp, lm = ls.copy(), ls.copy()
        lp.phi = ls.phi - dd       # metal grows
        lm.phi = ls.phi + dd       # metal shrinks
        fwd_p = problem.forward(lp)
        fp = problem_value(problem, fwd_p)
        if evaluation_callback:
            evaluation_callback(lp, fwd_p, dict(
                phase="gradient_plus", it=None, accepted=None,
                delta_cells=d, reason="gradient_check"))
        fwd_m = problem.forward(lm)
        fm = problem_value(problem, fwd_m)
        if evaluation_callback:
            evaluation_callback(lm, fwd_m, dict(
                phase="gradient_minus", it=None, accepted=None,
                delta_cells=d, reason="gradient_check"))
        slope_fd = (fp - fm) / (2 * dd)
        rows.append(dict(delta_cells=d, slope_adjoint=slope_adj, slope_fd=slope_fd,
                         ratio=slope_adj / slope_fd if slope_fd else float("nan")))
    return rows


# ----------------------------------------------------------------------------
# NOTES FOR THE CST (or HFSS/FEKO) SIDE -- not tested against CST
# ----------------------------------------------------------------------------
# Per iteration the solver adapter must:
#   forward(): rebuild geometry from boundary_polygons(ls) (extrude to metal
#       thickness), run the time/frequency-domain solve with E and H monitors at
#       f0, export E,H on a regular xy grid in the metal mid-plane, return S.
#   adjoint(): same geometry, excite the *output* port instead (the "backward"
#       simulation), export E,H at f0 -> FieldBundle.
# Derive the prefactor from the reciprocal port/field normalization; validate
# with an independent reference, rather than fitting gradient_check ratios.
# Keep the ~0.1-0.5 mm boundary point
# spacing recommended in the paper's Sec. III-C (this is the grid dx here).
