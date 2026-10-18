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
6. The outer loop of Fig. 4 with an accept/reject step-size control [Fig. 4]

What it does NOT do: run the Maxwell solver. You plug that in through the small
`Problem` interface (forward(), adjoint()). See demo_fdfd_check.py for a runnable
example and CST-side notes at the bottom of this file.

Conventions (read these once, they matter)
------------------------------------------
* Arrays are indexed [ix, iy]  (indexing="ij").
* phi < 0  : metal,   phi > 0 : air,   phi = 0 : boundary.
* Outward normal n = grad(phi)/|grad(phi)| points from metal INTO air.
* V > 0 means the metal GROWS along n (phi_t = -V|grad phi| decreases phi).
* The shape gradient g_i is "change of the objective per unit displacement of
  boundary point i along n", i.e. per unit metal growth.
* Time convention e^{+j w t}, as in the paper (curl E = -j w mu H).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Tuple

import numpy as np
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator
from scipy.spatial import cKDTree
from skimage import measure

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

    def __init__(self, grid: Grid2D, phi: np.ndarray):
        self.grid = grid
        self.phi = np.asarray(phi, dtype=float).copy()

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
        return LevelSet(self.grid, self.phi)

    # -- geometry helpers --------------------------------------------------
    def reinit(self) -> None:
        """Restore |grad phi| = 1 (signed distance). Uses scikit-fmm if available."""
        phi = self.phi
        if not (phi.min() < 0 < phi.max()):
            return
        try:
            import skfmm

            self.phi = np.asarray(skfmm.distance(phi, dx=self.grid.dx))
        except ImportError:  # coarse fallback, pixel-accurate only
            dx = self.grid.dx
            inside = phi < 0
            self.phi = (
                ndimage.distance_transform_edt(~inside)
                - ndimage.distance_transform_edt(inside)
            ) * dx

    def metal_mask(self) -> np.ndarray:
        return self.phi < 0

    def area(self) -> float:
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
    for c in measure.find_contours(ls.phi, 0.0):
        c = measure.approximate_polygon(c, tolerance=tol / g.dx)
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

    prefactor : -2j*w/P_in in the paper. It depends on how your solver
                normalises the port modes and the backward excitation, so
                calibrate it once with `gradient_check` (a single scalar).
    offset    : distance (>0 into air, <0 into metal) from the boundary at which the
                fields are sampled. Paper: samples slightly inside the metal for
                lossy/finite-conductivity metal. For a PEC-like export where the
                metal interior is zero, sample just outside, ~0.5-1.5 cells.
    The products are bilinear (no complex conjugate), as in Eq. (20)/(25).
    """
    ps = boundary.pts + offset * boundary.normals
    Ef, Hf = fwd.sample(ps)
    Eb, Hb = adj.sample(ps)
    n3 = np.vstack([boundary.normals.T, np.zeros(len(ps))])      # (3,N)
    dot = lambda a, b: np.sum(a * b, axis=0)
    e_perp = dot(Ef, n3) * dot(Eb, n3)                           # E_perp . Eb_perp
    h_par = dot(Hf, Hb) - dot(Hf, n3) * dot(Hb, n3)              # H_par . Hb_par
    return prefactor * (eps * e_perp + mu * h_par)


def fom_value_and_weight(s: complex, kind: str = "abs2") -> Tuple[float, complex]:
    """Real objective f(s) and weight c such that  df = Re( conj(c) * ds ).
    Lets you optimise |S|^2, |S| or S in dB from the *complex* sensitivity."""
    a = abs(s) + 1e-300
    if kind == "abs2":
        return a ** 2, 2 * s
    if kind == "abs":
        return a, s / a
    if kind == "db":
        return 20 * np.log10(a), (20 / np.log(10)) * s / a ** 2
    if kind == "real":
        return float(np.real(s)), 1.0 + 0j
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
    normalised convolution.  Smoothing = cheap Sobolev-type regularisation: it
    suppresses grid-scale ripples and enforces a minimum feature scale."""
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
    return V


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
    prefactor: complex          # calibrated, see gradient_check
    sample_offset: float        # field sampling offset from the boundary
    eps: float                  # permittivity at the sampling points (EPS0 for air)
    mu: float                   # permeability (MU0)

    def forward(self, ls: LevelSet) -> ForwardResult: ...
    def adjoint(self, ls: LevelSet, fwd: ForwardResult) -> FieldBundle: ...


@dataclass
class OptimizerConfig:
    maximize: bool = True
    step_cells: float = 0.8        # max boundary displacement per iteration (cells)
    step_min_cells: float = 0.05
    step_max_cells: float = 1.5
    cfl: float = 0.4
    smooth_sigma_cells: float = 1.5
    band_cells: float = 4.0
    max_backtracks: int = 5
    reinit_every: int = 1


