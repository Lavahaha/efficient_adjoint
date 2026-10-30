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
6. The outer loop with fixed pseudo-time and FoM backtracking

What it does NOT do: run the Maxwell solver. You plug that in through the small
`Problem` interface (forward(), adjoint()). See
`scripts/run_coupler_adjoint_levelset.py` for the CST adapter and the notes below.

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
    pseudo_time_step: float = 1e-8
    cfl: float = 0.4
    smooth_sigma_cells: float = 1.5
    band_cells: float = 4.0


class AdjointLevelSetOptimizer:
    def __init__(self, problem: Problem, design_mask: np.ndarray,
                 cfg: OptimizerConfig = OptimizerConfig(),
                 force_metal=None, force_air=None, anchor=None,
                 geometry_projector: Optional[Callable[[LevelSet], None]] = None):
        self.p, self.design_mask, self.cfg = problem, design_mask, cfg
        self.force_metal, self.force_air, self.anchor = force_metal, force_air, anchor
        self.geometry_projector = geometry_projector
        self.history: List[dict] = []
        self.stop_reason = "not_started"

    # one boundary velocity from one forward + one adjoint solve
    def velocity(self, ls: LevelSet, fwd: ForwardResult):
        bnd = extract_boundary(ls, self.design_mask)
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
        """Evolve with a fixed pseudo-time step and backtrack non-improving trials.

        The configured time step is independent of the current velocity
        magnitude, so a small shape derivative produces a proportionally small
        geometry update. CFL sub-stepping in :func:`advect_levelset` remains in
        place for numerical stability. A trial is committed only when its FoM
        improves; otherwise the same direction is retried with half the time
        step. ``state_callback`` receives state 0 and every accepted state.
        ``evaluation_callback`` receives every solved candidate, including
        rejected trials and the final restore, before another solve starts.
        """
        cfg, dx = self.cfg, ls.grid.dx
        if not np.isfinite(cfg.pseudo_time_step) or cfg.pseudo_time_step <= 0.0:
            raise ValueError("pseudo_time_step must be finite and positive")
        max_backtracks = 12
        backtrack_factor = 0.5
        displacement_tolerance = 1e-3 * dx
        fwd = self.p.forward(ls)
        f_cur = problem_value(self.p, fwd)
        if evaluation_callback:
            evaluation_callback(ls, fwd, dict(
                phase="initial", it=None, accepted=None, reason="initial"))
        if state_callback:
            state_callback(0, ls, fwd)
        pseudo_time_step = cfg.pseudo_time_step
        self.stop_reason = "max_iterations"
        for it in range(n_iter):
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
            accepted = False
            backtracks = 0
            trial_fwd = None
            trial = None
            trial_f = f_before
            trial_tau = pseudo_time_step
            small_step_reached = False
            for backtracks in range(max_backtracks + 1):
                if trial_tau * vmax <= displacement_tolerance:
                    small_step_reached = True
                    break
                trial = ls.copy()
                trial.phi = advect_levelset(
                    trial.phi, V, trial_tau, dx, cfl=cfg.cfl)
                apply_constraints(trial, self.force_metal, self.force_air, self.anchor)
                if self.geometry_projector is not None:
                    self.geometry_projector(trial)
                if np.max(np.abs(trial.phi - ls.phi)) > 1e-12 * max(dx, 1.0):
                    trial_fwd = self.p.forward(trial)
                    trial_f = problem_value(self.p, trial_fwd)
                    delta_f = trial_f - f_before
                    objective_gain = delta_f if cfg.maximize else -delta_f
                    accepted = bool(np.isfinite(objective_gain) and objective_gain > 0.0)
                    if evaluation_callback:
                        evaluation_callback(trial, trial_fwd, dict(
                            phase="trial", it=it, backtracks=backtracks,
                            pseudo_time_step_used=trial_tau,
                            predicted_displacement=trial_tau * vmax,
                            eval_id_before=eval_id_before, f_before=f_before,
                            delta_f=delta_f, objective_gain=objective_gain,
                            accepted=accepted,
                            reason=("committed" if accepted else "no_improvement")))
                    if accepted:
                        break
                if backtracks < max_backtracks:
                    trial_tau *= backtrack_factor

            if not accepted:
                # A rejected trial may have replaced the solver's active
                # geometry. Restore the last accepted shape before returning.
                fwd = self.p.forward(ls)
                if evaluation_callback:
                    evaluation_callback(ls, fwd, dict(
                        phase="restore", it=it, accepted=None,
                        eval_id_before=eval_id_before, f_before=f_before,
                        reason="restore_last_accepted"))
                restored_eval_id = (fwd.extra.get("eval_id")
                                    if isinstance(fwd.extra, dict) else None)
                rec = dict(
                    it=it, f_before=f_before, f_after=f_before, delta_f=0.0,
                    objective_gain=0.0, relative_gain=0.0,
                    pseudo_time_step_used=trial_tau,
                    pseudo_time_step_next=trial_tau,
                    predicted_displacement=trial_tau * vmax,
                    updated=False, area=ls.area(), vmax=vmax,
                    backtracks=backtracks, eval_id_before=eval_id_before,
                    eval_id_after=restored_eval_id,
                    reason=("converged_small_displacement" if small_step_reached
                            else "line_search_failed"),
                )
                self.history.append(rec)
                if callback:
                    callback(it, ls_before, ls_before, rec)
                self.stop_reason = (
                    "converged_small_displacement" if small_step_reached
                    else "line_search_failed")
                break

            fwd = trial_fwd
            f_cur = trial_f
            delta_f = f_cur - f_before
            objective_gain = delta_f if cfg.maximize else -delta_f
            relative_gain = objective_gain / max(abs(f_before), 1e-12)
            pseudo_time_step = trial_tau
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
                backtracks=backtracks,
                eval_id_before=eval_id_before, eval_id_after=eval_id_after,
                reason="committed",
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
    """Evaluate the same objective for initial, trial and restored geometries."""
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


def gradient_check(problem: Problem, ls: LevelSet, design_mask=None,
                   deltas=(0.05, 0.1, 0.2), evaluation_callback=None) -> List[dict]:
    """Compare  sum_i g_i * w_i * delta  (adjoint prediction for a uniform outward
    shift of the whole boundary by delta) with a central finite difference of the
    objective obtained by solving with phi -/+ delta.  Requires ls to be a signed
    distance function.  Expect ratio ~ 1.  A ratio ~ -1 means the normal / sign
    convention is flipped; a constant ratio != 1 means `prefactor` needs rescaling."""
    dx = ls.grid.dx
    fwd = problem.forward(ls)
    if evaluation_callback:
        evaluation_callback(ls, fwd, dict(
            phase="gradient_base", it=None, accepted=None, reason="gradient_check"))
    bnd = extract_boundary(ls, design_mask)
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
# Then `prefactor` = -2j*w/P_in (Eq. 25) up to the port-normalisation convention:
# verify with gradient_check and rescale.  Keep the ~0.1-0.5 mm boundary point
# spacing recommended in the paper's Sec. III-C (this is the grid dx here).
