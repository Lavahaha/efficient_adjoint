"""运行优化（论文 Fig. 4 流程）。

用法:
    python scripts/run_coupler.py [config.yaml]

solver.type=mock 时本地运行（默认）；solver.type=cst 时在服务器运行。
"""

import argparse

from eaopt.config import CaseConfig
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver.base import make_solver


def main() -> None:
    ap = argparse.ArgumentParser(description="运行伴随法形状优化")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml",
                    help="配置文件路径（默认 configs/coupler.yaml）")
    args = ap.parse_args()

    cfg = CaseConfig.from_yaml(args.config)
    print(cfg.summary())
    print("=" * 60)

    ls = make_level_set(cfg)
    solver = make_solver(cfg, ls)
    pipeline = OptimizerPipeline(cfg, solver, ls)
    pipeline.run()


if __name__ == "__main__":
    main()
