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
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "METAL_COLOR", "AIR_COLOR", "SUBSTRATE_COLOR", "OUTSIDE_COLOR",
    "REGIONS", "region_outlines", "draw_regions",
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
    "fixed": dict(label="fixed region (through line)",
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
