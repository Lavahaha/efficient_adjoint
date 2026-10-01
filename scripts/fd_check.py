"""有限差分验证命令行工具。

用法:
    python scripts/fd_check.py [config.yaml]

输出：预测/实测 ΔFoM 比值、mock 理论比值与偏差、velocity_sign 符号裁决。
（CST 端第 7 步复用同一流程，仅扰动实现不同。）
"""

import argparse

from eaopt.adjoint.fd_check import fd_check
from eaopt.config import CaseConfig
from eaopt.pipeline import make_level_set
from eaopt.solver.base import make_solver


def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(description="形状导数有限差分验证")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml",
                    help="配置文件路径（默认 configs/coupler.yaml）")
    ap.add_argument("--h", type=float, default=0.08,
                    help="扰动幅度（mm，默认 0.08）")
    args = ap.parse_args()

    cfg = CaseConfig.from_yaml(args.config)
    ls = make_level_set(cfg)
    solver = make_solver(cfg, ls)
    r = fd_check(cfg, ls, solver, h=args.h)

    print("=" * 60)
    print("有限差分验证报告")
    print("=" * 60)
    print(f"参与预测的采样点数 : {r['n_samples']}（全可动边界口径）")
    print(f"有效位移 h_eff      : {r['h_eff']:.3f} mm（栅格化量化校准）")
    print(f"预测 ΔFoM           : {r['pred']:+.6e}")
    print(f"实测 ΔFoM           : {r['actual']:+.6e}")
    print(f"比值 实测/预测       : {r['ratio']:+.4f}")
    print(f"理论比值 (mock)      : {r['expected']:.4e}")
    print(f"偏差                : {r['deviation']*100:.1f}%")
    print("-" * 60)
    if r["sign_ok"]:
        print("符号裁决: ratio > 0 => velocity_sign = +1（V>0 金属扩张）正确")
    else:
        print("符号裁决: ratio < 0 => velocity_sign = -1（请翻转配置中的符号）")
    print("=" * 60)


if __name__ == "__main__":
    main()
