"""Join evolving metal inside the design box to unchanged initial metal outside."""
from __future__ import annotations

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union
from skimage import measure

from eaopt.geometry.contour import (
    MetalRegion, SIMPLIFY_MAX_POINTS, SIMPLIFY_TOL_CELLS, simplify_polygon,
)


def polygon_union(polygons):
    """Initial polygons describe solid metal, including overlapping pieces."""
    shapes = [Polygon(np.asarray(p, dtype=float)) for p in polygons]
    if any(not p.is_valid or p.area <= 0 for p in shapes):
        raise ValueError("初始金属多边形必须有效、无自交且面积为正")
    return unary_union(shapes)


def design_box(cfg):
    b = cfg.design_region.box
    return box(b.x[0], b.y[0], b.x[1], b.y[1])


def region_rings(regions):
    return [ring for region in regions for ring in (region.outer, *region.holes)]


def _regions(geometry):
    if geometry.is_empty:
        return []
    if not geometry.is_valid:
        raise ValueError("区内外金属拼合后几何无效")
    polygons = [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)
    regions = []
    for polygon in polygons:
        if polygon.geom_type != "Polygon" or polygon.area <= 1e-12:
            continue
        # Remove collinear vertices without moving any frozen outside geometry.
        polygon = polygon.simplify(0.0, preserve_topology=True)
        regions.append(MetalRegion(
            np.asarray(polygon.exterior.coords),
            tuple(np.asarray(hole.coords) for hole in polygon.interiors)))
    return regions


def compose_metal_regions(cfg, phi):
    """Return (initial metal minus D) union (negative level-set faces inside D).

    Only phi inside D is stored. Polygonizing its zero contours together with
    the box boundary handles corners, open contours, holes and a full/empty D.
    """
    from eaopt.geometry.levelset import make_level_set

    initial_ls = make_level_set(cfg)
    phi = np.asarray(phi, dtype=float)
    if phi.shape != initial_ls.phi.shape or not np.all(np.isfinite(phi)):
        raise ValueError("水平集必须有限且与 design_region 网格尺寸一致")
    initial = polygon_union([p.vertices for p in cfg.initial_metal])
    domain = design_box(cfg)
    if np.array_equal(phi, initial_ls.phi):
        # Iteration zero must use the input polygons, without rasterization loss.
        regions = _regions(initial)
    else:
        outside = initial.difference(domain)
        contours = []
        for contour in measure.find_contours(phi, 0.0):
            points = np.column_stack((
                initial_ls.xs[0] + contour[:, 0] * initial_ls.dx,
                initial_ls.ys[0] + contour[:, 1] * initial_ls.dx))
            for axis, limits in enumerate((initial_ls.box.x, initial_ls.box.y)):
                for limit in limits:
                    points[np.isclose(points[:, axis], limit, rtol=0., atol=1e-9), axis] = limit
            contours.append(points)
        interpolate = RegularGridInterpolator((initial_ls.xs, initial_ls.ys), phi)
        tol = SIMPLIFY_TOL_CELLS * initial_ls.dx
        for _ in range(9):
            lines = [domain.boundary]
            for contour in contours:
                points = simplify_polygon(contour, tol, max_points=None)
                if np.array_equal(contour[0], contour[-1]) and (
                        len(points) < 4 or not Polygon(points).is_valid or Polygon(points).area <= 1e-12):
                    points = contour
                if len(points) >= 2:
                    lines.append(LineString(points))
            faces = []
            for face in polygonize(unary_union(lines)):
                point = face.representative_point()
                if float(interpolate([[point.x, point.y]])[0]) < 0.0:
                    faces.append(face)
            geometry = outside.union(unary_union(faces).intersection(domain))
            regions = _regions(geometry)
            if all(len(ring) <= SIMPLIFY_MAX_POINTS for ring in region_rings(regions)):
                break
            tol *= 2.0
    if any(len(ring) > SIMPLIFY_MAX_POINTS for ring in region_rings(regions)):
        raise ValueError("金属轮廓超过 CST 点数上限；区外固定几何不能自动简化移动")
    return regions


def connection_boundary_mask(ls, cfg, fixed_polygons):
    """Pin initial metal where the design boundary contacts frozen metal/feeds."""
    initial = polygon_union([p.vertices for p in cfg.initial_metal])
    domain = design_box(cfg)
    neighbors = initial.difference(domain).union(polygon_union(fixed_polygons))
    contact = neighbors.intersection(domain.boundary)
    mask = np.zeros(ls.phi.shape, dtype=bool)
    boundary = np.zeros_like(mask)
    boundary[[0, -1], :] = True
    boundary[:, [0, -1]] = True
    for i, j in np.argwhere(boundary & (ls.phi <= 1e-9)):
        # Protect sub-cell contact endpoints as well as nodes exactly on the feed.
        if contact.distance(Point(ls.xs[i], ls.ys[j])) <= .5 * ls.dx + 1e-9:
            mask[i, j] = True
    return mask
