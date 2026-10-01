"""生成 CST 模板宏——每个模板在 CST GUI 中执行一次。

**建模用结构宏 `.mcs`（动作进 History List），另存用控制宏 `.mcr`。**
服务器实测：拿 `.mcr` 建模，几何/端口/监视器在会话里都正常，但
History List 是空的 ⇒ 存盘重开是空工程。CST 的模型靠历史表重放，
历史为空模型就存不下来。详见 eaopt/solver/template_builder.py 头部。

用法（本地，无需 CST）:
    python scripts/build_cst_template.py [输出目录]

产物: <输出目录>/build_coupler_fwd.mcs（结构宏：端口 1 激励的模型）
      <输出目录>/save_coupler_fwd.mcr（控制宏：另存为 coupler_fwd.cst）
      build_coupler_bwd.mcs / save_coupler_bwd.mcr（端口 3 激励）
      polygon_test.mcs（诊断用，可选：验证 Extrude 直边）

服务器步骤:
    1. CST Studio 2024 → File → New（模板选 <None>）新建空工程；
    2. 从**主界面的 Macros 下拉菜单**运行 build_coupler_fwd.mcs
       （不是 VBA 编辑器里的运行图标——那样即使是结构宏也不进历史表）；
    3. **立刻检查 History List 不为空**（应能看到 Brick/Extrude/Port 各条），
       并确认 Components 里的实体、Ports 里的 4 个端口、Field Monitors
       的 E/H 监视器都在；
    4. 运行 save_coupler_fwd.mcr 另存（或 File → Save As 手工存）
       → <输出目录>/coupler_fwd.cst；
    5. 再 File → New，重复 2-4 用 bwd 那对宏 → coupler_bwd.cst；
    6. 把两个 .cst 路径填进 configs/coupler.yaml 的 solver 段
       （template_fwd / template_bwd），solver.type 改为 cst；
    7. 运行 scripts/cst_smoke.py 验证求解与场导出，把输出贴回开发者。
"""

import argparse
from pathlib import Path

from eaopt.solver.template_builder import (build_all_templates,
                                           build_polygon_test_macro)


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 CST 模板宏（.mcs 建模 + .mcr 另存）")
    ap.add_argument("outdir", nargs="?", default="cst",
                    help="输出目录（默认 cst/）")
    args = ap.parse_args()
    outdir = Path(args.outdir)
    for path in build_all_templates(outdir):
        print(f"已生成: {path}")
    print(f"已生成诊断宏（可选）: {build_polygon_test_macro(outdir)}")
    print("服务器步骤见脚本头部注释（File → New → Macros 菜单跑 .mcs → "
          "查 History List → 跑 .mcr 另存）")


if __name__ == "__main__":
    main()
