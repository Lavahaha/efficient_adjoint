#!/usr/bin/env python
"""初始化**后向**仿真工程（观测端口激励 = 伴随仿真）并跑第一次仿真。

薄脚本：全部逻辑在 ``eaopt/solver/cst_driver.py``（与 cst_init_fwd.py
共用一份，避免两个脚本各写一套而悄悄漂移）。

    python scripts/cst_init_bwd.py configs/coupler.yaml
    python scripts/cst_init_bwd.py configs/coupler.yaml --attach   # 附接 GUI

激励端口取自 ``objective.to_port``（耦合器 = 端口 3），建工程时写死，
之后 pipeline 永不触碰激励 API——多激励叠加的场会让伴随梯度静默失效，
而 S 参数照样出数，光看 S 参数发现不了。

产物：``<output.dir>/cst/<name>_bwd.cst``
      ``<output.dir>/iter_000/s_params_bwd.json``
读 S 参数时用的是 ``S_{i,to_port}``（如 S1,3），不是 fwd 的 ``S_{i,1}``。
"""

import sys

from eaopt.solver.cst_driver import init_main

if __name__ == "__main__":
    sys.exit(init_main("bwd"))
