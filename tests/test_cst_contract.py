"""仓库级合同：改架构时最容易悄悄破掉、破了又不报错的三件事。

1. **仓库根不能有 ``cst/``**——它会被当成命名空间包抢在官方库前面（PEP 420），
   报错信息完全不指向真因（"没有 add_to_history"之类）；
2. **"纯规则"模块不许 import cst**——``cst_setup``/``cst_model``/``pipeline``
   这些在没装 CST 的机器上也要能导入（画图、算例校验、CI 都靠它）；
3. **两个 init 脚本只许在 TAG 那一块不同**——~245 行逐字重复是"不封装官方库"
   的代价，漂移一次就会出现"fwd 修了、bwd 没修"的对称 bug。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

#: 不许依赖 CST 的模块（导入这些之后 ``sys.modules`` 里不该有 ``cst``）
CST_FREE = [
    "eaopt.config", "eaopt.artifacts", "eaopt.cli", "eaopt.pipeline",
    "eaopt.solver", "eaopt.solver.base", "eaopt.solver.cst_model",
    "eaopt.solver.cst_setup",
]

#: TAG 常量区的起止标记（两个 init 脚本里各一份，逐字相同）
TAG_MARK = "# ===================== 两个 init 脚本唯一的差异 =====================\n"
TAG_END = "# ===================================================================\n"


def _run_without_fake_cst(code: str) -> subprocess.CompletedProcess:
    """在**没有假库**的子进程里跑一段代码（本进程装了假 cst，必须隔离）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT)] + [p for p in [env.get("PYTHONPATH")] if p])
    env.pop("PYTHONSTARTUP", None)
    return subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                          env=env, capture_output=True, text=True, timeout=120)


# --------------------------------------------------------------------------- #
# 1. 仓库根没有 cst/（PEP 420 遮蔽）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["cst", "cst.py"])
def test_repo_root_has_no_cst_module(name):
    """根目录的 ``cst/`` 会挡住官方库：import 成功但内容全不对。

    工程文件放在 ``<output.dir>/cst/``（不是仓库根）、假库放在
    ``tests/fake_cst/``——都不在这个路径上。
    """
    assert not (REPO_ROOT / name).exists(), (
        f"仓库根出现了 {name}：它会遮蔽 CST 官方库（PEP 420），"
        "请改名或移走（工程文件用 <output.dir>/cst/）")


# --------------------------------------------------------------------------- #
# 2. 不依赖 CST 的模块
# --------------------------------------------------------------------------- #
def test_cst_free_modules_do_not_import_cst():
    """这些模块在没装 CST 的机器上必须能导入（画图/校验/CI 都要用）。"""
    code = "import sys\n" + "".join(f"import {m}\n" for m in CST_FREE) + (
        "leaked = sorted(k for k in sys.modules "
        "if k == 'cst' or k.startswith('cst.'))\n"
        "assert not leaked, f'这些模块偷偷 import 了 cst：{leaked}'\n")
    done = _run_without_fake_cst(code)
    assert done.returncode == 0, done.stderr


def test_cst_solver_module_fails_loudly_without_the_official_library():
    """没装官方库时 import 阶段就给可照做的提示（不是 ImportError 裸奔）。"""
    code = ("import importlib.util\n"
            "if importlib.util.find_spec('cst') is not None:\n"
            "    print('SKIP::装了 CST，这条不适用')\n"
            "    raise SystemExit(0)\n"
            "import eaopt.solver.cst\n")
    done = _run_without_fake_cst(code)
    if "SKIP::" in done.stdout:
        pytest.skip("本机装了 CST 官方库")
    assert done.returncode != 0
    assert "python_cst_libraries" in done.stderr      # 给出装法
    assert "cst/" in done.stderr                      # 给出遮蔽警告


# --------------------------------------------------------------------------- #
# 3. 两个 init 脚本只差 TAG
# --------------------------------------------------------------------------- #
def test_init_scripts_differ_only_in_the_tag_block():
    """fwd/bwd 是同一份程序：除 TAG 常量区外必须逐字节相同。"""
    def split(path: Path):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        i = lines.index(TAG_MARK)
        j = lines.index(TAG_END, i)
        return lines[:i], lines[i:j + 1], lines[j + 1:]

    head_f, tag_f, tail_f = split(SCRIPTS / "cst_init_fwd.py")
    head_b, tag_b, tail_b = split(SCRIPTS / "cst_init_bwd.py")

    assert head_f == head_b                     # 文档字符串与导入区
    assert tail_f == tail_b                     # main 与全部辅助函数
    assert tag_f != tag_b
    # 差异只在 TAG 的取值那一行：把 TAG 行剔掉两份块完全相同
    assert [l for l in tag_f if not l.startswith("TAG")] == \
           [l for l in tag_b if not l.startswith("TAG")]
    assert any(l.startswith('TAG = "fwd"') for l in tag_f)
    assert any(l.startswith('TAG = "bwd"') for l in tag_b)


def test_init_scripts_guard_the_import_and_do_nothing_on_import():
    """两个脚本都要有顶层 import 守卫，且 import 时零副作用（能按路径加载）。"""
    for tag in ("fwd", "bwd"):
        path = SCRIPTS / f"cst_init_{tag}.py"
        src = path.read_text(encoding="utf-8")
        assert "import cst.interface" in src
        assert "raise SystemExit" in src        # 守卫给出了可照做的提示

        spec = importlib.util.spec_from_file_location(f"_contract_init_{tag}",
                                                      path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)            # 不连 CST、不读配置
        assert callable(mod.main)
