"""渲染 CST 侧布局参考图（用来对着论文 Fig.5/Fig.9 核对 CST 里的模板模型）。

画两类东西，叠在一张图上，便于发现配置与模板不一致：
  - 模板侧（CST 宏会建出来的实体）：基板、固定金属、设计区初始金属、
    端口位置——**几何取自算例的模板模块**（``layout_view()``），
    画图脚本不自己算坐标，与 VBA 命令是同一批构造函数
  - 配置侧（算例相关，来自 YAML）：设计区 / 允许区 / 固定区三类框
    （样式与图例文案在 ``eaopt.plotting``，与 iter_NNN.png 共用一套）、
    initial_metal 各多边形（只描绿边，不填充）

**别拿红色画设计区以外的东西**：固定区（直通线/馈线）以前是红实线，用户
对着 iter_NNN.png 里同款红框说"红线框出的应该是设计区域"——设计区是红虚线，
固定区现在是蓝实线。

用法:
    python scripts/plot_layout.py                     # 耦合器
    python scripts/plot_layout.py configs/divider.yaml
    python scripts/plot_layout.py <任意 yaml> <输出路径>   # 覆盖缺省文件名
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
from eaopt.solver.case import load_case                          # noqa: E402

DEFAULT_CONFIG = Path("configs/coupler.yaml")
COLORS = {"substrate": plotting.SUBSTRATE_COLOR, "metal": plotting.METAL_COLOR}


def default_out(cfg: CaseConfig) -> Path:
    """缺省输出文件名**带算例名**：不传第二参数时不许覆盖别的算例的参考图。

    原先缺省是写死的 ``docs/layout_reference.png``：跑一次功分器就把耦合器
    那张覆盖掉，而且没有任何提示（实测踩过）。耦合器沿用历史文件名——README
    与 runbook 都指着它。
    """
    if cfg.name == "coupler":
        return Path("docs/layout_reference.png")
    return Path(f"docs/layout_reference_{cfg.name}.png")


def plot_layout(cfg: CaseConfig, out: Path) -> Path:
    _, model = load_case(cfg)          # 算例的模板模块（几何的单一来源）
    view = model.layout_view()

    fig, ax = plt.subplots(figsize=(9, 7))

    def add(pts, face, label=None, z=2, **kw):
        ax.add_patch(MplPolygon(list(pts), closed=True, facecolor=face,
                                edgecolor=kw.pop("edgecolor", "k"),
                                lw=kw.pop("lw", 0.4), label=label,
                                zorder=z, **kw))

    # ---- 模板侧（CST 宏会建出来的实体）----
    for kind, pts, label in view["shapes"]:
        add(pts, COLORS[kind], label, z=0 if kind == "substrate" else 2)

    # ---- 配置侧（算例 YAML；只描边，与模板边重合说明二者一致）----
    for i, poly in enumerate(cfg.initial_metal):
        ax.add_patch(MplPolygon(poly.vertices, closed=True, fill=False,
                                edgecolor="#2ca02c", lw=1.0, zorder=4,
                                label="initial_metal (YAML)" if i == 0 else None))
    # 设计区（红虚线）/ 允许区（橙点线）/ 固定区（蓝实线）——与 iter_NNN.png 同款
    plotting.draw_regions(ax, cfg)

    # ---- 端口位置（面 + 横向范围中点 + 朝内箭头）----
    for num, x, y, dx, dy in view["ports"]:
        ax.annotate(f"p{num}", (x, y), (x + 1.6 * dx - 0.2, y + 1.6 * dy),
                    color="b", ha="center", va="center",
                    arrowprops=dict(arrowstyle="->", color="b"), zorder=7)

    x0, x1, y0, y1 = view["bounds"]
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower center", fontsize=8, ncol=3)
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    # 标题保持 ASCII：默认字体没有 CJK 字形，中文会画成一串方框
    ax.set_title(f"{cfg.name}: CST template layout, "
                 f"grid {cfg.design_region.grid_step_mm:g} mm, "
                 f"box {cfg.design_region.box.width:g}x{cfg.design_region.box.height:g} mm",
                 fontsize=10)
    fig.tight_layout()

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def main(argv: list[str]) -> int:
    cfg_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CONFIG
    cfg = CaseConfig.from_yaml(cfg_path)
    out = Path(argv[2]) if len(argv) > 2 else default_out(cfg)
    print(f"已写出布局参考图: {plot_layout(cfg, out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
