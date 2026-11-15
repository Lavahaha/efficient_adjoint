"""CST template for a continuously optimized, single-feed patch antenna."""
from __future__ import annotations

import math
import json
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

from eaopt.solver.cst_model import (
    DESIGN_COMPONENT, MESH_CREATOR, UNITS_BLOCK, block_header, brick,
    delete_component, field_monitor, frequency_range, material_normal,
    polygon_extrude, set_boundaries, solver_setup, waveguide_port,
)
from eaopt.solver.cst_setup import ANTENNA
from eaopt.geometry.contour import MetalRegion

__all__ = [
    "template_blocks", "model_blocks", "setting_blocks", "block_header",
    "UNITS_BLOCK", "DESIGN_COMPONENT", "design_region_update", "layout_view",
    "fixed_metal_polygons", "BOARD", "PATCH", "FEED_X", "FEED_Y",
    "FEED_PAD",
    "SUB_H", "METAL_T", "FREQ", "FMIN", "FMAX", "AIR_H",
    "PIN_DIAMETER", "GROUND_CLEARANCE_DIAMETER",
    "COAX_LENGTH", "COAX_OUTER_RADIUS", "COAX_PORT_Z", "COAX_EPS_R",
    "SUPPORTS_NESTED_CONTOURS", "DESIGN_BOUNDARY_PAD_MM",
    "audit_design_regions",
]

BOARD = (-30.0, 30.0)
DESIGN_X0 = -20.0
PATCH_LENGTH = 20.0       # l_p1 / 2 from Table I
PATCH_WIDTH = 20.0        # l_w1 / 2 from Table I
FEED_PAD_SIZE = 5.0       # l_1 from Table I; fixed square outside the design region
PATCH = (DESIGN_X0, DESIGN_X0 + PATCH_LENGTH)
PATCH_Y = (-PATCH_WIDTH / 2.0, PATCH_WIDTH / 2.0)
FEED_X, FEED_Y = DESIGN_X0 - FEED_PAD_SIZE / 2.0, 0.0
FEED_PAD = [
    (DESIGN_X0 - FEED_PAD_SIZE, -FEED_PAD_SIZE / 2.0),
    (DESIGN_X0, -FEED_PAD_SIZE / 2.0),
    (DESIGN_X0, FEED_PAD_SIZE / 2.0),
    (DESIGN_X0 - FEED_PAD_SIZE, FEED_PAD_SIZE / 2.0),
]
PIN_DIAMETER = 1.27       # d_1 from Table I
GROUND_CLEARANCE_DIAMETER = 4.10  # d_2 from Table I
SUB_H = ANTENNA.substrate_h_mm
METAL_T = ANTENNA.metal_thickness_mm
FREQ = ANTENNA.frequency_ghz
FMIN, FMAX = 6.0, 9.0
AIR_H = 10.0
SUBSTRATE = "Rogers4003C"
GROUND_Z = -SUB_H - METAL_T
COAX_LENGTH = 5.0
COAX_OUTER_RADIUS = GROUND_CLEARANCE_DIAMETER / 2.0 + 0.5
COAX_PORT_Z = GROUND_Z - COAX_LENGTH
# Match the existing pin/clearance diameters to nominal 50 ohms without
# post-solve renormalization, so S parameters and adjoint fields stay consistent.
COAX_EPS_R = (59.9584916 * math.log(GROUND_CLEARANCE_DIAMETER / PIN_DIAMETER) / 50.0) ** 2
COAX_DIELECTRIC = "AntennaCoaxDielectric"
SUPPORTS_NESTED_CONTOURS = True
DESIGN_BOUNDARY_PAD_MM = 0.0


