"""运行优化（论文 Fig. 4 流程）——一条命令跑完初始化 + 迭代循环。

用法:
    python scripts/run_coupler.py [config.yaml] [--attach] [--save-fields]

流程：CST 工程不存在时自动初始化（等价于先跑两个 cst_init_*.py）→ 每轮
「更新设计区形状 → 正/反向仿真 → 形状导数 → 水准集更新」→ 直到
max_iterations 或收敛。产物落在 cfg.output.dir：iter_NNN/（每轮形状、
S 参数、可选 E/H 场、φ 快照）、history.jsonl、fom.png。

服务器上默认起无界面（headless）实例；有 GUI 许可想看界面时用 --attach
（附接已打开的 CST 实例）。
"""

from eaopt.cli import base_parser, make_log, safe_console
from eaopt.config import CaseConfig
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver.cst import CstSolver
from eaopt.solver.cst_setup import COUPLER


def main() -> None:
    safe_console()
    p = base_parser("运行伴随法形状优化（CST）",
                    config_default="configs/coupler.yaml")
    args = p.parse_args()
    log = make_log(args.log)

    cfg = CaseConfig.from_yaml(args.config)
    log(cfg.summary())
    log("-" * 60)
    log(f"CST         : {COUPLER.frequency_ghz} GHz, eps_r={COUPLER.eps_r}, "
        f"金属 {COUPLER.metal_material} {COUPLER.metal_thickness_mm} mm, "
        f"端口 {COUPLER.ports}")
    log("=" * 60)

    ls = make_level_set(cfg)
    solver = CstSolver(cfg, setup=COUPLER, auto_init=True,
                       save_fields=args.save_fields,
                       attach=args.attach or None,
                       log=log)
    pipeline = OptimizerPipeline(cfg, solver, ls, setup=COUPLER)
    pipeline.run()


if __name__ == "__main__":
    main()
