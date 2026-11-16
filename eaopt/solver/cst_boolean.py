"""Plan planar PEC unions while retaining fixed-feed names and components."""
from shapely.geometry import Polygon
from shapely.ops import unary_union


def plan_pec_unions(design_solids, fixed_solids):
    """Return CST Add commands and surviving (name, polygon) pairs.

    Fixed solids come first, so a connected group keeps its first fixed name.
    Point contacts and gaps are not bridged or enlarged.
    """
    solids = [(name, Polygon(points)) for name, points in fixed_solids]
    solids += list(design_solids)
    if any(not p.is_valid or p.is_empty or p.area <= 0 for _, p in solids):
        raise ValueError("PEC union requires valid nonempty polygons")
    commands = []
    survivors = []
    while solids:
        name, polygon = solids.pop(0)
        changed = True
        while changed:
            changed = False
            for i, (other_name, other) in enumerate(solids):
                if not polygon.intersects(other):
                    continue
                combined = polygon.union(other)
                if combined.geom_type != "Polygon":
                    continue
                commands.append(
                    f'Solid.Add "{name}", "{other_name}"\n'
                    f'If Not Solid.DoesExist("{name}") Then Err.Raise 1, , "PEC union target missing"\n'
                    f'If Solid.DoesExist("{other_name}") Then Err.Raise 1, , "PEC union source remains"\n')
                polygon = combined
                solids.pop(i)
                changed = True
                break
        survivors.append((name, polygon))
    return commands, survivors


def restore_fixed_metal(fixed_solids, height_mm, z0=0.0):
    """Remove last round's unions and recreate only the named fixed feeds."""
    from eaopt.solver.cst_model import polygon_extrude

    commands = []
    for full_name, points in fixed_solids:
        component, name = full_name.rsplit(":", 1)
        commands.append(f'If Solid.DoesExist("{full_name}") Then\n'
                        f'    Solid.Delete "{full_name}"\nEnd If\n')
        commands.append(polygon_extrude(name, component, "PEC", points, height_mm, z0))
    return "".join(commands)


def merged_design_solids(regions, fixed_solids, component="design_region"):
    """Expected native solids containing design metal after antenna updates."""
    design = [(f"{component}:antenna_metal_{i}", Polygon(r.outer, r.holes))
              for i, r in enumerate(regions)]
    _, survivors = plan_pec_unions(design, fixed_solids)
    metal = unary_union([p for _, p in design])
    return [(name, p) for name, p in survivors if p.intersection(metal).area > 0]
