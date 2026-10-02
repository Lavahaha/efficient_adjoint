#!/usr/bin/env python
"""形状更新：改两个 CST 工程的优化区 → 两个都求解 → 提取 S 参数与场。

薄脚本：全部逻辑在 ``eaopt/solver/cst_driver.py``。

    # 用一个手写的形状文件（iter_NNN/shape.json 或 [[x,y],...] / [[[x,y],...],...]）
    python scripts/cst_update.py configs/coupler.yaml --shape my_shape.json

    # 用最近一轮 pipeline 留下的 φ 快照（iter_NNN/ls_phi.npz）重算轮廓
    python scripts/cst_update.py configs/coupler.yaml --from-ls

    # 指定写到哪一轮（缺省 = 已有最新轮次 + 1）；附接 GUI 看模型变化
    python scripts/cst_update.py configs/coupler.yaml --shape s.json --iteration 7 --attach

一次调用做的事：
  1. 打开（或复用）两个工程；
  2. 每个工程写**一条**历史记录：删掉整个 design_region 组件 + 按新轮廓重建
     （走 add_to_history，所以工程重放历史得到的就是当前形状）；
  3. 两个工程各求解一次，存盘 → 读 S 参数（cst.results）→ 导 E/H 场并裁到
     设计区 ± field_margin_mm；
  4. 结果写到 ``<output.dir>/iter_NNN/``（shape/s_params/meta，可选场）。
"""

import sys

from eaopt.solver.cst_driver import update_main

if __name__ == "__main__":
    sys.exit(update_main())
