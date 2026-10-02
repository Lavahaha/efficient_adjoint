"""求解器层：抽象接口 + CST 实现（直接用 CST 官方 Python API，不再封装一层）。

六个文件的分工：

  ``base``        ``SolverInterface`` / ``Solution`` —— pipeline 只认这个契约
  ``cst_setup``   CST 侧常量与规则（频点、材料、端口、工程路径；纯数据）
  ``cst_model``   模板命令块（VBA 文本 + 常量规则；纯文本，不 import cst）
  ``cst_results`` S 参数读取、场导出/解析/裁剪（顶层 ``import cst.results``）
  ``cst``         ``CstSolver``：pipeline ↔ 两个工程的适配器（顶层
                  ``import cst.interface``，自带改形状/求解/取数）
  ``__init__``    本文件

``import cst.interface`` 只出现在 ``cst.py`` 与三个 ``scripts/cst_*.py`` 的
顶层；``import cst.results`` 只出现在 ``cst_results.py``。上面几个"纯规则"
模块（``cst_setup``/``cst_model``/``base``）不依赖 CST，没装 CST 的机器也能
导入它们（合同测试锁这一点）。其余模块在服务器上按 ``docs/server_runbook.md``
装好官方库即可用；本机无 CST，测试走 ``tests/fake_cst/``。
"""
