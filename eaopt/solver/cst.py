"""CST 求解器实现（服务器端，CST 2024；本地单测不依赖 CST）。

设计要点：
  - 双模板：coupler_fwd.cst（端口 1 激励）与 coupler_bwd.cst（端口 3
    激励），由 scripts/build_cst_template.py 生成的 .mcr 命令宏在 CST
    GUI 中执行一次创建。激励"烤死"在模板里，pipeline 不触碰激励 API
    （版本兼容性最稳）；
  - build_model 只重建两个工程中的 "design_region" 组件（可动金属
    多边形挤出 35µm），其余几何/设置永不改动；
  - 场导出：ASCII 后端（版本最稳），解析器见 ascii_fields.py；
    ResultReader COM / HDF5 后端待服务器 smoke 实测后启用；
  - 工作目录 cfg.output.dir/cst_work：模板副本在此打开运行，每轮
    迭代覆盖保存。

服务器首次运行前：先跑 scripts/cst_smoke.py 验证 COM 命令可用性
（S 参数读取路径、场导出格式），按其输出修正本模块。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from eaopt.adjoint.fields import FieldGrid
from eaopt.config import CaseConfig
from eaopt.geometry.contour import close_open_contours
from eaopt.solver import cst_api
from eaopt.solver import cst_project
from eaopt.solver import vba as V
from eaopt.solver.ascii_fields import parse_ascii_field
from eaopt.solver.base import Solution, SolverInterface

__all__ = ["CstSolver"]


class CstSolver(SolverInterface):
    def __init__(self, cfg: CaseConfig):
        self.cfg = cfg
        self._app = None
        self._fwd = None
        self._bwd = None
        self._workdir: Path | None = None

    # ------------------------------------------------------------------ #
    # 连接与工程管理
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        """连接 CST（服务器端）。优先附接已运行实例（GUI 已开时）。"""
        import win32com.client

        for factory in (
            lambda: win32com.client.Dispatch("CSTStudio.Application"),
            lambda: win32com.client.GetActiveObject("CSTStudio.Application"),
        ):
            try:
                self._app = factory()
                break
            except Exception:
                continue
        if self._app is None:
            raise RuntimeError(
                "无法连接 CST：请确认 CST Studio 2024 已启动（或可启动）且"
                "许可证正常；重试或先运行 GUI"
            )
        self._open_projects()

    def _open_projects(self) -> None:
        workdir = Path(self.cfg.output.dir) / "cst_work"
        workdir.mkdir(parents=True, exist_ok=True)
        self._workdir = workdir
        self._fwd = self._open_copy(Path(self.cfg.solver.template_fwd), workdir, "fwd")
        self._bwd = self._open_copy(Path(self.cfg.solver.template_bwd), workdir, "bwd")

    def _open_copy(self, template: Path, workdir: Path, tag: str):
        """把模板整份复制到工作目录再打开（**含同名文件夹**，见 cst_project）。

        只复制 `.cst` 文件会丢掉同名文件夹里的东西，打开后可能是个空工程
        （没有几何/端口/结果），CST 不报错——所以复制一律走 cst_project。
        """
        if not template.exists():
            raise FileNotFoundError(f"模板不存在: {template}（先跑 build_cst_template 宏）")
        dst = cst_project.copy_project(template, workdir,
                                       dst_name=f"{tag}_{template.name}")
        return self._app.OpenFile(str(dst))

    def close(self) -> None:
        """保存并退出（尽力而为）。"""
        for mws in (self._fwd, self._bwd):
            if mws is not None:
                try:
                    mws.Save()
                except Exception:
                    pass
        if self._app is not None:
            try:
                self._app.Quit()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # SolverInterface
    # ------------------------------------------------------------------ #
    def build_model(self, movable: list, fixed: list) -> None:
        """重建两个工程的设计区组件。

        movable: 可动金属轮廓（世界坐标 mm，可能开放——穿出设计区
        边界的臂上下边）；fixed: 固定金属多边形（已包含在模板中，
        此处忽略）。
        """
        if self._fwd is None or self._bwd is None:
            self.connect()
        polys = close_open_contours(movable, self.cfg.design_region.box)
        for mws in (self._fwd, self._bwd):
            self._rebuild_design(mws, polys)

    def solve_forward(self) -> Solution:
        return self._solve(self._fwd)

    def solve_backward(self) -> Solution:
        return self._solve(self._bwd)

    # ------------------------------------------------------------------ #
    # 建模（COM 对象模型，与 vba.py 的字符串命令一一对应）
    # ------------------------------------------------------------------ #
    def _rebuild_design(self, mws, polys: list[np.ndarray]) -> None:
        try:
            mws.Component.Delete("design_region")
        except Exception:
            pass  # 组件不存在或 API 名称不同（smoke 核实）
        for i, poly in enumerate(polys):
            self._extrude(mws, f"solid_{i}", poly)

    def _extrude(self, mws, name: str, pts: np.ndarray) -> None:
        """多边形直接挤出（Extrude 对象 .Mode "Pointlist"，与 vba.py 一致）。

        注意：Extrude 无 PlaneNormal 属性（CST 2024 实测报 no such
        property），挤出方向只能由 Origin + Uvector + Vvector 给出。
        """
        e = mws.Extrude
        e.Reset()
        e.Name(name)
        e.Component("design_region")
        e.Material("PEC")
        e.Mode("Pointlist")
        e.Height(str(float(self.cfg.metal.thickness_mm)))
        e.Twist("0.0")
        e.Taper("0.0")
        e.Origin("0.0", "0.0", "0.0")
        e.Uvector("1.0", "0.0", "0.0")
        e.Vvector("0.0", "1.0", "0.0")
        e.Point(str(float(pts[0, 0])), str(float(pts[0, 1])))
        for x, y in pts[1:]:
            e.LineTo(str(float(x)), str(float(y)))
        e.Create()

    # ------------------------------------------------------------------ #
    # 仿真与结果读取
    # ------------------------------------------------------------------ #
    def _solve(self, mws) -> Solution:
        mws.Solver.Start()
        sp = self._read_sparams(mws)
        # 监视器名/结果树路径由 vba 模块统一给出（模板宏创建监视器时用的是
        # 同一个函数，见 vba.field_monitor_name）
        e_grid = self._export_field(mws, "Efield")
        h_grid = self._export_field(mws, "Hfield")
        return Solution(
            s_params=sp,
            e_field=e_grid,
            h_field=h_grid,
            pin=float(self.cfg.solver.port_power_w),
        )

    def _read_sparams(self, mws) -> dict:
        """读 5 GHz 处的 S_{i,1}（i=1..4）。

        读取链收敛在 cst_api（CST 2024 实测：ResultTree.GetResultItem
        不存在，可用链是 GetResultIDsFromTreeItem + GetResultFromTreeItem
        + GetArray("x"/"yre"/"yim")）。路径先在结果树里按前缀找真实条目
        （CST 会在监视器名后加后缀），找不到才退回按惯例拼的路径。
        读不到就抛——宁可直接失败，也不要把 0j 悄悄塞进伴随法。
        """
        f = float(self.cfg.frequency)
        rt = mws.ResultTree
        folder = "1D Results\\S-Parameters"
        out = {}
        for i in (1, 2, 3, 4):
            notes: list[str] = []
            path = (cst_api.find_item(rt, folder, f"S{i},1", notes)
                    or f"{folder}\\S{i},1")
            v = cst_api.s_param_at(rt, path, f, notes, project=mws)
            if v is None:
                raise RuntimeError(
                    f"读不到 S{i},1（{path}）：{' | '.join(notes)}。"
                    "跑 scripts/cst_smoke.py 看实际结果树（第 4 节）")
            out[(i, 1)] = v
        return out

    def _export_field(self, mws, field_type: str) -> FieldGrid:
        """导出该监视器结果（ASCII 后端）并解析为 FieldGrid。

        导出范围 = 监视器的整个包围盒（Volume 监视器 => 整个计算域）：
        CST 的 ASCIIExport 没有区域范围属性（见 vba.ascii_export_params），
        设计区的裁剪在采样端按世界坐标做（FieldGrid 自带 origin/spacing），
        所以这里不影响正确性，只是文件更大。
        """
        dx = self.cfg.design_region.grid_step_mm
        f = float(self.cfg.frequency)
        path = self._workdir / f"{V.FIELD_TYPES[field_type][0]}.txt"

        # 结果条目名由 CST 自动加后缀（"e-field (f=5)" → "… [AC]"），先在
        # 结果树里按前缀找真实路径；找不到才退回按惯例拼的路径。
        notes: list[str] = []
        folder = f"2D/3D Results\\{V.FIELD_TYPES[field_type][1]}"
        item = (cst_api.find_item(mws.ResultTree, folder,
                                  V.field_monitor_name(field_type, f), notes)
                or V.field_result_path(field_type, f))
        mws.SelectTreeItem(item)
        a = mws.ASCIIExport
        a.Reset()
        a.FileName(str(path))
        for prop, val in V.ascii_export_params(dx):
            getattr(a, prop)(val)
        try:
            getattr(a, V.ASCII_EXPORT_EXECUTE)()
        except Exception as e:
            raise RuntimeError(
                f"{field_type} 场导出失败（选中条目 {item!r}）：{e}。"
                "CST 的 'not available for the current view' 表示选中的条目"
                "不是可导出的场结果——先跑 scripts/cst_smoke.py 第 5 节看"
                "结果树里真实的条目名。诊断：" + (" | ".join(notes) or "无")
            ) from e

        data, axes = parse_ascii_field(str(path))
        return FieldGrid(
            origin=(float(axes[0][0]), float(axes[1][0]), float(axes[2][0])),
            spacing=(float(np.diff(axes[0])[0]), float(np.diff(axes[1])[0]),
                     float(np.diff(axes[2])[0])),
            data=data,
        )


