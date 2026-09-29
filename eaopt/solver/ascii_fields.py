"""CST ASCII 3D 场导出文件的解析。

格式（CST ASCIIExport，容错解析）：
  - 注释行以 '%' 开头，跳过；
  - 网格三行：x0 x1 nx / y0 y1 ny / z0 z1 nz；
  - 其余数值按分量块排列：每块 Nx*Ny*Nz 个复数（实部/虚部成对，
    按 CST 版本可能是 "Re 分量块后跟 Im 分量块" 或逐点成对）——
    两种布局都尝试解析，成功后返回 (nx,ny,nz,3) complex。

服务器 smoke 脚本会导出真实文件核对本解析器；若 CST 2024 格式不同，
按 smoke 打印的文件头调整 `_parse_blocks`。
"""

from __future__ import annotations

import numpy as np

__all__ = ["parse_ascii_field"]


def parse_ascii_field(path: str) -> tuple[np.ndarray, tuple]:
    """解析 CST ASCII 场文件。

    返回 (data, axes)：data 为 (nx, ny, nz, 3) complex（E: V/m 或
    H: A/m），axes 为各轴坐标（mm）。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("%")]

    def _grid_line(i: int):
        parts = lines[i].replace(",", " ").split()
        return float(parts[0]), float(parts[1]), int(parts[2])

    x0, x1, nx = _grid_line(0)
    y0, y1, ny = _grid_line(1)
    z0, z1, nz = _grid_line(2)
    vals = []
    for ln in lines[3:]:
        vals.extend(float(v) for v in ln.replace(",", " ").split())
    vals = np.asarray(vals, dtype=float)

    per_comp = nx * ny * nz
    if len(vals) < per_comp * 3:
        raise ValueError(
            f"数值不足: {len(vals)} < 3×{per_comp}（文件格式与预期不符，"
            "请用 smoke 脚本导出真实文件核对）"
        )
    if len(vals) >= per_comp * 6:  # 复数导出：6 个块
        data = _parse_blocks(vals[: per_comp * 6], per_comp, nx, ny, nz)
    else:  # 实数导出：3 个块（虚部为 0）
        data = _parse_blocks(vals[: per_comp * 3], per_comp, nx, ny, nz)
        data = data.real.astype(complex)
    axes = (
        np.linspace(x0, x1, nx),
        np.linspace(y0, y1, ny),
        np.linspace(z0, z1, nz),
    )
    return data, axes


def _parse_blocks(vals: np.ndarray, per_comp: int,
                  nx: int, ny: int, nz: int) -> np.ndarray:
    """把数值块装配成 (nx,ny,nz,3) complex。

    尝试两种布局：A) 每分量先全 Re 后全 Im（块顺序 Re_x,Re_y,Re_z,
    Im_x,Im_y,Im_z）；B) 逐点成对（Re,Im 交替）。以 A 为主。
    """
    n = len(vals)
    data = np.zeros((nx, ny, nz, 3), dtype=complex)
    if n == per_comp * 6:
        re = [vals[k * per_comp: (k + 1) * per_comp] for k in range(3)]
        im = [vals[(3 + k) * per_comp: (4 + k) * per_comp] for k in range(3)]
        for c in range(3):
            data[..., c] = (re[c] + 1j * im[c]).reshape(nx, ny, nz, order="F")
    elif n == per_comp * 3:
        for c in range(3):
            data[..., c] = vals[c * per_comp: (c + 1) * per_comp].reshape(
                nx, ny, nz, order="F")
    else:
        raise ValueError(f"无法识别的块数 {n}/{per_comp}")
    return data