def audit_design_regions(m3d, regions, height_mm, destination):
    """Check actual extruded volume and bounds before permitting a field solve.

    These checks detect gross import/truncation errors, not every possible shape
    discrepancy. They do not certify the accuracy of the electromagnetic mesh.
    """
    regions = list(regions)
    if not regions or not np.isfinite(height_mm) or height_mm <= 0:
        raise ValueError('Positive thickness and nonempty antenna regions required')
    expected = [Polygon(r.outer,r.holes) for r in regions]
    if any(not p.is_valid or p.area <= 0 for p in expected):
        raise ValueError('Antenna audit requires valid nonempty polygonal regions')
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(dict(passed=False,status='started')),encoding='utf-8')
    raw = destination.with_suffix('.txt')
    raw.unlink(missing_ok=True)
    quoted_path = str(raw).replace('"','""')
    code = ['Sub Main','Dim a As Double,b As Double,c As Double,d As Double,e As Double,f As Double',
            'Dim ok As Boolean',f'Open "{quoted_path}" For Output As #1']
    for i in range(len(regions)):
        name = f'{DESIGN_COMPONENT}:antenna_metal_{i}'
        code += [f'Print #1, Solid.GetVolume("{name}")',
                 f'Print #1, Solid.GetNumberOfPoints("{name}")',
                 f'ok=Solid.GetLooseBoundingBoxOfShape("{name}",a,b,c,d,e,f)',
                 'If Not ok Then Err.Raise 1, , "Antenna bounding box unavailable"',
                 'Print #1,a','Print #1,b','Print #1,c','Print #1,d','Print #1,e','Print #1,f']
    code += ['Close #1','End Sub']
    m3d._execute_vba_code('\n'.join(code))
    data = np.array(list(map(float,raw.read_text().split())))
    if data.size != 8*len(regions) or not np.isfinite(data).all():
        raise ValueError('Incomplete or nonfinite CST antenna geometry audit')
    records=[]
    for i,(polygon,row) in enumerate(zip(expected,data.reshape(-1,8))):
        xmin,ymin,xmax,ymax=polygon.bounds
        volume=polygon.area*height_mm
        bounds=[xmin,xmax,ymin,ymax,0.,height_mm]
        records.append(dict(region=i,expected_volume_mm3=volume,native_volume_mm3=float(row[0]),
                            native_vertices=float(row[1]),expected_bounds_mm=bounds,native_bounds_mm=row[2:].tolist()))
    passed=all(np.isclose(r['native_volume_mm3'],r['expected_volume_mm3'],rtol=1e-8,atol=1e-9)
               and np.allclose(r['native_bounds_mm'],r['expected_bounds_mm'],rtol=0,atol=1e-9)
               and r['native_vertices'] >= 4 and float(r['native_vertices']).is_integer() for r in records)
    destination.write_text(json.dumps(dict(passed=bool(passed),status='complete',regions=records),indent=2),encoding='utf-8')
    if not passed:
        raise ValueError(f'CST antenna solid differs from its requested volume/bounds: {destination}')
    return records


def design_region_update(regions, height_mm: float,
                         component: str = DESIGN_COMPONENT,
                         material: str = "PEC", z0: float = 0.0) -> str:
    """Extrude each metal outer ring and subtract its enclosed hole tools."""
    regions = list(regions)
    if regions and not isinstance(regions[0], MetalRegion):
        from eaopt.geometry.contour import classify_metal_regions
        regions = classify_metal_regions(regions)
    if not regions:
        raise ValueError("regions 为空：拒绝生成没有辐射金属的天线")
    cut_overrun = 0.05
    commands = [delete_component(component)]
    for region_index, region in enumerate(regions):
        outer_name = f"antenna_metal_{region_index}"
        commands.append(polygon_extrude(
            outer_name, component, material, region.outer, height_mm, z0=z0))
        for hole_index, hole in enumerate(region.holes):
            tool_name = f"antenna_hole_{region_index}_{hole_index}"
            commands.append(polygon_extrude(
                tool_name, component, material, hole,
                height_mm + 2.0 * cut_overrun,
                z0=z0 - cut_overrun))
            commands.append(
                f'Solid.Subtract "{component}:{outer_name}", '
                f'"{component}:{tool_name}"\n')
    return "".join(commands)


def _coax_port() -> str:
    return waveguide_port(
        1, "coax_feed", "zmin", COAX_PORT_Z,
        FEED_X - COAX_OUTER_RADIUS, FEED_X + COAX_OUTER_RADIUS,
        FEED_Y - COAX_OUTER_RADIUS, FEED_Y + COAX_OUTER_RADIUS,
        port_on_bound=False,
    )


