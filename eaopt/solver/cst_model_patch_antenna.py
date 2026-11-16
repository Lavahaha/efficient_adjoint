"""7.5 GHz inset-fed rectangular patch; dimensions follow the supplied drawing.

The substrate and patch are centered at (0, 0); z=0 is the substrate top.
Only the feed below y=-20 mm is fixed, outside the 40 x 40 mm design box.
"""
from __future__ import annotations

from eaopt.solver.cst_model import (
    DESIGN_COMPONENT, MESH_CREATOR, UNITS_BLOCK, block_header, brick,
    field_monitor, frequency_range, material_normal, polygon_extrude,
    set_boundaries, solver_setup, waveguide_port,
)
from eaopt.solver.cst_model_antenna import audit_design_regions, design_region_update
from eaopt.solver.cst_setup import PATCH_ANTENNA

BOARD = (-30.0, 30.0)
DESIGN_Y0 = -20.0
PATCH_WIDTH, PATCH_LENGTH = 13.25, 9.93
FEED_WIDTH, INSET_DEPTH, INSET_GAP = 3.38, 3.59, 0.50
SUB_H = PATCH_ANTENNA.substrate_h_mm
METAL_T = PATCH_ANTENNA.metal_thickness_mm
FREQ = PATCH_ANTENNA.frequency_ghz
FMIN, FMAX = 6.0, 9.0
AIR_H = 10.0
SUBSTRATE = "Rogers4003C"
GROUND_Z = -SUB_H - METAL_T
FEED_END_Y = -PATCH_LENGTH / 2 + INSET_DEPTH
FEED_LENGTH = FEED_END_Y - BOARD[0]  # 28.625 mm, rounded to 28.63 in the drawing.
SUPPORTS_NESTED_CONTOURS = True
DESIGN_BOUNDARY_PAD_MM = 0.0


def initial_patch_polygon():
    """One connected polygon: patch, two open inset slots, and in-box feed."""
    w, h = PATCH_WIDTH / 2, PATCH_LENGTH / 2
    f, s = FEED_WIDTH / 2, FEED_WIDTH / 2 + INSET_GAP
    return [
        (-f, DESIGN_Y0), (f, DESIGN_Y0), (f, FEED_END_Y),
        (s, FEED_END_Y), (s, -h), (w, -h), (w, h), (-w, h),
        (-w, -h), (-s, -h), (-s, FEED_END_Y), (-f, FEED_END_Y),
    ]


def fixed_metal_polygons():
    f = FEED_WIDTH / 2
    return [[(-f, BOARD[0]), (f, BOARD[0]), (f, DESIGN_Y0), (-f, DESIGN_Y0)]]


def _feed_port():
    # Keep the reference plane at the board edge with expanded-open boundaries.
    half_width = FEED_WIDTH / 2 + 3 * SUB_H
    return waveguide_port(1, "microstrip_feed", "ymin", BOARD[0],
                          -half_width, half_width, GROUND_Z, 5 * SUB_H,
                          port_on_bound=False)


def model_blocks():
    a, b = BOARD
    return [
        material_normal(SUBSTRATE, PATCH_ANTENNA.eps_r, 1.0, PATCH_ANTENNA.loss_tangent),
        brick("substrate", "component1", SUBSTRATE, a, b, a, b, -SUB_H, 0.0),
        brick("ground", "component1", "PEC", a, b, a, b, GROUND_Z, -SUB_H),
        brick("air", "component1", "Vacuum", a, b, a, b, METAL_T, AIR_H),
        polygon_extrude("feed_fixed", "component1", "PEC", fixed_metal_polygons()[0], METAL_T),
        polygon_extrude("patch_initial", DESIGN_COMPONENT, "PEC", initial_patch_polygon(), METAL_T),
        _feed_port(),
    ]


def setting_blocks(portnum, solver_type="td"):
    if int(portnum) != 1:
        raise ValueError(f"patch_antenna only has waveguide port 1, got {portnum}")
    return [
        ("Mesh", MESH_CREATOR),
        ("Solver", solver_setup(portnum, FREQ, solver_type)),
        ("FrequencyRange", frequency_range(FMIN, FMAX)),
        ("Monitor Efield", field_monitor("Efield", FREQ)),
        ("Monitor Hfield", field_monitor("Hfield", FREQ)),
        ("Boundary", set_boundaries(*(["expanded open"] * 6))),
    ]


def template_blocks(project, portnum, solver_type="td"):
    return ([("Units", UNITS_BLOCK)]
            + [(block_header(c), c) for c in model_blocks()]
            + setting_blocks(portnum, solver_type))


def layout_view():
    a, b = BOARD
    return {
        "bounds": (a - 5, b + 5, a - 12, b + 5),
        "shapes": [
            ("substrate", [(a, a), (b, a), (b, b), (a, b)], "RO4003C 60 x 60 mm"),
            ("metal", fixed_metal_polygons()[0], "Fixed microstrip feed"),
            ("metal", initial_patch_polygon(), "Inset-fed patch 13.25 x 9.93 mm"),
        ],
        "ports": [(1, 0.0, a, 0, 1)],
    }
