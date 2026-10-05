"""渲染 CST 侧布局参考图（用来对着论文 Fig.5 核对 CST 里的模板模型）。

画两类东西，叠在一张图上，便于发现配置与模板不一致：
  - 配置侧（算例相关，来自 YAML）：设计区 / 允许区 / 固定区三类框
    （样式与图例文案在 ``eaopt.plotting``，与 iter_NNN.png 共用一套）、
    initial_metal 各多边形（只描绿边，不填充）
  - 模板侧（CST 宏会建出来的实体）：基板、直通线、两条腿、4 个端口

**别拿红色画设计区以外的东西**：固定区（直通线）以前是红实线，用户对着
iter_NNN.png 里同款红框说"红线框出的应该是设计区域"——设计区是红虚线，
固定区现在是蓝实线。

模板侧的布局常量目前在 eaopt/solver/cst_model.py（仍与耦合器
算例耦合，见该模块头部 TODO）。

用法:
    python scripts/plot_layout.py                       # 默认算例 + 默认输出
    python scripts/plot_layout.py configs/coupler.yaml docs/layout_reference.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt                                  # noqa: E402
from matplotlib.patches import Polygon as MplPolygon             # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eaopt import plotting                                       # noqa: E402
from eaopt.config import CaseConfig                              # noqa: E402
from eaopt.solver import cst_model as T                   # noqa: E402

DEFAULT_CONFIG = Path("configs/coupler.yaml")
DEFAULT_OUT = Path("docs/layout_reference.png")


def plot_layout(cfg: CaseConfig, out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(9, 7))

    def add(pts, face, label=None, z=2, **kw):
        ax.add_patch(MplPolygon(list(pts), closed=True, facecolor=face,
                                edgecolor=kw.pop("edgecolor", "k"),
                                lw=kw.pop("lw", 0.4), label=label,
                                zorder=z, **kw))

    # ---- 模板侧（CST 宏会建出来的实体）----
    add([(T.THRU_X0, T.LEG_BOT), (T.THRU_X1, T.LEG_BOT),
         (T.THRU_X1, T.SUB_TOP), (T.THRU_X0, T.SUB_TOP)],
        plotting.SUBSTRATE_COLOR, "substrate", z=0)
    add([(T.THRU_X0, T.THRU_LO), (T.THRU_X1, T.THRU_LO),
         (T.THRU_X1, T.THRU_HI), (T.THRU_X0, T.THRU_HI)],
        plotting.METAL_COLOR, "through line (p1-p2)")
    add(T._left_leg_polygon(), plotting.METAL_COLOR, "legs (p3-p4)")
    add(T._right_leg_polygon(), plotting.METAL_COLOR)
    # 设计区金属（= 耦合臂横段 + 两端内侧圆角；pipeline 每轮重建）
    add(T._arm_design_polygon(), plotting.METAL_COLOR, "design metal (arm)")

    # ---- 配置侧（算例 YAML；只描边，与模板边重合说明二者一致）----
    for i, poly in enumerate(cfg.initial_metal):
        ax.add_patch(MplPolygon(poly.vertices, closed=True, fill=False,
                                edgecolor="#2ca02c", lw=1.0, zorder=4,
                                label="initial_metal (YAML)" if i == 0 else None))
    # 设计区（红虚线）/ 允许区（橙点线）/ 固定区（蓝实线）——与 iter_NNN.png 同款
    plotting.draw_regions(ax, cfg)

    # ---- 端口位置（面 + 横向范围中点 + 朝内箭头）----
    for num, (x, y, dx, dy) in {
        1: (T.THRU_X0, (T.THRU_LO + T.THRU_HI) / 2, 1, 0),
        2: (T.THRU_X1, (T.THRU_LO + T.THRU_HI) / 2, -1, 0),
        3: ((T.LEG_L_OUT + T.LEG_L_IN) / 2, T.LEG_BOT, 0, 1),
        4: ((T.LEG_R_OUT + T.LEG_R_IN) / 2, T.LEG_BOT, 0, 1),
    }.items():
        ax.annotate(f"p{num}", (x, y), (x + 1.6 * dx - 0.2, y + 1.6 * dy),
                    color="b", ha="center", va="center",
                    arrowprops=dict(arrowstyle="->", color="b"), zorder=7)

    ax.set_xlim(T.THRU_X0 - 1.5, T.THRU_X1 + 1.5)
    ax.set_ylim(T.LEG_BOT - 1.0, T.SUB_TOP + 1.0)
    ax.set_aspect("equal")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower center", fontsize=8, ncol=3)
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(f"{cfg.name}: CST template layout vs {DEFAULT_CONFIG.name}\n"
                 f"w={T.W}  g={T.G}  d={T.D}  bend R_center={T.R_BEND}  "
                 f"(paper Fig.5)", fontsize=10)
    fig.tight_layout()

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def main(argv: list[str]) -> int:
    cfg_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CONFIG
    out = Path(argv[2]) if len(argv) > 2 else DEFAULT_OUT
    cfg = CaseConfig.from_yaml(cfg_path)
    print(f"已写出布局参考图: {plot_layout(cfg, out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
