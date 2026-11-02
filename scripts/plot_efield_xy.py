"""Plot an xy-plane electric-field vector map exported by CST.

The input is CST ``ASCIIExport`` text with columns
``x y z ExRe ExIm EyRe EyIm EzRe EzIm``.  Only the requested z plane is
loaded into memory, which keeps this script usable with large volume exports.

Examples
--------
    python scripts/plot_efield_xy.py --z 0 --output docs/efield_z0.png
    python scripts/plot_efield_xy.py --z -0.7485 --step 2 --phase-deg 90
    python scripts/plot_efield_xy.py --z 0 --x 0.5 11.5 --y -2.6 0.9
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402


DEFAULT_INPUT = Path("results/coupler/cst_work/bwd_Efield_5GHz.txt")
DEFAULT_OUTPUT = Path("docs/efield_xy.png")


def _data_row(line: str) -> list[float] | None:
    """Return one numeric CST row, ignoring headers and separator lines."""
    fields = line.split()
    if len(fields) != 9:
        return None
    try:
        return [float(value) for value in fields]
    except ValueError:
        return None


def _scan_z_values(path: Path) -> np.ndarray:
    """Find all z coordinates without retaining the volume field."""
    values: set[float] = set()
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            row = _data_row(line)
            if row is not None:
                values.add(row[2])
    if not values:
        raise ValueError(f"场文件没有找到 9 列数值数据：{path}")
    return np.array(sorted(values), dtype=float)


def _select_z(z_values: np.ndarray, requested: float) -> float:
    """Select the nearest exported plane, rejecting a clearly invalid request."""
    index = int(np.argmin(np.abs(z_values - requested)))
    selected = float(z_values[index])
    tolerance = max(1e-9, 1e-6 * max(1.0, abs(selected)))
    if abs(selected - requested) > tolerance:
        available = ", ".join(f"{z:g}" for z in z_values)
        raise ValueError(
            f"请求的 z={requested:g} mm 不在文件中；可用 z 平面为：{available}"
        )
    return selected


def _load_plane(path: Path, z: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load one plane as ``(x, y, E)`` where E has shape ``(n, 3)``."""
    rows: list[list[float]] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            row = _data_row(line)
            if row is not None and np.isclose(row[2], z, rtol=0.0, atol=1e-9):
                rows.append(row)

    if not rows:
        raise ValueError(f"z={z:g} mm 平面没有数据：{path}")
    values = np.asarray(rows, dtype=float)
    x = np.unique(values[:, 0])
    y = np.unique(values[:, 1])
    if x.size < 2 or y.size < 2:
        raise ValueError("选定平面不是二维网格：x 和 y 至少各需要两个坐标")
    if values.shape[0] != x.size * y.size:
        raise ValueError(
            f"z={z:g} mm 平面不是完整规则网格："
            f"{values.shape[0]} 行 != {x.size} x {y.size}"
        )

    x_index = np.searchsorted(x, values[:, 0])
    y_index = np.searchsorted(y, values[:, 1])
    flat = np.ravel_multi_index((x_index, y_index), (x.size, y.size))
    if np.unique(flat).size != flat.size:
        raise ValueError(f"z={z:g} mm 平面包含重复的 x-y 网格点")

    field = np.zeros((x.size, y.size, 3), dtype=complex)
    for component in range(3):
        real = values[:, 3 + 2 * component]
        imag = values[:, 4 + 2 * component]
        field[x_index, y_index, component] = real + 1j * imag
    return x, y, field


