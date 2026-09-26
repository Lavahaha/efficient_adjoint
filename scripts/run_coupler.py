"""加载并校验算例配置，打印摘要。

（第 1 步：仅验证配置系统；后续步骤将在此驱动完整优化流程。）
"""

import argparse
from pathlib import Path

from eaopt.config import CaseConfig


def main() -> None:
    ap = argparse.ArgumentParser(description="加载并校验算例配置")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml",
                    help="配置文件路径（默认 configs/coupler.yaml）")
    args = ap.parse_args()

    cfg = CaseConfig.from_yaml(args.config)
    print(cfg.summary())
    print(f"\n配置校验通过: {Path(args.config).resolve()}")


if __name__ == "__main__":
    main()
