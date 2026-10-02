#!/usr/bin/env python
"""初始化**前向**仿真工程（输入端口激励）并跑第一次仿真。

薄脚本：全部逻辑在 ``eaopt/solver/cst_driver.py``（与 cst_init_bwd.py
共用一份，避免两个脚本各写一套而悄悄漂移）。

    python scripts/cst_init_fwd.py configs/coupler.yaml
    python scripts/cst_init_fwd.py configs/coupler.yaml --attach   # 附接 GUI

产物：``<output.dir>/cst/<name>_fwd.cst``（工程，含模型历史）
      ``<output.dir>/iter_000/s_params_fwd.json``（S 参数；--save-fields 时另有场）
之后跑 cst_init_bwd.py 建反向工程，再用 cst_update.py 做形状迭代。
"""

import sys

from eaopt.solver.cst_driver import init_main

if __name__ == "__main__":
    sys.exit(init_main("fwd"))
