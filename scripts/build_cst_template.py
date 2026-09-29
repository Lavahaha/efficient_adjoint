"""生成 CST 模板宏（.bas）——在 CST GUI 中执行一次，创建双模板工程。

用法（本地，无需 CST）:
    python scripts/build_cst_template.py [输出目录]

产物: <输出目录>/build_templates.bas

服务器步骤:
    1. 把 build_templates.bas 拷到服务器，打开 CST Studio 2024；
    2. 打开任意（新）工程 → 宏面板（Macros）→ Import Macro... →
       选择 build_templates.bas；
    3. 运行宏 Main：生成 <输出目录>/coupler_fwd.cst（端口1激励）与
       coupler_bwd.cst（端口3激励）；
    4. 打开两个工程检查：4 个波导端口、5 GHz E/H 场监视器、边界
       （x/y/zmin 磁、zmax 电）。若激励未生效（宏的 Excitation 命令
       被拒绝），在端口对话框中手工勾选：fwd 勾端口 1，bwd 勾端口 3；
    5. 把两个 .cst 路径填进 configs/coupler.yaml 的 solver 段
       （template_fwd / template_bwd），solver.type 改为 cst；
    6. 运行 scripts/cst_smoke.py 验证求解与场导出，把输出贴回开发者。
"""

import argparse
from pathlib import Path

from eaopt.solver.template_builder import build_macro


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 CST 双模板宏 .bas")
    ap.add_argument("outdir", nargs="?", default="cst",
                    help="输出目录（默认 cst/）")
    args = ap.parse_args()
    path = build_macro(Path(args.outdir))
    print(f"已生成: {path}")
    print("服务器步骤见脚本头部注释（CST GUI 导入宏 → 运行 Main → 检查端口/激励）")


if __name__ == "__main__":
    main()
