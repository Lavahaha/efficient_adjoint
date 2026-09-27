"""场数据与插值（求解器输出 -> 形状导数输入）。

求解器（CST / Mock）把 E、H 复矢量场导出在规则笛卡尔网格上
（设计区包围盒 + 余量），FieldGrid 统一承载并支持三线性插值到
任意采样点；法向/切向分解服务于形状导数（论文式 25）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.interpolate import RegularGridInterpolator

__all__ = ["FieldGrid", "decompose_normal_tangential"]


@dataclass
class FieldGrid:
    """规则笛卡尔网格上的复矢量场（E: V/m，H: A/m）。

    origin: (x0, y0, z0) mm；spacing: (dx, dy, dz) mm；
    data: (nx, ny, nz, 3) complex。z 轴与设计平面法向一致。
    """

    origin: tuple[float, float, float]
    spacing: tuple[float, float, float]
    data: np.ndarray  # (nx, ny, nz, 3) complex

    def __post_init__(self):
        self.data = np.asarray(self.data)
        if self.data.ndim != 4 or self.data.shape[3] != 3:
            raise ValueError("data 形状必须为 (nx, ny, nz, 3)")
        self._interp = None

    def axes(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """各轴坐标（mm）。"""
        return tuple(
            self.origin[k] + self.spacing[k] * np.arange(self.data.shape[k])
            for k in range(3)
        )

    def interp(self, points: np.ndarray) -> np.ndarray:
        """三线性插值到 points (N,3) mm，返回 (N,3) complex。

        网格外返回 0（bounds_error=False, fill_value=0）。
        插值器按需构建并缓存（每次迭代大量采样点复用）。
        """
        if self._interp is None:
            self._interp = RegularGridInterpolator(
                self.axes(), self.data, bounds_error=False, fill_value=0.0
            )
        return self._interp(np.asarray(points, dtype=float))


def decompose_normal_tangential(
    field: np.ndarray, normals: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """把场分解为法向与切向分量。

    field: (N,3) complex；normals: (N,3)（自动归一化）。
    复矢量点积不取共轭（与论文推导一致；符号问题由 FD 验证裁决）。
    返回 (field_normal, field_tangential)，均为 (N,3)。
    """
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    n = normals / np.where(length < 1e-30, 1.0, length)
    dot = np.einsum("ij,ij->i", field, n)
    normal_part = dot[:, None] * n
    return normal_part, field - normal_part
