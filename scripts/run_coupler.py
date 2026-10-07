"""运行优化（论文 Fig. 4 流程）——一条命令跑完初始化 + 迭代循环。

**与算例无关**：跑哪一个由配置里的 ``name`` 决定（见 ``solver/case.py``）。
文件名里带 coupler 是历史包袱（当时只有耦合器一个算例），改名为
``run_optimize.py`` 留作后续。

用法:
    python scripts/run_coupler.py [config.yaml] [--attach] [--save-fields]
    python scripts/run_coupler.py configs/divider.yaml        # 功分器

流程：CST 工程不存在时自动初始化（等价于先跑两个 cst_init_*.py）→ 每轮
「更新设计区形状 → 正/反向仿真 → 形状导数 → 水准集更新」→ 直到
max_iterations 或收敛。产物落在 cfg.output.dir：iter_NNN/（每轮形状、
S 参数、可选 E/H 场、φ 快照）、history.jsonl、fom.png。

服务器上默认起无界面（headless）实例；有 GUI 许可想看界面时用 --attach
（附接已打开的 CST 实例）。
"""

from eaopt.cli import base_parser, banner, make_log, safe_console
from eaopt.config import CaseConfig
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver.case import load_case
from eaopt.solver.cst import CstSolver


def main() -> None:
    safe_console()
    p = base_parser("运行伴随法形状优化（CST）",
                    config_default="configs/coupler.yaml")
    args = p.parse_args()
    log = make_log(args.log)

    cfg = CaseConfig.from_yaml(args.config)
    setup, _ = load_case(cfg)          # 算例（CstSetup + 模板模块）由 cfg.name 选
    log(cfg.summary())
    banner(log, "运行优化", cfg, setup)

    ls = make_level_set(cfg)
    solver = CstSolver(cfg, setup=setup, auto_init=True,
                       save_fields=args.save_fields,
                       attach=args.attach or None,
                       log=log)
    pipeline = OptimizerPipeline(cfg, solver, ls, setup=setup)
    pipeline.run()


if __name__ == "__main__":
    main()
