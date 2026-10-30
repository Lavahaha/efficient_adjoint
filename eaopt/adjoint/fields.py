"""Regular complex field grids exported by CST."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["FieldGrid"]


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

    def axes(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """各轴坐标（mm）。"""
        return tuple(
            self.origin[k] + self.spacing[k] * np.arange(self.data.shape[k])
            for k in range(3)
        )


    def plane(self, iz: int) -> np.ndarray:
        """第 ``iz`` 个 z 面的场（nx, ny, 3）complex。"""
        return self.data[:, :, int(iz), :]
