"""测试共用件：假 CST 官方库的注入 + 小型耦合器配置工厂。

## 假 CST 库（必须在任何 eaopt 导入之前注入）

CST 侧模块（``eaopt/solver/cst.py``、``eaopt/solver/cst_results.py``、三个
脚本）在**顶层**直接 ``import cst.interface`` / ``import cst.results``——这是
"不封装官方库"的代价与收益。本机没有 CST（官方库只支持 3.6–3.11），所以
这里把 ``tests/fake_cst/`` 插到 ``sys.path[0]`` 并先导入一次：真库/仓库根的
``cst/`` 目录都抢不过它，且 pytest 收集测试模块时（早于任何 fixture）生产
代码拿到的就已经是假库。

下面那条 assert 是安全绳：**服务器上真装了 CST 也绝不能走真库**——测试会真
的起 CST 实例、动真工程。断言让"假库没生效"当场失败，而不是悄悄连真机。
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

_TESTS = Path(__file__).resolve().parent

_FAKE_LIB = _TESTS / "fake_cst"
_fake = str(_FAKE_LIB)
if _fake in sys.path:
    sys.path.remove(_fake)
sys.path.insert(0, _fake)

import cst                    # noqa: E402
import cst.interface          # noqa: E402
import cst.results            # noqa: E402

assert _FAKE_LIB in Path(cst.__file__).resolve().parents, (
    f"测试进程导入的 cst 不是假库：{cst.__file__}\n"
    "  检查 tests/fake_cst/cst/ 是否完整；若是真库，测试会去动真的 CST 工程。"
)

import pytest                 # noqa: E402

from eaopt.config import (    # noqa: E402
    BoxSpec,
    CaseConfig,
    ConstraintSpec,
    DesignRegionSpec,
    LevelSetSpec,
    OptimizerSpec,
    OutputSpec,
    PolygonSpec,
    SamplingSpec,
)

REPO_ROOT = _TESTS.parent

ARM_TOP = 0.0
LINE_BOTTOM = 0.4


@pytest.fixture(autouse=True)
def _reset_fake_cst():
    """每个测试从干净的假 CST 会话开始（状态逐个测试复位）。"""
    cst.interface.reset()
    cst.results.reset()
    yield


_SCRIPTS: dict = {}


def load_script(name: str):
    """按路径加载 ``scripts/<name>.py`` 并缓存。

    ``scripts/`` 不是包（也不该是：它们是可直接执行的程序），所以用
    importlib 按文件路径加载。脚本必须"import 时零副作用"，逻辑在
    ``main(argv)`` 里——否则这里一加载就会去连 CST。
    """
    if name not in _SCRIPTS:
        path = REPO_ROOT / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_eaopt_script_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _SCRIPTS[name] = mod
    return _SCRIPTS[name]


def make_compact_cfg(tmp_path, min_gap: float = 0.1, velocity_sign: float = 1.0,
                     arm_top: float = ARM_TOP, max_iterations: int = 8) -> CaseConfig:
    return CaseConfig(
        name="compact-test",
        design_region=DesignRegionSpec(
            box=BoxSpec(x=(0.0, 4.0), y=(-1.0, 1.0)),
            grid_step_mm=0.1,
            field_margin_mm=0.5,
        ),
        initial_metal=[
            PolygonSpec(vertices=[[0.0, -0.6], [4.0, -0.6], [4.0, arm_top], [0.0, arm_top]]),
            PolygonSpec(vertices=[[0.0, LINE_BOTTOM], [4.0, LINE_BOTTOM],
                                  [4.0, 1.0], [0.0, 1.0]]),
        ],
        fixed_region=[
            PolygonSpec(vertices=[[0.0, LINE_BOTTOM], [4.0, LINE_BOTTOM],
                                  [4.0, 1.0], [0.0, 1.0]]),
        ],
        constraints=ConstraintSpec(min_gap_mm=min_gap),
        # 偏移 0.06 > 半网格（dx=0.1/2）：插值窗口不得跨过金属边界，
        # 否则金属侧 E=0 会把采样场压掉一半
        sampling=SamplingSpec(point_spacing_mm=0.2, sample_offset_mm=0.06,
                              sample_side="outside"),
        # convergence_window > max_iterations：测试里常要跑满步数观察完整
        # 轨迹，不想被收敛窗口提前截断（收敛逻辑本身不变）
        optimizer=OptimizerSpec(step_cells=1.0, cfl=0.5, velocity_sign=velocity_sign,
                                max_iterations=max_iterations,
                                fom_tolerance=1e-4,
                                convergence_window=max_iterations + 10),
        level_set=LevelSetSpec(reinit_every=5, extension_band_mm=0.3),
        output=OutputSpec(dir=str(tmp_path / "results")),
    )


def write_case_yaml(tmp_path, cfg: CaseConfig | None = None, **kw) -> Path:
    """把配置写成 YAML 文件（脚本按 YAML 驱动，测试就走真正的 YAML 入口）。

    ``CaseConfig`` 里全是 tuple（``box.x`` 等），PyYAML 的 safe_dump 不认，
    先转成 list——这与 YAML 里的写法一致，读回来由 ``_build`` 再转回 tuple。
    """
    import yaml

    cfg = make_compact_cfg(tmp_path, **kw) if cfg is None else cfg
    path = Path(tmp_path) / "case.yaml"
    path.write_text(yaml.safe_dump(_plain(dataclasses.asdict(cfg)),
                                   allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def _plain(obj):
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def with_arm_top(cfg: CaseConfig, arm_top: float) -> CaseConfig:
    """返回臂上边缘抬到 arm_top 的配置副本。"""
    cfg2 = deepcopy(cfg)
    cfg2.initial_metal[0].vertices[2][1] = arm_top
    cfg2.initial_metal[0].vertices[3][1] = arm_top
    return cfg2
