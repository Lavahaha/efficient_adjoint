"""图纸约定：设计区 / 允许区 / 固定区怎么画（迭代快照与布局参考图共用一套）。

2026-10-05：迭代快照（``iter_NNN.png``）里**只画了固定区**（直通线）的红框、
没画设计区，用户把它当成了"设计区画错了"（原话："红线框出的应该是设计
区域"）；``docs/layout_reference.png`` 里同一个红框又是红实线，同样的误读。
两份图的视觉语言因此收进本模块——**改一处，两张图同时跟着变**。

约定（别再拿红色画其它东西）：

    设计区 design_region.box   红虚线（论文 Fig.5 的红虚线框 = 可动范围）
    允许区 constraints.allowed_region  橙点线（只在盒子比活动范围大时才画）
    固定区 fixed_region        蓝实线

设计区与允许区**本就是一个东西**（论文只有一个域）：本算例不再单列
``allowed_region``，红虚线框就是论文图里那个框；橙色那层只在旧式配置
（盒子兼作数值网格范围）里才会出现。

迭代快照里的金属用 :data:`METAL_COLOR`（**深色**）按 φ=0 的等值线填色：
早先用 ``copper``（1 端是浅色）配 ``phi < 0``，金属是浅色、背景反倒全黑，
看起来"整个设计区都被金属填满了"；后来改用按节点上色的 ``pcolormesh``，
单元画在节点上方，整块金属看着平移了半格。

2026-10-12：本模块还管**电场分布图**与**收敛过程图**（论文 Fig.10/11 那类）：

    iter_NNN.png            左 = φ 俯视（现状），右 = 采样面 |E|
    sparams_vs_iter.png     各响应端口 |S_i1|（dB）随迭代，FoM 那条加粗
    efield_evolution.png    等间隔若干轮的 |E| 演化（共享色标）

电场一律画在**实际使用的采样面**（请求 z=0 时优先取最近 z>=0 面）上——
那是 δp 取场的位置（论文式 54 的配对就来自顶面 PEC 条件），标题里写明
z 值，免得被当成顶面场（顶面上切向 E 恒为零，画出来是黑的）。

所有函数**只吃数据对象**（ls / FieldGrid / 配置），matplotlib 在函数内
import、不依赖 CST——本机、服务器、事后重画脚本用的是同一套代码。
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "METAL_COLOR", "AIR_COLOR", "SUBSTRATE_COLOR", "OUTSIDE_COLOR",
    "REGIONS", "region_outlines", "draw_regions",
    "draw_metal_contour", "field_magnitude", "draw_field_map",
    "load_ls", "snapshot_figure", "sparams_curves", "sparams_figure",
    "efield_evolution_figure", "adjoint_levelset_state_figure",
    "adjoint_levelset_sparams_figure",
]

#: 金属 / 空气 / 基板 / 设计区之外的底色（与 layout_reference.png 同色系）
METAL_COLOR = "#333333"
AIR_COLOR = "#ffffff"
SUBSTRATE_COLOR = "#ececec"
OUTSIDE_COLOR = "#f2f2f2"

#: 三类区域框的颜色 / 线型 / 图例文案（label 同时是两张图的图例文字）
REGIONS = {
    "design": dict(label="design region (design_region.box)",
                   color="#d62728", ls="--", lw=1.5),
    "allowed": dict(label="allowed region (movable extent)",
                    color="#e08000", ls=(0, (2, 3)), lw=1.4),
    "fixed": dict(label="fixed region (feeds)",   # 耦合器=直通线/腿，功分器=三条馈线
                  color="#1f77b4", ls="-", lw=1.2),
}


def _rect(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """矩形折线（首点重复一次，闭合）。"""
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], dtype=float)


def region_outlines(cfg) -> list[tuple[str, np.ndarray]]:
    """``[(区域名, 折线 (n, 2))]``：设计区、允许区（可选）、固定区（每多边形一条）。"""
    out: list[tuple[str, np.ndarray]] = []
    box = cfg.design_region.box
    out.append(("design", _rect(box.x[0], box.y[0], box.x[1], box.y[1])))
    area = getattr(cfg.constraints, "allowed_region", None)
    if area is not None:
        out.append(("allowed", _rect(area.x[0], area.y[0], area.x[1], area.y[1])))
    for poly in getattr(cfg, "fixed_region", None) or []:
        v = np.asarray(poly.vertices, dtype=float)
        out.append(("fixed", np.vstack([v, v[:1]])))
    return out


def draw_regions(ax, cfg, *, zorder: int = 6, legend: bool = True):
    """把三类区域框画到 ``ax`` 上，返回图例句柄（已带 label，调用方自己 ``legend``）。"""
    handles = []
    for name, pts in region_outlines(cfg):
        st = REGIONS[name]
        (ln,) = ax.plot(pts[:, 0], pts[:, 1], color=st["color"], ls=st["ls"],
                        lw=st["lw"], zorder=zorder,
                        label=st["label"] if legend else None)
        handles.append(ln)
    return handles


def _design_view(ax, cfg, *, pad: float = 0.5) -> None:
    """视野 = 设计区 ± ``pad`` mm（固定馈线在 x 上比设计区宽，不夹住会把视野撑开）。"""
    box = cfg.design_region.box
    ax.set_xlim(box.x[0] - pad, box.x[1] + pad)
    ax.set_ylim(box.y[0] - pad, box.y[1] + pad)
    ax.set_aspect("equal")


# ---------------------------------------------------------------------- #
# 电场分布（论文 Fig.10 那类图）
# ---------------------------------------------------------------------- #
def draw_metal_contour(ax, ls, *, color: str = METAL_COLOR, lw: float = 0.8,
                       zorder: int = 5):
    """金属轮廓 = φ=0 的零等值线（细线，叠在场图上）。"""
    return ax.contour(ls.xs, ls.ys, ls.phi.T, levels=[0.0], colors=[color],
                      linewidths=lw, zorder=zorder)


def field_magnitude(slice_grid) -> np.ndarray:
    """单层切片的 |E| = sqrt(|Ex|²+|Ey|²+|Ez|²)，返回 (nx, ny)。"""
    d = np.asarray(slice_grid.data)
    if d.shape[2] != 1:
        raise ValueError(f"切片应是单层（nz=1），收到 {d.shape}")
    return np.sqrt(np.sum(np.abs(d[:, :, 0, :]) ** 2, axis=-1))


def draw_field_map(ax, slice_grid, *, ls=None, cfg=None, vmin=None, vmax=None,
                   cmap: str = "inferno", colorbar: bool = True,
                   label: str = "|E| (V/m)"):
    """单层场切片画成俯视图（imshow），返回 image 句柄。

    imshow 的像素是**格心**，所以 extent 用轴端 ± 半格；``ls`` 叠金属轮廓、
    ``cfg`` 叠三类区域框。演化图共享色标时，各面板传同一个 vmin/vmax。
    """
    xs, ys = slice_grid.axes()[0], slice_grid.axes()[1]
    dx, dy = slice_grid.spacing[0], slice_grid.spacing[1]
    ext = (xs[0] - dx / 2, xs[-1] + dx / 2, ys[0] - dy / 2, ys[-1] + dy / 2)
    im = ax.imshow(field_magnitude(slice_grid).T,  # 行 = y
                   origin="lower", extent=ext, cmap=cmap,
                   vmin=vmin, vmax=vmax, aspect="equal",
                   interpolation="nearest", zorder=1)
    if ls is not None:
        # 场图底色是深色系：金属轮廓用白色细线（METAL_COLOR 深灰在上面看不见）
        draw_metal_contour(ax, ls, color="white", lw=0.7)
    if cfg is not None:
        draw_regions(ax, cfg, legend=False)
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    if colorbar:
        ax.figure.colorbar(im, ax=ax, fraction=0.046, pad=0.02, label=label)
    return im


def load_ls(outdir, n: int, cfg):
    """从 ``iter_NNN/ls_phi.npz`` 重建 φ 网格（事后重画用；读法与 cst_update 一致）。"""
    from eaopt.artifacts import iteration_dir, load_ls_phi
    from eaopt.geometry.levelset import LevelSet2D

    snap = load_ls_phi(iteration_dir(outdir, n) / "ls_phi.npz")
    ls = LevelSet2D(cfg.design_region.box, cfg.design_region.grid_step_mm)
    if snap["phi"].shape != (ls.nx, ls.ny):
        raise ValueError(
            f"iter_{n:03d} 的 φ 尺寸 {snap['phi'].shape} 与当前配置的网格 "
            f"{(ls.nx, ls.ny)} 不符——配置改过了？旧结果目录要么重跑、"
            "要么用当时的配置来画。")
    ls.phi = snap["phi"]
    return ls


def snapshot_figure(cfg, ls, *, it: int, e_slice=None, z_used=None, path=None,
                    dpi: int = 110):
    """每轮快照 ``iter_NNN.png``：左 = φ 俯视，右 = 采样面 |E|（没有切片则单栏）。

    左栏必须画**设计区**（红虚线）；金属按 φ=0 的零等值线填色——别用
    ``phi < 0`` 判断（界面压着节点时 φ 是 ±1e-15 的噪声，会画出一排假毛刺），
    也别用按节点上色的 pcolormesh（单元画在节点上方，看着平移半格）。
    这两条都是踩过的坑，见模块开头的说明。
    """
    import matplotlib.pyplot as plt

    ncol = 2 if e_slice is not None else 1
    fig, axes = plt.subplots(1, ncol, figsize=(9 * ncol + 4 * (ncol - 1), 4.6),
                             squeeze=False)
    ax = axes[0, 0]
    ax.set_facecolor(OUTSIDE_COLOR)              # 设计区之外不是优化对象
    box = cfg.design_region.box
    ax.add_patch(plt.Rectangle((box.x[0], box.y[0]), box.width, box.height,
                               facecolor=AIR_COLOR, lw=0, zorder=1))
    ax.contourf(ls.xs, ls.ys, ls.phi.T, levels=[-1e9, 0.0],
                colors=[METAL_COLOR], zorder=2)
    draw_regions(ax, cfg)
    _design_view(ax, cfg)
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(f"iteration {it}", pad=34)   # 图例两行时 24 会压住标题
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3,
              fontsize=8, frameon=False)

    if e_slice is not None:
        ax2 = axes[0, 1]
        ax2.set_facecolor(OUTSIDE_COLOR)
        draw_field_map(ax2, e_slice, ls=ls, cfg=cfg)
        # 图上的文字一律 ASCII：matplotlib 默认字体没有 CJK 字形，中文会画成
        # 一串方框（plot_layout.py 里踩过同一个坑）
        zt = "" if z_used is None else f" @ z = {z_used:g} mm"
        ax2.set_title(f"|E|{zt}", pad=10)
        _design_view(ax2, cfg)                   # 两栏同视野，便于左右对照
    if path is not None:
        fig.savefig(path, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
    return fig


# ---------------------------------------------------------------------- #
# 收敛过程（论文 Fig.11 那类图）
# ---------------------------------------------------------------------- #
def sparams_curves(outdir, cfg) -> tuple[list[int], dict]:
    """读所有**已完成**轮次的 fwd S 参数 → ``(迭代号, {(响应, 激励): [dB, ...]})``。

    dB = 20log10|S|。响应端口从 JSON 的键推（不依赖 CST 模板），激励端口取
    ``cfg.objective.from_port``；读不到 S 参数的轮次跳过。
    """
    from eaopt.artifacts import list_iterations, load_s_params

    exc = int(cfg.objective.from_port)
    used: list[int] = []
    series: dict[tuple[int, int], list[float]] = {}
    for n in list_iterations(outdir):
        cur = {k: float(20 * np.log10(abs(v)))
               for k, v in load_s_params(outdir, n, "fwd").items()
               if k[1] == exc}
        if not cur:
            continue
        used.append(n)
        for k, v in cur.items():
            series.setdefault(k, []).append(v)
    return used, series


def sparams_figure(cfg, outdir, path):
    """各响应端口 |S_i1| 随迭代（论文 Fig.11）：FoM 那条加粗、带标记。"""
    import matplotlib.pyplot as plt

    from eaopt.optimize.objective import fom_key

    its, series = sparams_curves(outdir, cfg)
    if not its:
        print(f"[图] {outdir} 下没有可读的 s_params_fwd.json，跳过 S 参数曲线图")
        return None
    fkey = fom_key(cfg)
    fig, ax = plt.subplots(figsize=(6.6, 4.2))
    for k in sorted(series):
        is_fom = k == fkey
        ax.plot(its, series[k],
                marker="o" if is_fom else None, ms=3.5,
                lw=2.0 if is_fom else 1.1,
                alpha=1.0 if is_fom else 0.8,
                label=(f"FoM = |S{k[0]}{k[1]}|" if is_fom else f"|S{k[0]}{k[1]}|"))
    ax.set_xlabel("iteration")
    ax.set_ylabel("|S| (dB)")
    ax.set_title(f"{cfg.name}: S parameters vs iteration")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    if len(its) <= 25:
        ax.set_xticks(its)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path


def efield_evolution_figure(cfg, outdir, path, *, max_panels: int = 6):
    """等间隔若干轮的采样面 |E| 演化（论文 Fig.10）：共享色标，附金属轮廓。"""
    import matplotlib.pyplot as plt

    from eaopt.artifacts import iteration_dir, list_iterations, load_field_slice

    have = [n for n in list_iterations(outdir)
            if (iteration_dir(outdir, n) / "efield_fwd.npz").is_file()]
    if not have:
        print("[图] 没有任何 iter_NNN/efield_fwd.npz（旧轮次没有这个产物）"
              "→ 跳过电场演化图")
        return None
    pick = [have[i] for i in np.unique(np.linspace(
        0, len(have) - 1, min(max_panels, len(have))).round().astype(int))]
    grids = [load_field_slice(outdir, n, "fwd") for n in pick]
    # 共享色标：按各面板 99.5% 分位取上界，免得个别热点把其余场压成一片黑
    vmax = max(float(np.percentile(field_magnitude(g), 99.5)) for g in grids)
    ncol = min(3, len(pick))
    nrow = -(-len(pick) // ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.4 * ncol, 3.9 * nrow),
                             squeeze=False)
    im = None
    for ax, n, g in zip(axes.ravel(), pick, grids):
        im = draw_field_map(ax, g, ls=load_ls(outdir, n, cfg), cfg=cfg,
                            vmax=vmax, colorbar=False)
        ax.set_title(f"iteration {n}")
        _design_view(ax, cfg)
    for ax in axes.ravel()[len(pick):]:
        ax.axis("off")
    fig.colorbar(im, ax=axes.ravel().tolist(), fraction=0.02, pad=0.02,
                 label=f"|E| (V/m), shared vmax = {vmax:.3g}")
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path


def adjoint_levelset_state_figure(cfg, phi, x, y, e_x, e_y, e_magnitude, *,
                                  iteration: int, z_used: float, path, dpi: int = 140):
    """Full coupler layout with the updated level set and its forward |E| map."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Polygon as MplPolygon, Rectangle
    from eaopt.solver import cst_model

    phi = np.asarray(phi, dtype=float)
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    e_x, e_y = np.asarray(e_x, dtype=float), np.asarray(e_y, dtype=float)
    e_mag = np.asarray(e_magnitude, dtype=float)
    if e_mag.shape != (e_x.size, e_y.size):
        raise ValueError(
            f"|E| shape {e_mag.shape} does not match field axes "
            f"({e_x.size}, {e_y.size})"
        )
    if phi.shape != (x.size, y.size):
        raise ValueError(f"phi shape {phi.shape} does not match level-set axes ({x.size}, {y.size})")

    layout = cst_model.layout_view()
    substrate = next(s for s in layout["shapes"] if s[0] == "substrate")
    substrate_pts = np.asarray(substrate[1], dtype=float)
    xstep = float(np.median(np.diff(e_x))) if e_x.size > 1 else 0.1
    ystep = float(np.median(np.diff(e_y))) if e_y.size > 1 else 0.1
    extent = (e_x[0] - xstep / 2, e_x[-1] + xstep / 2,
              e_y[0] - ystep / 2, e_y[-1] + ystep / 2)

    finite = e_mag[np.isfinite(e_mag)]
    vmax = float(np.percentile(finite, 99.5)) if finite.size else 1.0
    if vmax <= 0.0:
        vmax = 1.0
    fig, ax = plt.subplots(figsize=(12, 6.5), constrained_layout=True)
    ax.set_facecolor(OUTSIDE_COLOR)
    image = ax.imshow(
        e_mag.T, origin="lower", extent=extent, cmap="inferno",
        vmin=0.0, vmax=vmax, interpolation="nearest", aspect="equal", zorder=1,
    )

    # The fixed feeds are template geometry; the arm inside the design box is
    # drawn from the current level set below.
    for kind, points, label in layout["shapes"]:
        if kind != "metal" or label == "design metal (arm)":
            continue
        ax.add_patch(MplPolygon(
            np.asarray(points, dtype=float), closed=True,
            facecolor=METAL_COLOR, edgecolor="white", linewidth=0.35,
            zorder=4, label="fixed feed metal" if label else None,
        ))

    if np.min(phi) < 0.0:
        ax.contourf(x, y, phi.T,
                    levels=[float(np.min(phi) - 1e-9), 0.0],
                    colors=[METAL_COLOR], zorder=5)
        if np.min(phi) < 0.0 < np.max(phi):
            ax.contour(x, y, phi.T, levels=[0.0], colors=["white"],
                       linewidths=0.65, zorder=6)

    # Mark the substrate edge and the movable design box over the field map.
    sx0, sx1 = float(substrate_pts[:, 0].min()), float(substrate_pts[:, 0].max())
    sy0, sy1 = float(substrate_pts[:, 1].min()), float(substrate_pts[:, 1].max())
    ax.add_patch(Rectangle((sx0, sy0), sx1 - sx0, sy1 - sy0,
                           fill=False, edgecolor="#555555", linewidth=0.7, zorder=3))
    box = cfg.design_region.box
    ax.add_patch(Rectangle((box.x[0], box.y[0]), box.width, box.height,
                           fill=False, edgecolor="#d62728", linestyle="--",
                           linewidth=1.2, zorder=7))

    bounds = layout["bounds"]
    ax.set_xlim(bounds[0], bounds[1])
    ax.set_ylim(bounds[2], bounds[3])
    ax.set_aspect("equal")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(f"Coupler shape and forward electric field, iteration {iteration} (z={z_used:g} mm)")
    fig.colorbar(image, ax=ax, fraction=0.035, pad=0.02, label="|E| (V/m), 99.5th percentile scale")
    handles = [
        Rectangle((0, 0), 1, 1, facecolor=METAL_COLOR, label="metal"),
        Line2D([0], [0], color="#d62728", linestyle="--", label="design region"),
        Line2D([0], [0], color="#555555", linewidth=0.7, label="substrate edge"),
    ]
    ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


def adjoint_levelset_sparams_figure(cfg, history, path, *, dpi: int = 140):
    """Plot forward |S_i,j| in dB for each accepted optimization state."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = list(history)
    if not rows:
        return None
    iterations = [int(row["iteration"]) for row in rows]
    keys = sorted({key for row in rows for key in row.get("s_params_db", {})})
    if not keys:
        return None

    fom_key = f"S{int(cfg.objective.to_port)}{int(cfg.objective.from_port)}"
    fig, ax = plt.subplots(figsize=(7.2, 4.6), constrained_layout=True)
    for key in keys:
        values = [row.get("s_params_db", {}).get(key) for row in rows]
        values = [np.nan if value is None else float(value) for value in values]
        is_fom = key == fom_key
        ax.plot(iterations, values,
                marker="o" if is_fom else "s", markersize=4 if is_fom else 3,
                linewidth=2.2 if is_fom else 1.2,
                label=(f"FoM: |{key}|" if is_fom else f"|{key}|"))
    ax.set_xlabel("Iteration")
    ax.set_ylabel("S parameter (dB)")
    ax.set_title(f"{cfg.name}: S parameters vs iteration")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    if len(iterations) <= 25:
        ax.set_xticks(iterations)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path
