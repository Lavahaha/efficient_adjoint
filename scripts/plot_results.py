"""事后重画结果目录里的过程图（**不用重跑仿真**，服务器上直接跑）。

画三样，与 pipeline 跑完时自动写的那套是同一份代码（``eaopt.plotting``）：

    iter_NNN.png            左 = φ 快照（形状），右 = 采样面 |E|
                            （旧轮次没有 efield_fwd.npz 时只画左栏）
    sparams_vs_iter.png     各响应端口 |S_i1| 随迭代（论文 Fig.11）
    efield_evolution.png    等间隔若干轮的 |E| 演化（论文 Fig.10）

为什么要有这个脚本：图上的约定（金属按 φ=0 等值线、视野夹在设计区、
电场画在**采样面**而不是金属顶面）改一次就得把已有结果全部重画一遍；
重画只读 ``iter_NNN/`` 的产物（ls_phi.npz / s_params_fwd.json /
efield_fwd.npz），几十轮也只花几秒。

用法::

    python scripts/plot_results.py                          # 耦合器 + 配置里的 output.dir
    python scripts/plot_results.py results/divider configs/divider.yaml
    python scripts/plot_results.py results/divider configs/divider.yaml --only sparams
    python scripts/plot_results.py results/divider configs/divider.yaml --iters 0,10,49
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")                       # 服务器无显示：写文件即可

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eaopt import artifacts, plotting                            # noqa: E402
from eaopt.config import CaseConfig                              # noqa: E402

DEFAULT_CONFIG = Path("configs/coupler.yaml")


def _iterations(outdir: Path) -> list[int]:
    """有 φ 快照的轮次（iter_NNN.png 能重画的最低要求）。"""
    return [n for n in artifacts.list_iterations(outdir)
            if (artifacts.iteration_dir(outdir, n) / "ls_phi.npz").is_file()]


def parse_iters(spec: str | None, outdir: Path) -> list[int]:
    if not spec:
        return _iterations(outdir)
    return [int(t) for t in spec.replace(" ", "").split(",") if t != ""]


def redraw_snapshots(cfg: CaseConfig, outdir: Path, iters: list[int]) -> int:
    n_ok = 0
    for n in iters:
        try:
            e_slice = artifacts.load_field_slice(outdir, n, "fwd")
            z_used = float(e_slice.axes()[2][0])
        except FileNotFoundError:
            e_slice, z_used = None, None      # 旧轮次：只画左栏
        plotting.snapshot_figure(
            cfg, plotting.load_ls(outdir, n, cfg), it=n, e_slice=e_slice,
            z_used=z_used, path=outdir / f"iter_{n:03d}.png")
        n_ok += 1
    return n_ok


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="重画结果目录的过程图（只读 iter_NNN/，不跑仿真）")
    ap.add_argument("outdir", nargs="?", default=None,
                    help="结果目录（缺省 = 配置里的 output.dir）")
    ap.add_argument("config", nargs="?", default=None,
                    help=f"算例 YAML（缺省 {DEFAULT_CONFIG}）")
    ap.add_argument("--only", choices=("all", "snapshot", "sparams", "efield"),
                    default="all", help="只画其中一种（缺省全画）")
    ap.add_argument("--iters", default=None,
                    help="只重画面板/快照的轮次，逗号分隔（缺省=全部有 φ 快照的轮次）")
    args = ap.parse_args(argv[1:])

    cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG
    cfg = CaseConfig.from_yaml(cfg_path)
    outdir = Path(args.outdir) if args.outdir else Path(cfg.output.dir)
    if not outdir.is_dir():
        print(f"结果目录不存在: {outdir}", file=sys.stderr)
        return 2
    print(f"配置 {cfg_path}（算例 {cfg.name}） → 结果目录 {outdir}")

    did = []
    if args.only in ("all", "snapshot"):
        iters = parse_iters(args.iters, outdir)
        if iters:
            did.append(f"{redraw_snapshots(cfg, outdir, iters)} 张 iter_NNN.png")
        else:
            print("[图] 没有可重画的轮次（缺 ls_phi.npz）")
    if args.only in ("all", "sparams"):
        if plotting.sparams_figure(cfg, outdir, outdir / "sparams_vs_iter.png"):
            did.append("sparams_vs_iter.png")
    if args.only in ("all", "efield"):
        if plotting.efield_evolution_figure(cfg, outdir,
                                            outdir / "efield_evolution.png"):
            did.append("efield_evolution.png")
    print("已写出: " + ("、".join(did) if did else "（无）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