def plot_plane(
    path: Path,
    output: Path,
    *,
    z: float,
    step: int = 1,
    phase_deg: float = 0.0,
    arrow_length: float | None = None,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
) -> Path:
    """Read and plot one xy plane, returning the written image path."""
    if step < 1:
        raise ValueError(f"--step 必须是正整数，收到 {step}")
    if x_range is not None and x_range[0] >= x_range[1]:
        raise ValueError(f"--x 范围必须满足最小值 < 最大值，收到 {x_range}")
    if y_range is not None and y_range[0] >= y_range[1]:
        raise ValueError(f"--y 范围必须满足最小值 < 最大值，收到 {y_range}")
    z_value = _select_z(_scan_z_values(path), z)
    x, y, field = _load_plane(path, z_value)
    if x_range is not None:
        x_mask = (x >= x_range[0]) & (x <= x_range[1])
        x = x[x_mask]
        field = field[x_mask, :, :]
    if y_range is not None:
        y_mask = (y >= y_range[0]) & (y <= y_range[1])
        y = y[y_mask]
        field = field[:, y_mask, :]
    if x.size < 2 or y.size < 2:
        raise ValueError("指定的绘制区域至少需要包含两个 x 坐标和两个 y 坐标")
    if step > 1:
        x = x[::step]
        y = y[::step]
        field = field[::step, ::step, :]

    # Arrow length is the in-plane phasor amplitude, not the total 3-D field.
    magnitude = np.sqrt(np.sum(np.abs(field[:, :, :2]) ** 2, axis=2))
    phase = np.deg2rad(phase_deg)
    in_plane = np.real(field[:, :, :2] * np.exp(1j * phase))
    direction_norm = np.linalg.norm(in_plane, axis=2)
    arrow_length = (
        arrow_length
        if arrow_length is not None
        else 3.5 * min(np.diff(x).min(), np.diff(y).min())
    )
    if arrow_length <= 0:
        raise ValueError("--arrow-length 必须为正数（单位：mm）")
    max_magnitude = float(np.nanmax(magnitude))
    if max_magnitude > 0:
        u = np.divide(
            in_plane[:, :, 0] * magnitude,
            direction_norm,
            out=np.zeros_like(magnitude),
            where=direction_norm > 0,
        )
        v = np.divide(
            in_plane[:, :, 1] * magnitude,
            direction_norm,
            out=np.zeros_like(magnitude),
            where=direction_norm > 0,
        )
        u *= arrow_length / max_magnitude
        v *= arrow_length / max_magnitude
    else:
        u = np.zeros_like(magnitude)
        v = np.zeros_like(magnitude)

    xx, yy = np.meshgrid(x, y, indexing="ij")
    fig, ax = plt.subplots(figsize=(10, 7))
    ax.pcolormesh(
        x,
        y,
        magnitude.T,
        shading="auto",
        cmap="inferno",
        alpha=0.28,
    )
    arrows = ax.quiver(
        xx,
        yy,
        u,
        v,
        magnitude,
        angles="xy",
        scale_units="xy",
        scale=1,
        pivot="mid",
        width=0.0025,
        headwidth=3.5,
        headlength=4.5,
        headaxislength=4,
        cmap="inferno",
    )
    fig.colorbar(arrows, ax=ax, label="|E_xy| (V/m)")
    ax.set(
        aspect="equal",
        xlabel="x (mm)",
        ylabel="y (mm)",
        title=(
            f"Electric field on z={z_value:g} mm "
            f"(arrows: E_xy at phase {phase_deg:g} deg)"
        ),
    )
    if x_range is not None:
        ax.set_xlim(x_range)
    if y_range is not None:
        ax.set_ylim(y_range)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150)
    plt.close(fig)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="CST E-field text file")
    parser.add_argument("--z", type=float, required=True, help="z plane in mm")
    parser.add_argument(
        "--x",
        dest="x_range",
        nargs=2,
        type=float,
        metavar=("X_MIN", "X_MAX"),
        help="plot x range in mm, for example: --x 0.5 11.5",
    )
    parser.add_argument(
        "--y",
        dest="y_range",
        nargs=2,
        type=float,
        metavar=("Y_MIN", "Y_MAX"),
        help="plot y range in mm, for example: --y -2.6 0.9",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output PNG path")
    parser.add_argument("--step", type=int, default=1, help="plot every Nth grid point")
    parser.add_argument(
        "--phase-deg",
        type=float,
        default=0.0,
        help="phase used for the Ex/Ey arrow direction (degrees)",
    )
    parser.add_argument(
        "--arrow-length",
        type=float,
        default=None,
        help="maximum arrow length in mm (default: 3.5 times the grid spacing)",
    )
    args = parser.parse_args()
    output = plot_plane(
        args.input,
        args.output,
        z=args.z,
        step=args.step,
        phase_deg=args.phase_deg,
        arrow_length=args.arrow_length,
        x_range=tuple(args.x_range) if args.x_range is not None else None,
        y_range=tuple(args.y_range) if args.y_range is not None else None,
    )
    print(f"已写出电场图: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
