"""CST 求解器实现（服务器端，CST 2024；本地单测不依赖 CST）。

设计要点：
  - 双模板：coupler_fwd.cst（端口 1 激励）与 coupler_bwd.cst（端口 3
    激励），由 scripts/build_cst_template.py 生成的 .bas 宏在 CST GUI
    中执行一次创建。激励"烤死"在模板里，pipeline 不触碰激励 API
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

import shutil
from pathlib import Path

import numpy as np

from eaopt.adjoint.fields import FieldGrid
from eaopt.config import CaseConfig
from eaopt.geometry.contour import close_open_contours
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
        if not template.exists():
            raise FileNotFoundError(f"模板不存在: {template}（先跑 build_cst_template 宏）")
        dst = workdir / f"{tag}_{template.name}"
        shutil.copy(template, dst)
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
            curve = f"p_curve_{i}"
            self._polygon(mws, curve, poly)
            self._extrude(mws, f"solid_{i}", curve)

    def _polygon(self, mws, curve: str, pts: np.ndarray) -> None:
        p = mws.Polygon
        p.Reset()
        p.Name(curve)
        p.Curve(curve)
        p.Point(str(float(pts[0, 0])), str(float(pts[0, 1])))
        for x, y in pts[1:]:
            p.LineTo(str(float(x)), str(float(y)))
        p.Create()

    def _extrude(self, mws, name: str, curve: str) -> None:
        e = mws.Extrude
        e.Reset()
        e.Name(name)
        e.Component("design_region")
        e.Material("PEC")
        e.Origin("0.0", "0.0", "0.0")
        e.PlaneNormal("0", "0", "1")
        e.Height(str(float(self.cfg.metal.thickness_mm)))
        e.Twist("0")
        e.Taper("0")
        e.Create()

    # ------------------------------------------------------------------ #
    # 仿真与结果读取
    # ------------------------------------------------------------------ #
    def _solve(self, mws) -> Solution:
        mws.Solver.Start()
        sp = self._read_sparams(mws)
        e_grid = self._export_field(mws, "E-Field", "e-field")
        h_grid = self._export_field(mws, "H-Field", "h-field")
        return Solution(
            s_params=sp,
            e_field=e_grid,
            h_field=h_grid,
            pin=float(self.cfg.solver.port_power_w),
        )

    def _read_sparams(self, mws) -> dict:
        """读 5 GHz 处的 S_{i,1}（i=1..4）。

        读取方法链为候选列表（CST 版本差异），服务器 smoke 实测后
        收敛到第一个可用的。
        """
        f = float(self.cfg.frequency)
        out = {}
        for i in (1, 2, 3, 4):
            try:
                item = mws.ResultTree.GetResultItem(
                    f"1D Results\\S-Parameters\\S{i},1"
                )
                out[(i, 1)] = self._item_value(item, f)
            except Exception:
                out[(i, 1)] = 0j
        return out

    def _item_value(self, item, freq_ghz: float) -> complex:
        for meth, args in (
            ("GetValueAtFrequency", (freq_ghz,)),
            ("GetComplexValueAtFrequency", (freq_ghz,)),
        ):
            try:
                v = getattr(item, meth)(*args)
                return _to_complex(v)
            except Exception:
                continue
        # 回退：取数据数组并挑最近频点（smoke 核实数组布局）
        try:
            v = item.GetYData()
            return complex(np.asarray(v).ravel()[0])
        except Exception:
            raise RuntimeError("无法读取 S 参数（所有候选方法失败，见 smoke 输出）")

    def _export_field(self, mws, field_type: str, prefix: str) -> FieldGrid:
        dr = self.cfg.design_region
        box = dr.box
        m = dr.field_margin_mm
        dx = dr.grid_step_mm
        f = float(self.cfg.frequency)
        z0 = self.cfg.sampling.field_z_mm - 0.1
        z1 = self.cfg.sampling.field_z_mm + 0.1
        path = self._workdir / f"{prefix}.txt"

        mws.SelectTreeItem(f"2D/3D Results\\{field_type}\\{prefix} (f={f}) [AC]")
        a = mws.ASCIIExport
        a.Reset()
        a.FileName(str(path))
        a.Mode("FixedNumber")
        a.StepX(str(dx))
        a.StepY(str(dx))
        a.StepZ(str(dx))
        a.XStart(str(box.x[0] - m))
        a.XEnd(str(box.x[1] + m))
        a.YStart(str(box.y[0] - m))
        a.YEnd(str(box.y[1] + m))
        a.ZStart(str(z0))
        a.ZEnd(str(z1))
        a.Export()

        data, axes = parse_ascii_field(str(path))
        return FieldGrid(
            origin=(float(axes[0][0]), float(axes[1][0]), float(axes[2][0])),
            spacing=(float(np.diff(axes[0])[0]), float(np.diff(axes[1])[0]),
                     float(np.diff(axes[2])[0])),
            data=data,
        )


def _to_complex(v) -> complex:
    """把 CST 返回的数值（可能为复数对象/元组/字符串）转 complex。"""
    try:
        return complex(v)
    except (TypeError, ValueError):
        try:
            return complex(v[0], v[1])
        except (TypeError, ValueError, IndexError):
            return complex(float(v))
