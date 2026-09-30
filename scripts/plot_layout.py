"""渲染 CST 侧布局参考图（用来对着论文 Fig.5 核对 CST 里的模板模型）。

画两类东西，叠在一张图上，便于发现配置与模板不一致：
  - 配置侧（算例相关，来自 YAML）：设计区红框、allowed_region、
    initial_metal 各多边形（只描红边，不填充）
  - 模板侧（CST 宏会建出来的实体）：基板、直通线、两条腿、4 个端口

模板侧的布局常量目前在 eaopt/solver/template_builder.py（仍与耦合器
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

from eaopt.config import CaseConfig                              # noqa: E402
from eaopt.solver import template_builder as T                   # noqa: E402

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
        "#ececec", "substrate", z=0)
    add([(T.THRU_X0, T.THRU_LO), (T.THRU_X1, T.THRU_LO),
         (T.THRU_X1, T.THRU_HI), (T.THRU_X0, T.THRU_HI)],
        "#333333", "through line (p1-p2)")
    add(T._left_leg_polygon(), "#333333", "legs (p3-p4)")
    add(T._right_leg_polygon(), "#333333")
    # 设计区金属（= 耦合臂横段 + 两端内侧圆角；pipeline 每轮重建）
    add(T._arm_design_polygon(), "#333333", "design metal (arm)")

    # ---- 配置侧（算例 YAML；只描边，与模板边重合说明二者一致）----
    for i, poly in enumerate(cfg.initial_metal):
        ax.add_patch(MplPolygon(poly.vertices, closed=True, fill=False,
                                edgecolor="#d02020", lw=1.0, zorder=4,
                                label="initial_metal (YAML)" if i == 0 else None))
    box = cfg.design_region.box
    ax.add_patch(MplPolygon([(box.x[0], box.y[0]), (box.x[1], box.y[0]),
                             (box.x[1], box.y[1]), (box.x[0], box.y[1])],
                            closed=True, fill=False, edgecolor="red",
                            ls=(0, (6, 4)), lw=1.1, zorder=5,
                            label="design box"))
    area = cfg.constraints.allowed_region
    ax.add_patch(MplPolygon([(area.x[0], area.y[0]), (area.x[1], area.y[0]),
                             (area.x[1], area.y[1]), (area.x[0], area.y[1])],
                            closed=True, fill=False, edgecolor="#e08000",
                            ls=(0, (2, 3)), lw=1.0, zorder=5,
                            label="allowed_region"))

    # ---- 端口位置（面 + 横向范围中点 + 朝内箭头）----
    for num, (x, y, dx, dy) in {
        1: (T.THRU_X0, (T.THRU_LO + T.THRU_HI) / 2, 1, 0),
        2: (T.THRU_X1, (T.THRU_LO + T.THRU_HI) / 2, -1, 0),
        3: ((T.LEG_L_OUT + T.LEG_L_IN) / 2, T.LEG_BOT, 0, 1),
        4: ((T.LEG_R_OUT + T.LEG_R_IN) / 2, T.LEG_BOT, 0, 1),
    }.items():
        ax.annotate(f"p{num}", (x, y), (x + 1.6 * dx - 0.2, y + 1.6 * dy),
                    color="b", ha="center", va="center",
                    arrowprops=dict(arrowstyle="->", color="b"), zorder=6)

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