def model_blocks() -> list[str]:
    """Build the antenna and a uniform coaxial feed with a single-mode port."""
    x0, x1 = BOARD
    y0, y1 = BOARD
    px0, px1 = PATCH
    py0, py1 = PATCH_Y
    clearance_radius = GROUND_CLEARANCE_DIAMETER / 2.0
    ground_cut_overrun = 0.01
    substrate_cut_overrun = 0.01
    return [
        material_normal(SUBSTRATE, ANTENNA.eps_r, 1.0, ANTENNA.loss_tangent),
        material_normal(COAX_DIELECTRIC, COAX_EPS_R, 1.0, 0.0),
        brick("substrate", "component1", SUBSTRATE,
              x0, x1, y0, y1, -SUB_H, 0.0),
        _cylinder("substrate_probe_bore", "component1", "Vacuum",
                  FEED_X, FEED_Y, PIN_DIAMETER / 2.0,
                  -SUB_H - substrate_cut_overrun,
                  substrate_cut_overrun),
        'Solid.Subtract "component1:substrate", '
        '"component1:substrate_probe_bore"\n',
        brick("ground", "component1", "PEC",
              x0, x1, y0, y1, GROUND_Z, -SUB_H),
        _cylinder("ground_feed_clearance", "component1", "Vacuum",
                  FEED_X, FEED_Y, clearance_radius,
                  GROUND_Z - ground_cut_overrun,
                  -SUB_H + ground_cut_overrun),
        'Solid.Subtract "component1:ground", '
        '"component1:ground_feed_clearance"\n',
        brick("air", "component1", "Vacuum",
              x0, x1, y0, y1, METAL_T, AIR_H),
        polygon_extrude(
            "feed_pad_fixed", "component1", "PEC", FEED_PAD, METAL_T),
        polygon_extrude(
            "patch_initial", DESIGN_COMPONENT, "PEC",
            [(px0, py0), (px1, py0), (px1, py1), (px0, py1)], METAL_T),
        _cylinder("feed_probe", "component1", "Copper (annealed)",
                  FEED_X, FEED_Y, PIN_DIAMETER / 2.0,
                  COAX_PORT_Z, 0.0),
        _cylinder("coax_dielectric", "component1", COAX_DIELECTRIC,
                  FEED_X, FEED_Y, clearance_radius,
                  COAX_PORT_Z, GROUND_Z, inner_radius=PIN_DIAMETER / 2.0),
        _cylinder("coax_outer", "component1", "PEC",
                  FEED_X, FEED_Y, COAX_OUTER_RADIUS,
                  COAX_PORT_Z, -SUB_H, inner_radius=clearance_radius),
        _coax_port(),
    ]


def _cylinder(name: str, component: str, material: str,
              x: float, y: float, radius: float,
              z0: float, z1: float, *, inner_radius: float = 0.0) -> str:
    """CST z-axis cylinder; coordinates and radii are in millimetres."""
    return (
        "With Cylinder\n"
        "    .Reset\n"
        f'    .Name "{name}"\n'
        f'    .Component "{component}"\n'
        f'    .Material "{material}"\n'
        '    .Axis "z"\n'
        f'    .OuterRadius "{radius:.6g}"\n'
        f'    .InnerRadius "{inner_radius:.6g}"\n'
        f'    .Xcenter "{x:.6g}"\n'
        f'    .Ycenter "{y:.6g}"\n'
        f'    .Zrange "{z0:.6g}", "{z1:.6g}"\n'
        '    .Segments "0"\n'
        "    .Create\n"
        "End With\n"
    )


def setting_blocks(portnum: int, solver_type: str = "td") -> list[tuple[str, str]]:
    if int(portnum) != 1:
        raise ValueError(f"antenna template only has waveguide port 1, got {portnum}")
    return [
        ("Mesh", MESH_CREATOR),
        ("Solver", solver_setup(portnum, FREQ, solver_type)),
        ("FrequencyRange", frequency_range(FMIN, FMAX)),
        ("Monitor Efield", field_monitor("Efield", FREQ)),
        ("Monitor Hfield", field_monitor("Hfield", FREQ)),
        ("Boundary", set_boundaries(
            "expanded open", "expanded open",
            "expanded open", "expanded open",
            "expanded open", "expanded open")),
    ]


def template_blocks(project: str, portnum: int, solver_type: str = "td") -> list[tuple[str, str]]:
    blocks = [("Units", UNITS_BLOCK)]
    blocks += [(block_header(command), command) for command in model_blocks()]
    blocks += setting_blocks(portnum, solver_type)
    return blocks


def initial_patch_polygon():
    px0, px1 = PATCH
    py0, py1 = PATCH_Y
    return [(px0, py0), (px1, py0), (px1, py1), (px0, py1)]


def fixed_metal_polygons() -> list[list[tuple[float, float]]]:
    return [FEED_PAD]


def layout_view() -> dict:
    x0, x1 = BOARD
    y0, y1 = BOARD
    return {
        "bounds": (x0 - 10.0, x1 + 10.0, y0 - 10.0, y1 + 10.0),
        "shapes": [
            ("substrate", [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
             "Rogers 4003C"),
            ("metal", FEED_PAD, "5 mm fixed feed pad"),
            ("metal", initial_patch_polygon(), "20 × 20 mm initial design patch"),
        ],
        "ports": [(1, FEED_X, FEED_Y, 0, 1)],
    }
