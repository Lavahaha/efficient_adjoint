"""CST template for a continuously optimized, single-feed patch antenna."""
from __future__ import annotations

from eaopt.solver.cst_model import (
    DESIGN_COMPONENT, MESH_CREATOR, UNITS_BLOCK, block_header, brick,
    delete_component, field_monitor, frequency_range, material_normal,
    polygon_extrude, set_boundaries, time_domain_solver_setup,
)
from eaopt.solver.cst_setup import ANTENNA
from eaopt.geometry.contour import MetalRegion

__all__ = [
    "template_blocks", "model_blocks", "setting_blocks", "block_header",
    "UNITS_BLOCK", "DESIGN_COMPONENT", "design_region_update", "layout_view",
    "fixed_metal_polygons", "BOARD", "PATCH", "FEED_X", "FEED_Y",
    "SUB_H", "METAL_T", "FREQ", "FMIN", "FMAX", "AIR_H",
    "PIN_DIAMETER", "GROUND_CLEARANCE_DIAMETER",
    "SUPPORTS_NESTED_CONTOURS", "DESIGN_BOUNDARY_PAD_MM",
]

BOARD = (-30.0, 30.0)
FEED_X, FEED_Y = -15.0, 0.0
PATCH_LENGTH = 20.0       # l_p1 / 2 from Table I
PATCH_WIDTH = 20.0        # l_w1 / 2 from Table I
FEED_EDGE_OFFSET = 5.0    # l_1 from Table I; feed is measured from patch's left edge
PATCH = (FEED_X - FEED_EDGE_OFFSET,
         FEED_X - FEED_EDGE_OFFSET + PATCH_LENGTH)
PATCH_Y = (-PATCH_WIDTH / 2.0, PATCH_WIDTH / 2.0)
PIN_DIAMETER = 1.27       # d_1 from Table I
GROUND_CLEARANCE_DIAMETER = 4.10  # d_2 from Table I
SUB_H = ANTENNA.substrate_h_mm
METAL_T = ANTENNA.metal_thickness_mm
FREQ = ANTENNA.frequency_ghz
FMIN, FMAX = 6.0, 9.0
AIR_H = 10.0
SUBSTRATE = "Rogers4003C"
GROUND_Z = -SUB_H - METAL_T
SUPPORTS_NESTED_CONTOURS = True
DESIGN_BOUNDARY_PAD_MM = 0.0


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


def _discrete_port() -> str:
    # Excite radially across the annular clearance at the ground/substrate
    # interface: both endpoints sit on conductor edges, with the port line
    # spanning only the air gap between the probe and the ground plane.
    probe_edge_x = FEED_X + PIN_DIAMETER / 2.0
    ground_edge_x = FEED_X + GROUND_CLEARANCE_DIAMETER / 2.0
    return (
        "With DiscretePort\n"
        "    .Reset\n"
        '    .PortNumber "1"\n'
        '    .Type "SParameter"\n'
        '    .Impedance "50"\n'
        '    .Monitor "True"\n'
        '    .Radius "0.0"\n'
        f'    .SetP1 "False", "{probe_edge_x:g}", "{FEED_Y:g}", "{-SUB_H:g}"\n'
        f'    .SetP2 "False", "{ground_edge_x:g}", "{FEED_Y:g}", "{-SUB_H:g}"\n'
        "    .Create\n"
        "End With\n"
    )


def model_blocks() -> list[str]:
    """Build the substrate, slotted ground, patch, probe pin, and feed port."""
    x0, x1 = BOARD
    y0, y1 = BOARD
    px0, px1 = PATCH
    py0, py1 = PATCH_Y
    clearance_radius = GROUND_CLEARANCE_DIAMETER / 2.0
    ground_cut_overrun = 0.01
    substrate_cut_overrun = 0.01
    return [
        material_normal(SUBSTRATE, ANTENNA.eps_r, 1.0, ANTENNA.loss_tangent),
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
            "patch_initial", DESIGN_COMPONENT, "PEC",
            [(px0, py0), (px1, py0), (px1, py1), (px0, py1)], METAL_T),
        _cylinder("feed_probe", "component1", "Copper (annealed)",
                  FEED_X, FEED_Y, PIN_DIAMETER / 2.0,
                  -SUB_H, METAL_T),
        _discrete_port(),
    ]


def _cylinder(name: str, component: str, material: str,
              x: float, y: float, radius: float,
              z0: float, z1: float) -> str:
    """CST z-axis cylinder; coordinates and radii are in millimetres."""
    return (
        "With Cylinder\n"
        "    .Reset\n"
        f'    .Name "{name}"\n'
        f'    .Component "{component}"\n'
        f'    .Material "{material}"\n'
        '    .Axis "z"\n'
        f'    .OuterRadius "{radius:.6g}"\n'
        '    .InnerRadius "0"\n'
        f'    .Xcenter "{x:.6g}"\n'
        f'    .Ycenter "{y:.6g}"\n'
        f'    .Zrange "{z0:.6g}", "{z1:.6g}"\n'
        '    .Segments "0"\n'
        "    .Create\n"
        "End With\n"
    )


def setting_blocks(portnum: int) -> list[tuple[str, str]]:
    if int(portnum) != 1:
        raise ValueError(f"antenna template only has discrete port 1, got {portnum}")
    return [
        ("Mesh", MESH_CREATOR),
        ("Solver", time_domain_solver_setup("1")),
        ("FrequencyRange", frequency_range(FMIN, FMAX)),
        ("Monitor Efield", field_monitor("Efield", FREQ)),
        ("Monitor Hfield", field_monitor("Hfield", FREQ)),
        ("Boundary", set_boundaries(
            "expanded open", "expanded open",
            "expanded open", "expanded open",
            "expanded open", "expanded open")),
    ]


def template_blocks(project: str, portnum: int) -> list[tuple[str, str]]:
    blocks = [("Units", UNITS_BLOCK)]
    blocks += [(block_header(command), command) for command in model_blocks()]
    blocks += setting_blocks(portnum)
    return blocks


def initial_patch_polygon():
    px0, px1 = PATCH
    py0, py1 = PATCH_Y
    return [(px0, py0), (px1, py0), (px1, py1), (px0, py1)]


def fixed_metal_polygons() -> list[list[tuple[float, float]]]:
    return []


def layout_view() -> dict:
    x0, x1 = BOARD
    y0, y1 = BOARD
    return {
        "bounds": (x0 - 10.0, x1 + 10.0, y0 - 10.0, y1 + 10.0),
        "shapes": [
            ("substrate", [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
             "Rogers 4003C"),
            ("metal", initial_patch_polygon(), "20 × 20 mm initial design patch"),
        ],
        "ports": [(1, FEED_X, FEED_Y, 0, 1)],
    }
