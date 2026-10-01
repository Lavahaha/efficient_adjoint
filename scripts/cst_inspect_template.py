"""直接检查模板 .cst 里到底有什么（**不需要 CST**，纯 Python）。

用法（服务器/本机都能跑）:
    python scripts/cst_inspect_template.py                  # 读配置里的两个模板
    python scripts/cst_inspect_template.py cst/coupler_fwd.cst

回答的问题是："打开的工程里没有模型/端口/结果"到底是
  (a) 模板文件本身就是空工程（宏保存出来的东西不完整），还是
  (b) 模板是好的、复制/打开姿势不对（少了同名文件夹）。
判断依据：**两处都查**——.cst 当作 zip 容器列出成员（读不动就按本地文件
头恢复、再退回搜原始字节），同名文件夹递归列出并搜对象名
（substrate / design_region / thru_line / Port 1 ...）。服务器实测：
几何确实可能只在同名文件夹里，只看 .cst 会得出"空工程"的错误结论。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eaopt.solver import cst_project  # noqa: E402


def inspect(path: Path) -> None:
    print("=" * 60)
    for line in cst_project.describe(path):
        print(line)
    parent = path.parent
    if parent.is_dir():
        sibs = sorted(parent.iterdir())
        print(f"  所在目录 {parent} 的内容：")
        for s in sibs[:20]:
            kind = "目录" if s.is_dir() else "文件"
            size = "-" if s.is_dir() else f"{s.stat().st_size / 1e3:.1f} KB"
            print(f"    [{kind}] {s.name}  {size}")
    print("=" * 60)


def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(description="检查 CST 模板文件内容")
    ap.add_argument("paths", nargs="*", help="模板 .cst 路径（默认取配置里的两个）")
    ap.add_argument("--config", default="configs/coupler.yaml")
    args = ap.parse_args()

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        from eaopt.config import CaseConfig

        cfg = CaseConfig.from_yaml(args.config)
        targets = [Path(cfg.solver.template_fwd), Path(cfg.solver.template_bwd)]

    print("模板内容检查（.cst 与同名文件夹两处都查：模型可能在任何一处）")
    for t in targets:
        inspect(t)


if __name__ == "__main__":
    main()
