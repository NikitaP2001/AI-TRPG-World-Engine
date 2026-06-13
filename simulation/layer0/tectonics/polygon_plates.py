"""Polygon-based plate tectonics — Euler rotation on sphere.

Each tectonic plate is a Shapely Polygon on lat/lon.
Plate motion = Euler rotation of ALL polygon vertices.

Usage:
    polygons = build_plate_polygons(h3_ids, assignment)
    rotated = euler_rotate_polygon(poly, omega, dt_myr)
    pid = assign_cell_to_plate(lat, lon, plate_polygons)

No Voronoi — cells are assigned by point-in-polygon query.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
from shapely.geometry import MultiPoint, Polygon as ShapelyPolygon
from shapely import concave_hull


# ── Euler rotation helpers ─────────────────────────────────────────

def rotate_vector(v: Tuple[float, float, float],
                   axis: Tuple[float, float, float],
                   angle_rad: float) -> Tuple[float, float, float]:
    """Rotate 3D vector v around unit axis by angle_rad (Rodrigues).

    https://en.wikipedia.org/wiki/Rodrigues%27_rotation_formula
    """
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    dot = v[0] * axis[0] + v[1] * axis[1] + v[2] * axis[2]
    cross = (
        axis[1] * v[2] - axis[2] * v[1],
        axis[2] * v[0] - axis[0] * v[2],
        axis[0] * v[1] - axis[1] * v[0],
    )
    return (
        v[0] * cos_a + cross[0] * sin_a + axis[0] * dot * (1 - cos_a),
        v[1] * cos_a + cross[1] * sin_a + axis[1] * dot * (1 - cos_a),
        v[2] * cos_a + cross[2] * sin_a + axis[2] * dot * (1 - cos_a),
    )


def latlon_to_cartesian(lat_deg: float, lon_deg: float) -> Tuple[float, float, float]:
    """Convert lat/lon degrees to 3D unit vector."""
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    return (
        math.cos(lat_r) * math.cos(lon_r),
        math.sin(lat_r),
        math.cos(lat_r) * math.sin(lon_r),
    )


def cartesian_to_latlon(x: float, y: float, z: float) -> Tuple[float, float]:
    """Convert 3D unit vector to (lat_deg, lon_deg)."""
    lat = math.degrees(math.asin(max(-1.0, min(1.0, y))))
    lon = math.degrees(math.atan2(z, x))
    return lat, lon


def euler_rotate_point(
    lat_deg: float, lon_deg: float,
    omega_x: float, omega_y: float, omega_z: float,
    dt_myr: float,
    speed_deg_per_myr: float = 0.5,
) -> Tuple[float, float]:
    """Rotate a lat/lon point by Euler vector for dt_myr.

    Euler vector omega = (omega_x, omega_y, omega_z) is unit axis.
    Speed: ~5 cm/yr ≈ 0.5 deg/Myr.

    Returns (new_lat, new_lon) in degrees.
    """
    angle_rad = math.radians(speed_deg_per_myr * dt_myr)
    axis = (omega_x, omega_y, omega_z)
    x, y, z = latlon_to_cartesian(lat_deg, lon_deg)
    nx, ny, nz = rotate_vector((x, y, z), axis, angle_rad)
    return cartesian_to_latlon(nx, ny, nz)


def euler_rotate_polygon(
    polygon: ShapelyPolygon,
    omega_x: float, omega_y: float, omega_z: float,
    dt_myr: float,
    speed_deg_per_myr: float = 0.5,
) -> ShapelyPolygon:
    """Rotate every vertex of a polygon by Euler vector.

    Returns new ShapelyPolygon with rotated exterior + holes.
    """
    def _rotate_coords(coords):
        return [
            euler_rotate_point(lat, lon, omega_x, omega_y, omega_z,
                               dt_myr, speed_deg_per_myr)
            for lon, lat in coords  # Shapely uses (x=lon, y=lat)
        ]

    exterior = _rotate_coords(polygon.exterior.coords)
    holes = [_rotate_coords(h.coords) for h in polygon.interiors]

    # Convert to Shapely's (lon, lat) convention
    ext_ll = [(lon, lat) for lat, lon in exterior]
    holes_ll = [[(lon, lat) for lat, lon in h] for h in holes]

    return ShapelyPolygon(ext_ll, holes_ll)


# ── Polygon construction from cell assignments ─────────────────────

def build_plate_polygons(
    h3_ids: List[str],
    assignment: Dict[str, int],
    concavity: float = 0.02,
) -> Dict[int, ShapelyPolygon]:
    """Build a Shapely Polygon for each plate from its cells' centroids.

    Uses concave_hull to produce natural-looking plate boundaries
    that follow cell cluster shapes.

    Args:
        h3_ids: All cell IDs.
        assignment: h3_id -> plate_id.
        concavity: Concavity parameter for concave_hull (0=convex, 1=concave).

    Returns:
        Dict[plate_id] -> ShapelyPolygon in (lon, lat) convention.
    """
    import h3 as _h3

    # Group cells by plate
    plate_cells: Dict[int, List[str]] = {}
    for hid in h3_ids:
        pid = assignment.get(hid, -1)
        if pid < 0:
            continue
        plate_cells.setdefault(pid, []).append(hid)

    # Build MultiPoint per plate → concave_hull
    polygons: Dict[int, ShapelyPolygon] = {}
    for pid, cells in plate_cells.items():
        if len(cells) < 3:
            continue  # too few cells for a polygon
        points = []
        for hid in cells:
            latlng = _h3.cell_to_latlng(hid)
            points.append((latlng[1], latlng[0]))  # (lon, lat) for Shapely
        mp = MultiPoint(points)
        hull = concave_hull(mp, ratio=concavity)
        if hull is None or hull.is_empty:
            # Fall back to convex hull
            hull = mp.convex_hull
        polygons[pid] = hull

    return polygons


# ── Cell assignment via point-in-polygon ───────────────────────────

def assign_cells_via_polygons(
    h3_ids: List[str],
    plate_polygons: Dict[int, ShapelyPolygon],
) -> Dict[str, int]:
    """Assign each cell to the plate whose polygon contains its centroid.

    Uses spatial index (STRtree) for O(log N) queries.

    Args:
        h3_ids: All cell IDs.
        plate_polygons: Dict[plate_id] -> ShapelyPolygon.

    Returns:
        Dict[h3_id] -> plate_id. Cells not in any polygon get nearest plate.
    """
    import h3 as _h3
    from shapely.prepared import prep as _prep

    # Prepare polygons for fast contains check
    prep_polys = {pid: _prep(poly) for pid, poly in plate_polygons.items()}
    poly_list = list(plate_polygons.values())
    pid_list = list(plate_polygons.keys())

    # Build R-tree for nearest-polygon fallback
    from shapely import STRtree as _STRtree
    tree = _STRtree(poly_list)

    assignment: Dict[str, int] = {}
    for hid in h3_ids:
        latlng = _h3.cell_to_latlng(hid)
        lon, lat = latlng[1], latlng[0]
        pt = ShapelyPolygon([(lon - 0.001, lat - 0.001),
                              (lon + 0.001, lat - 0.001),
                              (lon + 0.001, lat + 0.001),
                              (lon - 0.001, lat + 0.001)])

        found = False
        for pid, prep in prep_polys.items():
            if prep.contains(pt):
                assignment[hid] = pid
                found = True
                break
        if not found:
            # Nearest polygon
            nearest = tree.nearest(
                ShapelyPolygon([(lon, lat), (lon + 0.001, lat),
                                 (lon + 0.001, lat + 0.001), (lon, lat + 0.001)])
            )
            if isinstance(nearest, ShapelyPolygon):
                idx = poly_list.index(nearest)
                assignment[hid] = pid_list[idx]
            else:
                assignment[hid] = 0  # fallback

    return assignment


# ── Boundary detection from polygons ───────────────────────────────

def detect_boundaries_from_polygons(
    h3_ids: List[str],
    assignment: Dict[str, int],
    plate_polygons: Dict[int, ShapelyPolygon],
    omega_vectors: Dict[int, Tuple[float, float, float]],
) -> Tuple[Dict[str, str], Dict[str, float], Dict[str, Tuple[int, int]], Dict[str, float]]:
    """Detect plate boundaries from neighbour differences.

    Replaces the Voronoi-based detect_boundaries.

    Returns:
        boundary_type: h3_id -> convergent|divergent|transform|intraplate
        distance_to_boundary: h3_id -> cells to nearest boundary
        boundary_plate_ids: h3_id -> (plate_a, plate_b)
        convergence_velocity: h3_id -> 0-1 relative speed
    """
    import h3 as _h3

    boundary_type: Dict[str, str] = {}
    distance_to_boundary: Dict[str, float] = {}
    boundary_plate_ids: Dict[str, Tuple[int, int]] = {}
    convergence_velocity: Dict[str, float] = {}

    # Pass 1: find boundary cells
    is_boundary: Dict[str, bool] = {}
    for hid in h3_ids:
        pid = assignment.get(hid, -1)
        try:
            ring = _h3.grid_ring(hid, k=1)
        except Exception:
            ring = []
        neighbours = [n for n in ring if n in h3_ids]
        nid = None
        for n in neighbours:
            np_id = assignment.get(n, -1)
            if np_id != pid and np_id >= 0:
                nid = np_id
                break
        if nid is not None:
            is_boundary[hid] = True
            boundary_plate_ids[hid] = (pid, nid)

            # Classify from Euler vectors
            omega_a = omega_vectors.get(pid, (0, 0, 0))
            omega_b = omega_vectors.get(nid, (0, 0, 0))
            dot = (omega_a[0] * omega_b[0] +
                   omega_a[1] * omega_b[1] +
                   omega_a[2] * omega_b[2])
            dot = max(-1.0, min(1.0, dot))

            if dot < -0.3:
                boundary_type[hid] = "convergent"
                convergence_velocity[hid] = (1.0 + dot) / 2.0
            elif dot > 0.3:
                boundary_type[hid] = "divergent"
                convergence_velocity[hid] = (1.0 - dot) / 2.0
            else:
                boundary_type[hid] = "transform"
                convergence_velocity[hid] = 0.5
        else:
            is_boundary[hid] = False
            boundary_type[hid] = "intraplate"
            convergence_velocity[hid] = 0.0

    # Pass 2: BFS for distance to boundary
    from collections import deque
    dist: Dict[str, float] = {}
    q = deque()
    for hid in h3_ids:
        if is_boundary.get(hid, False):
            dist[hid] = 0.0
            q.append(hid)
    while q:
        hid = q.popleft()
        try:
            ring = _h3.grid_ring(hid, k=1)
        except Exception:
            ring = []
        for n in ring:
            if n in h3_ids and n not in dist:
                dist[n] = dist[hid] + 1.0
                q.append(n)
    for hid in h3_ids:
        distance_to_boundary[hid] = dist.get(hid, 999.0)

    return boundary_type, distance_to_boundary, boundary_plate_ids, convergence_velocity
