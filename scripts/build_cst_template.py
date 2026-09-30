"""生成 CST 模板宏（.mcr 命令宏）——每个模板在 CST GUI 中执行一次。

用法（本地，无需 CST）:
    python scripts/build_cst_template.py [输出目录]

产物: <输出目录>/build_coupler_fwd.mcr（端口 1 激励）
      <输出目录>/build_coupler_bwd.mcr（端口 3 激励）

为什么是 .mcr：Import Macro 对话框只列 "CST Macro Files (*.mcs; *.mcr)"，
.bas 不可见；且工程级指令（新建/另存工程）只在**命令宏**（.mcr）上下文
合法——在 .mcs（结构宏）中执行 NewProject 实测报 "Invalid instruction"。
故宏内不含 NewProject，新建工程由用户在 GUI 中完成。

服务器步骤:
    1. CST Studio 2024 → File → New（模板选 <None>）新建空工程；
    2. Home → Macros → Import Macro...，文件类型切到
       "CST Macro Files (*.mcs; *.mcr)"，选 build_coupler_fwd.mcr，
       运行 Main → 自动另存为 <输出目录>/coupler_fwd.cst；
    3. 再 File → New，导入并运行 build_coupler_bwd.mcr →
       coupler_bwd.cst；
    4. 打开两个工程检查：4 个波导端口、5 GHz E/H 场监视器、边界
       （x/y/zmin 磁、zmax 电）。若激励未生效（Excitation 命令被拒），
       在端口对话框中手工勾选：fwd 勾端口 1，bwd 勾端口 3；
    5. 把两个 .cst 路径填进 configs/coupler.yaml 的 solver 段
       （template_fwd / template_bwd），solver.type 改为 cst；
    6. 运行 scripts/cst_smoke.py 验证求解与场导出，把输出贴回开发者。
"""

import argparse
from pathlib import Path

from eaopt.solver.template_builder import build_all_templates


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 CST 模板命令宏 .mcr")
    ap.add_argument("outdir", nargs="?", default="cst",
                    help="输出目录（默认 cst/）")
    args = ap.parse_args()
    for path in build_all_templates(Path(args.outdir)):
        print(f"已生成: {path}")
    print("服务器步骤见脚本头部注释（File → New → 导入宏 → 运行 Main → 检查端口/激励）")


if __name__ == "__main__":
    main()