class AdjointLevelSetOptimizer:
    def __init__(self, problem: Problem, design_mask: np.ndarray,
                 cfg: OptimizerConfig = OptimizerConfig(),
                 force_metal=None, force_air=None, anchor=None):
        self.p, self.design_mask, self.cfg = problem, design_mask, cfg
        self.force_metal, self.force_air, self.anchor = force_metal, force_air, anchor
        self.history: List[dict] = []

    # one boundary velocity from one forward + one adjoint solve
    def velocity(self, ls: LevelSet, fwd: ForwardResult):
        bnd = extract_boundary(ls, self.design_mask)
        adj = self.p.adjoint(ls, fwd)
        g_c = shape_derivative_complex(bnd, fwd.fields, adj, self.p.prefactor,
                                       self.p.sample_offset,
                                       getattr(self.p, "eps", EPS0), getattr(self.p, "mu", MU0))
        f, c = fom_value_and_weight(fwd.s, self.p.fom_kind)
        g = real_gradient(g_c, c)
        if not self.cfg.maximize:
            g = -g
        V = boundary_to_grid_velocity(ls, bnd, g, self.design_mask,
                                      self.cfg.band_cells,
                                      smooth_sigma_cells=self.cfg.smooth_sigma_cells)
        return V, f, bnd, g

    def run(self, ls: LevelSet, n_iter: int = 30,
            callback: Optional[Callable[[int, LevelSet, dict], None]] = None,
            state_callback: Optional[Callable[[int, LevelSet, ForwardResult], None]] = None):
        """Run iterations; ``state_callback`` receives state 0 and each accepted update."""
        cfg, dx = self.cfg, ls.grid.dx
        sign = 1.0 if cfg.maximize else -1.0
        fwd = self.p.forward(ls)
        f_cur = fom_value_and_weight(fwd.s, self.p.fom_kind)[0]
        if state_callback:
            state_callback(0, ls, fwd)
        step = cfg.step_cells
        for it in range(n_iter):
            V, f, bnd, g = self.velocity(ls, fwd)
            vmax = np.abs(V).max()
            if vmax == 0:
                break
            accepted = False
            for bt in range(cfg.max_backtracks + 1):
                trial = ls.copy()
                trial.phi = advect_levelset(
                    trial.phi, V, step * dx / vmax, dx, cfl=cfg.cfl)
                apply_constraints(trial, self.force_metal, self.force_air, self.anchor)
                fwd_t = self.p.forward(trial)
                f_t = fom_value_and_weight(fwd_t.s, self.p.fom_kind)[0]
                if sign * (f_t - f_cur) > 0:
                    accepted = True
                    break
                step = max(step * 0.5, cfg.step_min_cells)
            rec = dict(it=it, f=f_cur, f_trial=f_t, step=step, accepted=accepted,
                       area=ls.area(), vmax=vmax, backtracks=bt)
            self.history.append(rec)
            if callback:
                callback(it, ls, rec)
            if not accepted:
                break
            ls, fwd, f_cur = trial, fwd_t, f_t
            if state_callback:
                state_callback(it + 1, ls, fwd)
            step = min(step * 1.25, cfg.step_max_cells)
        self.history.append(dict(it=len(self.history), f=f_cur, final=True))
        return ls


# ----------------------------------------------------------------------------
# Gradient verification (do this FIRST on any new solver adapter)
# ----------------------------------------------------------------------------
def gradient_check(problem: Problem, ls: LevelSet, design_mask=None,
                   deltas=(0.05, 0.1, 0.2)) -> List[dict]:
    """Compare  sum_i g_i * w_i * delta  (adjoint prediction for a uniform outward
    shift of the whole boundary by delta) with a central finite difference of the
    objective obtained by solving with phi -/+ delta.  Requires ls to be a signed
    distance function.  Expect ratio ~ 1.  A ratio ~ -1 means the normal / sign
    convention is flipped; a constant ratio != 1 means `prefactor` needs rescaling."""
    dx = ls.grid.dx
    fwd = problem.forward(ls)
    adj = problem.adjoint(ls, fwd)
    bnd = extract_boundary(ls, design_mask)
    g_c = shape_derivative_complex(bnd, fwd.fields, adj, problem.prefactor,
                                   problem.sample_offset,
                                   getattr(problem, "eps", EPS0), getattr(problem, "mu", MU0))
    _, c = fom_value_and_weight(fwd.s, problem.fom_kind)
    g = real_gradient(g_c, c)
    if design_mask is not None:
        g = np.where(bnd.active, g, 0.0)
    slope_adj = float(np.sum(g * bnd.weights))            # d f / d(delta)
    rows = []
    for d in deltas:
        dd = d * dx
        lp, lm = ls.copy(), ls.copy()
        lp.phi = ls.phi - dd       # metal grows
        lm.phi = ls.phi + dd       # metal shrinks
        fp = fom_value_and_weight(problem.forward(lp).s, problem.fom_kind)[0]
        fm = fom_value_and_weight(problem.forward(lm).s, problem.fom_kind)[0]
        slope_fd = (fp - fm) / (2 * dd)
        rows.append(dict(delta_cells=d, slope_adjoint=slope_adj, slope_fd=slope_fd,
                         ratio=slope_adj / slope_fd))
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
# Then `prefactor` = -2j*w/P_in (Eq. 25) up to the port-normalisation convention:
# verify with gradient_check and rescale.  Keep the ~0.1-0.5 mm boundary point
# spacing recommended in the paper's Sec. III-C (this is the grid dx here).
