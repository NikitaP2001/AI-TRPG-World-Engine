"""PlanetScene — 3D H3 cell planet rendered with OpenGL.

Loads cells + features from SQLite database (no parquet/JSON fallback).
Renders: elevation, biomes, lakes, rivers, wetlands, ore deposits,
springs, mountains, soil regions, temperature bands.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

# Add project root for imports
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import moderngl as mgl
from PIL import Image, ImageDraw, ImageFont

from opengl_app.scene import Scene
from simulation.layer0.cell_model import CellData
from simulation.layer0.climate import koppen_name
from simulation.layer0.geology import geology_name


# ── Contour colour palette (same as renderer.py) ──────────────────
_CONTOUR_FLAT = (0.275, 0.529, 0.235)    # el 0.0-0.2
_CONTOUR_LOW = (0.471, 0.608, 0.333)     # el 0.2-0.4
_CONTOUR_MID = (0.725, 0.647, 0.431)     # el 0.4-0.55
_CONTOUR_HIGH = (0.647, 0.510, 0.314)    # el 0.55-0.70
_CONTOUR_PEAK = (0.529, 0.373, 0.216)    # el 0.70-0.85
_CONTOUR_SNOW = (0.804, 0.784, 0.784)    # el > 0.85

_OCEAN_DEEP = (0.071, 0.125, 0.373)
_OCEAN_MID = (0.118, 0.216, 0.471)
_OCEAN_SHALLOW = (0.176, 0.314, 0.588)

_RIVER_COL = (0.157, 0.549, 0.902)


# ── Soil fertility colour palette ──────────────────────────────
_SOIL_BARREN = (0.6, 0.5, 0.4)      # Soil ≈ 0.00-0.05 (barren/ocean)
_SOIL_POOR = (0.7, 0.65, 0.5)       # Soil ≈ 0.05-0.15
_SOIL_MODERATE = (0.6, 0.7, 0.4)    # Soil ≈ 0.15-0.30
_SOIL_GOOD = (0.4, 0.65, 0.3)       # Soil ≈ 0.30-0.50
_SOIL_RICH = (0.2, 0.5, 0.15)       # Soil > 0.50

# ── Vegetation colour palette ──────────────────────────────────
_VEG_OCEAN = (0.07, 0.13, 0.37)
_VEG_BARREN = (0.5, 0.45, 0.35)
_VEG_DESERT = (0.8, 0.7, 0.45)
_VEG_TUNDRA = (0.55, 0.6, 0.65)
_VEG_GRASSLAND = (0.6, 0.75, 0.4)
_VEG_SHRUBLAND = (0.65, 0.6, 0.35)
_VEG_SAVANNA = (0.7, 0.65, 0.3)
_VEG_FOREST = (0.2, 0.55, 0.2)
_VEG_RAINFOREST = (0.1, 0.4, 0.1)
_VEG_TAIGA = (0.25, 0.45, 0.3)

_VEG_MAP = {
    "barren": _VEG_BARREN,
    "desert": _VEG_DESERT,
    "tundra": _VEG_TUNDRA,
    "grassland": _VEG_GRASSLAND,
    "shrubland": _VEG_SHRUBLAND,
    "savanna": _VEG_SAVANNA,
    "forest": _VEG_FOREST,
    "rainforest": _VEG_RAINFOREST,
    "taiga": _VEG_TAIGA,
}

# ── Geology colour palette ─────────────────────────────────────
_GEO_COLORS = {
    0: (0.1, 0.15, 0.4),    # oceanic
    1: (0.6, 0.55, 0.45),   # shelf
    2: (0.5, 0.6, 0.35),    # continental
    3: (0.55, 0.35, 0.2),   # mountain belt
    4: (0.4, 0.3, 0.5),     # rift valley
    5: (0.7, 0.5, 0.3),     # craton
    6: (0.5, 0.3, 0.3),     # fault zone
}


def _soil_color(fertility: float) -> Tuple[float, float, float]:
    """Map soil fertility 0-1 to colour."""
    if fertility < 0.03:
        return _SOIL_BARREN
    elif fertility < 0.15:
        return _SOIL_POOR
    elif fertility < 0.30:
        return _SOIL_MODERATE
    elif fertility < 0.50:
        return _SOIL_GOOD
    else:
        return _SOIL_RICH


def _veg_color(veg_type: str, is_ocean: bool) -> Tuple[float, float, float]:
    """Map vegetation type to colour."""
    if is_ocean:
        return _VEG_OCEAN
    return _VEG_MAP.get(veg_type, _VEG_GRASSLAND)


def _geo_color(gtype: int) -> Tuple[float, float, float]:
    """Map geological type to colour."""
    return _GEO_COLORS.get(gtype, (0.5, 0.5, 0.5))


def _runoff_color(runoff: float) -> Tuple[float, float, float]:
    """Map runoff ratio 0-1 to colour (blue = wet, yellow = dry)."""
    r = max(0.0, min(1.0, runoff))
    return (1.0 - r * 0.8, 1.0 - r * 0.5, 0.3 + r * 0.6)


# ── Star shaders ────────────────────────────────────────────────
STAR_VERTEX_SHADER = """
#version 330

uniform mat4 uViewProj;

in vec3 in_position;
in vec2 in_uv;
in float in_brightness;

out vec2 vUv;
out float vBrightness;

void main() {
    gl_Position = uViewProj * vec4(in_position, 1.0);
    // Pin to far plane so stars look infinitely distant
    gl_Position.z = gl_Position.w * 0.9999;
    vUv = in_uv;
    vBrightness = in_brightness;
}
"""

STAR_FRAGMENT_SHADER = """
#version 330

in vec2 vUv;
in float vBrightness;

out vec4 fragColor;

void main() {
    // Circular glow from quad center
    vec2 center = vUv - vec2(0.5);
    float d = length(center) * 2.0;
    if (d > 1.0) discard;
    // Gaussian glow with bright core
    float glow = exp(-d * d * 6.0);
    float core = exp(-d * d * 40.0);
    float brightness = max(glow * 0.6, core) * vBrightness;
    // Slight color warmth variation
    vec3 col = mix(vec3(1.0, 0.95, 0.85), vec3(1.0), vBrightness);
    fragColor = vec4(col * brightness, 1.0);
}
"""


# ── Planet vertex shader ────────────────────────────────────────
VERTEX_SHADER = """
#version 330

uniform mat4 uViewProj;
uniform mat4 uModel;
uniform float uRadius;
uniform float uElevScale;

in vec3 in_position;
in vec3 in_color;
in float in_elevation;

out vec3 vColor;
out vec3 vNormal;
out vec3 vWorldPos;
out float vElevation;

void main() {
    vec3 dir = normalize(in_position);
    vec3 pos = (in_position + dir * in_elevation * uElevScale) * uRadius;
    vec4 worldPos = uModel * vec4(pos, 1.0);
    gl_Position = uViewProj * worldPos;
    vColor = in_color;
    vNormal = normalize(mat3(uModel) * in_position);
    vWorldPos = worldPos.xyz;
    vElevation = in_elevation;
}
"""

# ── Fragment shader ──────────────────────────────────────────────
FRAGMENT_SHADER = """
#version 330

uniform vec3 uSunDir;
uniform vec3 uCamPos;
uniform float uAtmosIntensity;
uniform float uDistance;
uniform vec3 uTerrainTint;

in vec3 vColor;
in vec3 vNormal;
in vec3 vWorldPos;
in float vElevation;

out vec4 fragColor;

void main() {
    vec3 N = normalize(vNormal);
    vec3 L = normalize(uSunDir);
    vec3 V = normalize(uCamPos - vWorldPos);

    float diff = max(dot(N, L), 0.0);
    float ambient = 0.20;
    float sunlight = ambient + diff * 0.80;

    // Terrain cover tint (blended at distance)
    float lod = clamp((uDistance - 2.0) / 4.0, 0.0, 1.0);
    vec3 tint = mix(uTerrainTint, vec3(0.0), lod);
    vec3 baseCol = vColor + tint;

    // Atmosphere rim glow
    float rim = 1.0 - max(dot(V, N), 0.0);
    float atmos = pow(rim, 3.0) * uAtmosIntensity;
    vec3 atmosColor = vec3(0.3, 0.5, 1.0);

    // Snow on sun-facing high slopes (fades with distance)
    float snow = smoothstep(0.85, 0.95, vColor.r) * diff * 0.5 * (1.0 - lod * 0.5);

    // Elevation shading: darker valleys, brighter peaks (land only)
    float elevShade = 0.85 + vElevation * 0.25;
    float isLand = 1.0 - step(0.3, vColor.b - vColor.r * 0.5);
    vec3 shaded = mix(vec3(1.0), vec3(elevShade), isLand * 0.3);
    vec3 baseShaded = baseCol * shaded;

    vec3 finalColor = baseShaded * sunlight + atmos * atmosColor + vec3(snow);
    fragColor = vec4(finalColor, 1.0);
}
"""

# ── Border shaders ─────────────────────────────────────────────
BORDER_VERTEX_SHADER = """
#version 330

uniform mat4 uViewProj;
uniform float uRadius;

in vec3 in_position;

void main() {
    vec3 pos = in_position * uRadius;
    gl_Position = uViewProj * vec4(pos, 1.0);
}
"""

BORDER_FRAGMENT_SHADER = """
#version 330

uniform float uDistance;

out vec4 fragColor;

void main() {
    // Only visible when very close (distance < 5)
    float t = max(0.0, 1.0 - (uDistance - 2.0) / 3.0);
    float alpha = 0.4 * t * t;
    if (alpha < 0.005) discard;
    fragColor = vec4(0.0, 0.0, 0.0, alpha);
}
"""

RIVER_FRAGMENT_SHADER = """
#version 330

out vec4 fragColor;

void main() {
    fragColor = vec4(0.2, 0.5, 0.9, 0.6);
}
"""

# ── Selection highlight shaders ────────────────────────────────
SEL_VERTEX_SHADER = """
#version 330
uniform mat4 uViewProj;
uniform float uRadius;
in vec3 in_position;
void main() {
    vec3 pos = in_position * uRadius;
    gl_Position = uViewProj * vec4(pos, 1.0);
}
"""
SEL_FRAGMENT_SHADER = """
#version 330
out vec4 fragColor;
void main() {
    fragColor = vec4(0.0, 0.8, 1.0, 1.0);  // cyan highlight
}
"""

# ── Panel shaders ──────────────────────────────────────────────
PANEL_VERTEX_SHADER = """
#version 330
in vec2 in_position;
in vec2 in_uv;
out vec2 vUv;
void main() {
    gl_Position = vec4(in_position, 0.0, 1.0);
    vUv = in_uv;
}
"""
PANEL_FRAGMENT_SHADER = """
#version 330
uniform sampler2D uTexture;
in vec2 vUv;
out vec4 fragColor;
void main() {
    fragColor = texture(uTexture, vUv);
}
"""


def _latlon_to_3d(lat_deg: float, lon_deg: float, radius: float = 1.0) -> Tuple[float, float, float]:
    """Convert lat/lon degrees to 3D position on sphere."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    x = radius * math.cos(lat) * math.cos(lon)
    y = radius * math.sin(lat)
    z = radius * math.cos(lat) * math.sin(lon)
    return (x, y, z)


def _contour_color(el: float, is_ocean: bool, temp: float) -> Tuple[float, float, float]:
    """Elevation contour colour (normalised 0-1 floats).

    Ocean depth uses negative elevation (el < 0), not temperature.
    """
    if is_ocean:
        depth = min(1.0, max(0.0, -el * 2.0))
        if depth < 0.25:
            c = _OCEAN_SHALLOW
        elif depth < 0.60:
            c = _OCEAN_MID
        else:
            c = _OCEAN_DEEP
        darken = depth * 0.3
        return (max(0.0, c[0] - darken),
                max(0.0, c[1] - darken * 0.8),
                min(1.0, c[2] + darken * 0.5))
    if el > 0.80:
        return _CONTOUR_SNOW
    elif el > 0.60:
        return _CONTOUR_PEAK
    elif el > 0.45:
        return _CONTOUR_HIGH
    elif el > 0.30:
        return _CONTOUR_MID
    elif el > 0.15:
        return _CONTOUR_LOW
    else:
        return _CONTOUR_FLAT


class PlanetScene(Scene):
    """Renders a planet as a smooth subdivided icosahedron surface.

    Hex cell borders are drawn as an overlay for UI reference.
    Cells are used for click detection and info panel only.
    Elevation, terrain, and features are sampled continuously.
    """

    def __init__(self, parquet_path: str):
        self.parquet_path = parquet_path
        self.cells: List[CellData] = []
        # Icosahedron surface (smooth, continuous)
        self._ico_vao = None
        self._ico_vbo = None
        self._ico_ibo = None
        self._ico_num_indices = 0
        self._ico_vertex_count = 0
        self._ico_vertices_base = []
        self._ico_vertex_elevs = []
        self._ico_vertex_colors = []
        self._program = None
        self.radius = 1.5
        # Sun direction
        self.sun_dir = np.array([1.0, 0.8, 0.6], dtype=np.float32)
        self.sun_dir = self.sun_dir / np.linalg.norm(self.sun_dir)
        self.atmos_intensity = 0.4
        # Hex borders overlay
        self._border_vao = None
        self._border_vbo = None
        self._border_ibo = None
        self._border_num_indices = 0
        self._border_program = None
        self.show_borders = True
        # Selected cell highlight
        self.selected_cell_id: str = ""
        self.selected_cell_data: Optional[CellData] = None
        self._sel_border_vao = None
        self._sel_border_vbo = None
        self._sel_border_ibo = None
        self._sel_border_num_indices = 0
        self._sel_border_program = None
        # Info panel
        self._panel_vao = None
        self._panel_vbo = None
        self._panel_texture = None
        self._panel_program = None
        self._panel_dirty = False
        # Feature store
        self._feature_store = None
        # Lakes
        self._lake_vao = None
        self._lake_vbo = None
        self._lake_ibo = None
        self._lake_program = None
        self._lake_indices = 0
        self._ore_vao = None
        self._ore_vbo = None
        self._ore_ibo = None
        self._ore_program = None
        self._ore_count = 0
        # Rivers
        self._river_vao = None
        self._river_vbo = None
        self._river_ibo = None
        self._river_program = None
        self._river_points = 0
        # Panel state
        self._panel_scroll = 0
        self._panel_content_height = 0
        self._expanded_sections = {0}  # section 0 (General) open by default
        self._section_headers = {}  # section idx → (y_top, y_bot) in content coords
        self._panel_texture_pw = 480
        self._panel_redraw = False

        # View mode: 0=elevation, 1=soil, 2=vegetation, 3=geology, 4=runoff
        self._view_mode = 0
        self._view_mode_names = ["Elevation", "Soil Fertility", "Vegetation", "Geology", "Runoff"]

    def name(self) -> str:
        return "Planet"

    def cycle_view_mode(self) -> None:
        """Switch to next view mode — rebuild icosahedron colors."""
        self._view_mode = (self._view_mode + 1) % len(self._view_mode_names)
        if self._ico_vao is not None:
            self._ico_rebuild_colors()
        print(f"[PlanetScene] View mode: {self._view_mode_names[self._view_mode]}")

    def set_view_mode(self, mode: int) -> None:
        """Set specific view mode — rebuild icosahedron colors."""
        if 0 <= mode < len(self._view_mode_names):
            self._view_mode = mode
            if self._ico_vao is not None:
                self._ico_rebuild_colors()
            print(f"[PlanetScene] View mode: {self._view_mode_names[self._view_mode]}")

    def _get_cell_color(self, cell) -> Tuple[float, float, float]:
        """Get cell color for current view mode."""
        if self._view_mode == 0:  # Elevation
            return _contour_color(cell.elevation_mean, cell.geological_type == 0, cell.temperature)
        elif self._view_mode == 1:  # Soil
            return _soil_color(cell.soil_fertility)
        elif self._view_mode == 2:  # Vegetation
            return _veg_color(cell.vegetation_cover, cell.geological_type == 0)
        elif self._view_mode == 3:  # Geology
            return _geo_color(cell.geological_type)
        elif self._view_mode == 4:  # Runoff
            ro = getattr(cell, 'runoff_ratio', 0.5)
            return _runoff_color(ro)
        return _contour_color(cell.elevation_mean, cell.geological_type == 0, cell.temperature)

    def _build_icosahedron_sphere(self, subdivisions: int = 4):
        """Build a subdivided icosahedron sphere mesh.

        Returns (vertices_3d, indices) where vertices_3d is list of (x,y,z)
        on unit sphere. Subdivision 4 → ~2,562 vertices, ~5,120 triangles.
        """
        import math
        phi = (1.0 + math.sqrt(5.0)) / 2.0

        # 12 vertices of icosahedron
        raw = [
            (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
            (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
            (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1),
        ]
        # Normalize to unit sphere
        verts = []
        for v in raw:
            d = math.sqrt(v[0]*v[0] + v[1]*v[1] + v[2]*v[2])
            verts.append((v[0]/d, v[1]/d, v[2]/d))

        # 20 faces
        faces = [
            (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
            (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
            (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
            (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
        ]

        # Subdivide
        for _ in range(subdivisions):
            edge_mid = {}
            new_faces = []
            for a, b, c in faces:
                # Edge midpoints (with dedup)
                ab = self._ico_edge_key(a, b, verts, edge_mid)
                bc = self._ico_edge_key(b, c, verts, edge_mid)
                ca = self._ico_edge_key(c, a, verts, edge_mid)
                # 4 new faces
                new_faces.append((a, ab, ca))
                new_faces.append((b, bc, ab))
                new_faces.append((c, ca, bc))
                new_faces.append((ab, bc, ca))
            faces = new_faces

        # Convert to flat arrays
        result_verts = list(verts)
        # Remap face indices to flat array
        indices = []
        for a, b, c in faces:
            indices.extend([a, b, c])
        return result_verts, indices

    @staticmethod
    def _ico_edge_key(a: int, b: int, verts: list, cache: dict) -> int:
        """Get or create midpoint vertex for edge a-b."""
        key = (a, b) if a < b else (b, a)
        if key in cache:
            return cache[key]
        x1, y1, z1 = verts[a]
        x2, y2, z2 = verts[b]
        mx, my, mz = (x1 + x2) / 2.0, (y1 + y2) / 2.0, (z1 + z2) / 2.0
        d = math.sqrt(mx*mx + my*my + mz*mz)
        verts.append((mx/d, my/d, mz/d))
        idx = len(verts) - 1
        cache[key] = idx
        return idx


    def _ico_rebuild_colors(self) -> None:
        """Rebuild icosahedron vertex colors from feature store + view mode.

        Ocean detection uses geological type (geo_type == 0), NOT
        elevation — rift valleys have negative elevation but are land.
        """
        import h3
        if not self._ico_vertices_base:
            return
        n = len(self._ico_vertices_base)
        verts = []
        cell_geo_map = {}
        if self.cells:
            cell_geo_map = {cell.h3_id: cell.geological_type for cell in self.cells}
        for i in range(n):
            x, y, z = self._ico_vertices_base[i]
            el = self._ico_vertex_elevs[i]
            lat = math.degrees(math.asin(max(-1.0, min(1.0, y))))
            lon = math.degrees(math.atan2(z, x))
            # Ocean from geological type, not elevation
            h = h3.latlng_to_cell(lat, lon, 2)
            geo_type = cell_geo_map.get(h, 2)
            is_ocean = (geo_type == 0)
            col = self._sample_color(lat, lon, el, is_ocean, geo_type)
            self._ico_vertex_colors[i] = col
            verts.append((x, y, z, col[0], col[1], col[2], el))
        arr = np.array(verts, dtype=np.float32)
        if self._ico_vbo:
            self._ico_vbo.write(arr.tobytes())

    def _build_icosahedron_sphere(self, subdivisions: int = 4):
        """Build a subdivided icosahedron sphere mesh.

        Returns (vertices_3d, indices) where vertices_3d is list of (x,y,z)
        on unit sphere. Subdivision 4 → ~2,562 vertices, ~5,120 triangles.
        """
        phi = (1.0 + math.sqrt(5.0)) / 2.0

        raw = [
            (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
            (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
            (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1),
        ]
        verts = []
        for v in raw:
            d = math.sqrt(v[0]*v[0] + v[1]*v[1] + v[2]*v[2])
            verts.append((v[0]/d, v[1]/d, v[2]/d))

        faces = [
            (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
            (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
            (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
            (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
        ]

        for _ in range(subdivisions):
            edge_mid = {}
            new_faces = []
            for a, b, c in faces:
                ab = self._ico_edge_key(a, b, verts, edge_mid)
                bc = self._ico_edge_key(b, c, verts, edge_mid)
                ca = self._ico_edge_key(c, a, verts, edge_mid)
                new_faces.append((a, ab, ca))
                new_faces.append((b, bc, ab))
                new_faces.append((c, ca, bc))
                new_faces.append((ab, bc, ca))
            faces = new_faces

        result_verts = list(verts)
        indices = []
        for a, b, c in faces:
            indices.extend([a, b, c])
        return result_verts, indices

    @staticmethod
    def _ico_edge_key(a: int, b: int, verts: list, cache: dict) -> int:
        key = (a, b) if a < b else (b, a)
        if key in cache:
            return cache[key]
        x1, y1, z1 = verts[a]
        x2, y2, z2 = verts[b]
        mx, my, mz = (x1 + x2) / 2.0, (y1 + y2) / 2.0, (z1 + z2) / 2.0
        d = math.sqrt(mx*mx + my*my + mz*mz)
        verts.append((mx/d, my/d, mz/d))
        idx = len(verts) - 1
        cache[key] = idx
        return idx

    def _sample_color(self, lat: float, lon: float, elev: float,
                      is_ocean: bool, geo_type: int = 2) -> Tuple[float, float, float]:
        """Sample vertex color at any lat/lon from feature store + view mode.

        ALL features are embedded directly in the surface color — no
        separate overlay geometry except for the info panel. Lakes,
        wetlands, biomes, and rivers are all rendered as part of the
        icosahedron vertex colors, not as floating overlays.

        Args:
            lat, lon: vertex position
            elev: vertex elevation
            is_ocean: whether this vertex is ocean (from geological_type)
            geo_type: geological_type of the cell at this vertex (0=oceanic, 2=continental, etc.)
        """
        veg = "barren"
        is_lake = False
        biome_canopy = None
        if self._feature_store is not None:
            features = self._feature_store.at_point(lat, lon)
            for f in features:
                if f.type == "terrain_cover":
                    veg = f.properties.get("cover_type", veg)
                elif f.type == "lake":
                    ff = f.properties.get("fill_fraction", 1)
                    if ff > 0.05:  # skip nearly-empty lakes
                        is_lake = True
                elif f.type == "biome":
                    bk = f.properties.get("biome_key", "")
                    if bk:
                        veg = bk  # Use biome as vegetation type
                        biome_canopy = f.properties.get("canopy", 0)

        # Lake: water color regardless of elevation/view mode
        lake_col = (0.15, 0.40, 0.70)

        if self._view_mode == 0:
            if is_lake:
                return lake_col
            return _contour_color(elev, is_ocean, 0.5)
        elif self._view_mode == 1:
            if is_lake:
                return lake_col
            return _soil_color(max(0.02, elev * 0.5 + 0.1))
        elif self._view_mode == 2:
            if is_lake:
                return lake_col
            return _veg_color(veg, is_ocean)
        elif self._view_mode == 3:
            if not is_ocean and geo_type == 0:
                geo_type = 2
            return _geo_color(geo_type)
        elif self._view_mode == 4:
            if is_lake:
                return lake_col
            return (0.5, 0.5, 0.5)
        return _contour_color(elev, is_ocean, 0.5)

    def _find_cell(self, h3_id: str):
        """Find cell data by H3 ID."""
        for c in getattr(self, 'cells', []) or []:
            if c.h3_id == h3_id:
                return c
        return None

    def _setup_stars(self, ctx) -> None:
        """Generate starfield as glowing quads on a far sphere."""
        import random
        random.seed(42)
        n_stars = 1200
        star_radius = 95.0

        verts = []
        idxs = []
        base = 0
        for _ in range(n_stars):
            # Random position on unit sphere (biased toward sparse regions)
            theta = random.random() * 2.0 * math.pi
            phi = math.acos(2.0 * random.random() - 1.0)
            dx = math.sin(phi) * math.cos(theta)
            dy = math.sin(phi) * math.sin(theta)
            dz = math.cos(phi)
            center = np.array([dx, dy, dz], dtype=np.float32)
            pos = center * star_radius

            # Power-law brightness: most stars are dim, few are bright
            brightness = random.random() ** 3.0 * 0.9 + 0.1
            # Size varies with brightness (brighter = bigger)
            star_size = 0.08 + brightness * 0.5

            # Local tangent basis for the quad
            up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
            if abs(np.dot(center, up)) > 0.99:
                up = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            t1 = np.cross(center, up)
            t1 = t1 / np.linalg.norm(t1)
            t2 = np.cross(center, t1)

            # 4 corners with UV coordinates
            s = star_size
            corners = [(-1,-1,0,0), (1,-1,1,0), (1,1,1,1), (-1,1,0,1)]
            for cx, cy, u, v in corners:
                p = pos + t1 * cx * s + t2 * cy * s
                verts.append((p[0], p[1], p[2], u, v, brightness))
            idxs.extend([base, base+1, base+2, base, base+2, base+3])
            base += 4

        v_arr = np.array(verts, dtype=np.float32)
        i_arr = np.array(idxs, dtype=np.uint32)
        self._star_vbo = ctx.buffer(v_arr.tobytes())
        self._star_ibo = ctx.buffer(i_arr.tobytes())
        self._star_program = ctx.program(
            vertex_shader=STAR_VERTEX_SHADER,
            fragment_shader=STAR_FRAGMENT_SHADER,
        )
        self._star_vao = ctx.vertex_array(
            self._star_program,
            [(self._star_vbo, '3f 2f f', 'in_position', 'in_uv', 'in_brightness')],
            index_buffer=self._star_ibo,
        )
        self._num_stars = len(idxs)

    def _build_rivers(self, ctx) -> None:
        """Build river rendering VAO from feature store LineStrings.
        Skips vertices that are in the ocean (elevation < 0).
        """
        self._river_points = 0
        self._river_vao = None
        self._river_vbo = None
        if self._feature_store is None:
            return
        rivers = self._feature_store.get_features_by_type("river")
        if not rivers:
            return
        verts = []
        idxs = []
        base = 0
        for riv in rivers:
            if riv.geometry is None or riv.geometry.geom_type != "LineString":
                continue
            coords = list(riv.geometry.coords)
            if len(coords) < 2:
                continue
            # Check if river end is in ocean (geology_region type 0)
            # by querying feature store at the last vertex
            last_lon, last_lat = coords[-1]
            is_coastal = False
            if self._feature_store is not None:
                for f in self._feature_store.at_point(last_lat, last_lon):
                    if f.type == "geology_region" and f.properties.get("geological_type", -1) == 0:
                        is_coastal = True
                        break
            if is_coastal:
                coords = coords[:-1]  # drop ocean vertex
                if len(coords) < 2:
                    continue
            for lon, lat in coords:
                pos = _latlon_to_3d(lat, lon)
                verts.append((pos[0], pos[1], pos[2]))
            for i in range(len(coords) - 1):
                idxs.append(base + i)
                idxs.append(base + i + 1)
            base += len(coords)
        if not verts:
            return
        v_arr = np.array(verts, dtype=np.float32)
        i_arr = np.array(idxs, dtype=np.uint32)
        self._river_program = ctx.program(
            vertex_shader=BORDER_VERTEX_SHADER,
            fragment_shader=RIVER_FRAGMENT_SHADER,
        )
        self._river_vbo = ctx.buffer(v_arr.tobytes())
        self._river_ibo = ctx.buffer(i_arr.tobytes())
        self._river_points = len(idxs)
        self._river_vao = ctx.vertex_array(
            self._river_program,
            [(self._river_vbo, '3f', 'in_position')],
            index_buffer=self._river_ibo,
        )

    def _build_ore_markers(self, ctx) -> None:
        """Build ore deposit markers from feature store (colored quads)."""
        self._ore_vao = None
        self._ore_vbo = None
        self._ore_ibo = None
        self._ore_program = None
        self._ore_count = 0
        if self._feature_store is None:
            return
        ores = self._feature_store.get_features_by_type("ore_deposit")
        if not ores:
            return

        ore_colors = {
            "coal": (0.3, 0.3, 0.3), "anthracite": (0.15, 0.15, 0.15),
            "hematite": (0.7, 0.2, 0.2), "magnetite": (0.3, 0.1, 0.1),
            "chalcopyrite": (0.8, 0.7, 0.1), "native_gold": (1.0, 0.8, 0.0),
            "native_silver": (0.8, 0.8, 0.9), "native_platinum": (0.7, 0.7, 0.8),
            "galena": (0.5, 0.5, 0.5), "sphalerite": (0.6, 0.4, 0.2),
            "cassiterite": (0.4, 0.3, 0.2), "uraninite": (0.2, 0.8, 0.2),
            "bauxite": (0.8, 0.4, 0.3), "chromite": (0.3, 0.5, 0.3),
            "ilmenite": (0.5, 0.3, 0.4), "pentlandite": (0.6, 0.7, 0.5),
            "diamond": (0.0, 1.0, 1.0), "calcite": (0.9, 0.9, 0.9),
            "graphite": (0.3, 0.3, 0.3),
        }
        dcolor = (0.5, 0.5, 0.3)

        verts = []
        idxs = []
        base = 0

        for ore in ores:
            if ore.geometry is None or ore.geometry.is_empty:
                continue
            c = ore.geometry.centroid
            if c is None:
                continue
            lon, lat = c.x, c.y
            primary = ore.properties.get("primary_ore", "unknown")
            color = ore_colors.get(primary, dcolor)
            grade = ore.properties.get("grade", 0.5)
            vol = ore.properties.get("volume_m3", 1000)
            ms = min(0.015, 0.003 + 0.003 * min(1.0, grade) + 0.001 * min(1.0, vol / 50000))
            pos = _latlon_to_3d(lat, lon, radius=1.003)
            corners = [
                (pos[0]-ms, pos[1]-ms, pos[2]),
                (pos[0]+ms, pos[1]-ms, pos[2]),
                (pos[0]+ms, pos[1]+ms, pos[2]),
                (pos[0]-ms, pos[1]+ms, pos[2]),
            ]
            for p in corners:
                verts.extend(p)
                verts.extend(color)
            idxs.extend([base, base+1, base+2, base, base+2, base+3])
            base += 4

        if not verts:
            return

        v_arr = np.array(verts, dtype=np.float32)
        i_arr = np.array(idxs, dtype=np.uint32)
        self._ore_count = len(idxs)

        ORE_VS = """#version 330
uniform mat4 uViewProj;
uniform float uRadius;
in vec3 in_position;
in vec3 in_color;
out vec3 vColor;
void main() {
    vec3 pos = in_position * uRadius;
    gl_Position = uViewProj * vec4(pos, 1.0);
    vColor = in_color;
}"""
        ORE_FS = """#version 330
in vec3 vColor;
out vec4 fragColor;
void main() { fragColor = vec4(vColor, 1.0); }"""
        self._ore_program = ctx.program(vertex_shader=ORE_VS, fragment_shader=ORE_FS)
        self._ore_vbo = ctx.buffer(v_arr.tobytes())
        self._ore_ibo = ctx.buffer(i_arr.tobytes())
        self._ore_vao = ctx.vertex_array(
            self._ore_program,
            [(self._ore_vbo, '3f', 'in_position'), (self._ore_vbo, '3f', 'in_color')],
            index_buffer=self._ore_ibo,
        )

    def _build_lakes(self, ctx) -> None:
        """Build lake rendering VAO from feature store polygons.

        Renders lakes as filled translucent polygons slightly above
        the surface to avoid z-fighting with terrain.
        """
        self._lake_vao = None
        self._lake_vbo = None
        self._lake_ibo = None
        self._lake_indices = 0
        if self._feature_store is None:
            return
        lakes = self._feature_store.get_features_by_type("lake")
        if not lakes:
            return
        verts = []
        idxs = []
        base = 0
        for lake in lakes:
            if lake.geometry is None or lake.geometry.geom_type != "Polygon":
                continue
            coords = list(lake.geometry.exterior.coords)
            if len(coords) < 3:
                continue
            for lon, lat in coords:
                pos = _latlon_to_3d(lat, lon, radius=1.002)
                verts.append(pos)
            for i in range(1, len(coords) - 1):
                idxs.append(base)
                idxs.append(base + i)
                idxs.append(base + i + 1)
            base += len(coords)
        if not verts:
            return
        v_arr = np.array(verts, dtype=np.float32)
        i_arr = np.array(idxs, dtype=np.uint32)
        LAKE_VS = """#version 330
uniform mat4 uViewProj;
uniform float uRadius;
in vec3 in_position;
void main() {
    vec3 pos = in_position * uRadius;
    gl_Position = uViewProj * vec4(pos, 1.0);
}"""
        LAKE_FS = """#version 330
out vec4 fragColor;
uniform float uDistance;
void main() {
    float t = max(0.0, 1.0 - (uDistance - 2.0) / 5.0);
    vec3 col = vec3(0.15, 0.45, 0.75);
    float alpha = 0.5 * t;
    if (alpha < 0.01) discard;
    fragColor = vec4(col, alpha);
}"""
        self._lake_program = ctx.program(
            vertex_shader=LAKE_VS,
            fragment_shader=LAKE_FS,
        )
        self._lake_vbo = ctx.buffer(v_arr.tobytes())
        self._lake_ibo = ctx.buffer(i_arr.tobytes())
        self._lake_indices = len(idxs)
        self._lake_vao = ctx.vertex_array(
            self._lake_program,
            [(self._lake_vbo, '3f', 'in_position')],
            index_buffer=self._lake_ibo,
        )

    def setup(self, ctx) -> None:
        import h3
        self._setup_stars(ctx)

        # Load data from SQLite (sole data source)
        world_dir = os.path.dirname(self.parquet_path)
        db_path = os.path.join(world_dir, "world.sqlite")
        if not os.path.isfile(db_path):
            raise FileNotFoundError(f"No world.sqlite at {db_path} — run generator first")

        from simulation.world_db import WorldDB
        db = WorldDB(db_path)
        self._feature_store = db.load_features()
        cells_raw = db.load_cells()
        db.close()

        from simulation.layer0.climate import norm_to_c
        cells = []
        for r in cells_raw:
            tn = r["temperature_norm"]
            if tn is None:
                tn = (r["temperature_c"] + 5.0) / 45.0 if r["temperature_c"] else 0.5
            sl = (r["slope"], r.get("slope_dir", 0.0)) if r["slope"] else (0.0, 0.0)
            c = CellData(
                h3_id=r["h3_id"], resolution=2,
                elevation_mean=r["elevation"],
                elevation_variance=r.get("elevation_variance", 0.0),
                slope=sl,
                geological_type=r["geological_type"],
                temperature=tn,
                temp_seasonal_range=0.2,
                precipitation=r["precipitation_norm"] or 0.5,
                precip_seasonality=r.get("precip_seasonality", 0.3),
                prevailing_wind=(r.get("wind_u", 0.0), r.get("wind_v", 0.0)),
                soil_fertility=r["soil_fertility"] or 0.5,
                soil_depth=r["soil_depth"] or 0.5,
                water_table_depth=r.get("water_table_depth") or 5.0,
                organic_matter=r["organic_matter"] or 0.0,
                vegetation_cover=r["vegetation_cover"] or "barren",
                bedrock_class=r.get("bedrock_class", "unknown"),
                crustal_age_myr=r.get("crustal_age", 100.0),
                crustal_thickness_km=r.get("crustal_thickness", 35.0),
                thermal_gradient=r.get("thermal_gradient", 25.0),
                climate_class=r.get("climate_class", "") or "",
                canopy_density=r.get("canopy_density") or 0.0,
                biomass_kgm2=r.get("biomass_kgm2") or 0.0,
                runoff_ratio=r.get("runoff_ratio") or 0.5,
                effective_precip=r.get("effective_precip") or 0.0,
                hazard_level=r.get("hazard_level") or 0.0,
                tectonic_stress=r.get("tectonic_stress") or 0.0,
                clay_content=r.get("clay_content") or 0.0,
                sand_content=r.get("sand_content") or 0.0,
                silt_content=r.get("silt_content") or 0.0,
                soil_ph=r.get("soil_ph") or 7.0,
                cation_exchange=r.get("cation_exchange") or 5.0,
                interception_coefficient=r.get("interception_coefficient") or 0.15,
            )
            cells.append(c)

        # Print feature summary
        ftypes = {}
        for f in (self._feature_store.all_active if self._feature_store else []):
            ftypes[f.type] = ftypes.get(f.type, 0) + 1
        print(f"[PlanetScene] Loaded {len(cells)} cells, "
              f"{self._feature_store.count if self._feature_store else 0} features")
        for t, n in sorted(ftypes.items(), key=lambda x: -x[1]):
            print(f"  {t}: {n}")
        self.cells = cells

        print(f"[PlanetScene] Building icosahedron surface + hex borders...")

        # Build centroid array for elevation interpolation
        cent_3d = []
        for cell in cells:
            latlng = h3.cell_to_latlng(cell.h3_id)
            cx, cy, cz = _latlon_to_3d(latlng[0], latlng[1])
            cent_3d.append((cx, cy, cz, cell.elevation_mean))
        cent_arr = np.array(cent_3d, dtype=np.float32)

        def _interp_elevation(lat: float, lon: float) -> float:
            px, py, pz = _latlon_to_3d(lat, lon)
            dx = cent_arr[:, 0] - px
            dy = cent_arr[:, 1] - py
            dz = cent_arr[:, 2] - pz
            dists = np.sqrt(dx*dx + dy*dy + dz*dz)
            idx = np.argpartition(dists, 3)[:3]
            w = 1.0 / (dists[idx] + 1e-10)
            return float(np.average(cent_arr[idx, 3], weights=w))

        # ── 1. Icosahedron surface (smooth, continuous) ──
        ico_verts, ico_indices = self._build_icosahedron_sphere(subdivisions=4)
        self._ico_vertices_base = list(ico_verts)
        self._ico_vertex_elevs = []
        self._ico_vertex_colors = []

        vertices = []
        # Pre-build cell lookup maps
        cell_elev_map = {cell.h3_id: cell.elevation_mean for cell in cells}
        cell_geo_map = {cell.h3_id: cell.geological_type for cell in cells}
        for i, (x, y, z) in enumerate(ico_verts):
            lat = math.degrees(math.asin(max(-1.0, min(1.0, y))))
            lon = math.degrees(math.atan2(z, x))
            el = _interp_elevation(lat, lon)
            # Ocean detection from geological type at vertex position
            h = h3.latlng_to_cell(lat, lon, 2)
            if h in cell_geo_map:
                geo_type = cell_geo_map[h]
                is_ocean_cell = (geo_type == 0)
            else:
                is_ocean_cell = el < -0.015
            col = self._sample_color(lat, lon, el, is_ocean_cell, geo_type)
            self._ico_vertex_elevs.append(el)
            self._ico_vertex_colors.append(col)
            vertices.append((x, y, z, col[0], col[1], col[2], el))

        self._ico_vertex_count = len(ico_verts)
        vertices_arr = np.array(vertices, dtype=np.float32)
        indices_arr = np.array(ico_indices, dtype=np.uint32)
        print(f"[PlanetScene] Icosahedron: {len(ico_verts)} verts, {len(ico_indices)//3} tris")

        self._program = ctx.program(
            vertex_shader=VERTEX_SHADER,
            fragment_shader=FRAGMENT_SHADER,
        )
        self._ico_vbo = ctx.buffer(vertices_arr.tobytes())
        self._ico_ibo = ctx.buffer(indices_arr.tobytes())
        self._ico_num_indices = len(indices_arr)
        self._ico_vao = ctx.vertex_array(
            self._program,
            [(self._ico_vbo, '3f 3f f', 'in_position', 'in_color', 'in_elevation')],
            index_buffer=self._ico_ibo,
        )

        # ── 2. Lakes ──
        self._build_lakes(ctx)
        # ── 3. Rivers ──
        self._build_rivers(ctx)
        # ── 4. Ore deposits ──
        self._build_ore_markers(ctx)

        # ── 3. Hex borders overlay ──
        border_verts = []
        border_indices = []
        border_base = 0
        for cell in cells:
            bnd = h3.cell_to_boundary(cell.h3_id)
            n = len(bnd)
            if n < 3:
                continue
            for b in bnd:
                pos = _latlon_to_3d(b[0], b[1])
                border_verts.append(pos)
            for i in range(n):
                border_indices.append(border_base + i)
                border_indices.append(border_base + (i + 1) % n)
            border_base += n

        bv_arr = np.array(border_verts, dtype=np.float32)
        bi_arr = np.array(border_indices, dtype=np.uint32)
        self._border_program = ctx.program(
            vertex_shader=BORDER_VERTEX_SHADER,
            fragment_shader=BORDER_FRAGMENT_SHADER,
        )
        self._border_vbo = ctx.buffer(bv_arr.tobytes())
        self._border_ibo = ctx.buffer(bi_arr.tobytes())
        self._border_num_indices = len(bi_arr)
        self._border_vao = ctx.vertex_array(
            self._border_program,
            [(self._border_vbo, '3f', 'in_position')],
            index_buffer=self._border_ibo,
        )

        self._sel_border_vao = None
        print("[PlanetScene] Setup complete.")

    def on_click(self, screen_x: int, screen_y: int, win_w: int, win_h: int, camera, ctx) -> None:
        """Handle mouse click — ray-pick hex cell and show info panel
        or toggle section in info panel."""
        # Check if click is on info panel section headers
        pw = win_w // 3
        ph = win_h // 3
        if screen_x < pw and screen_y > win_h - ph:
            # Click is within the panel area
            tex_y = screen_y - (win_h - ph)  # Y from top of panel texture
            content_y = tex_y + self._panel_scroll
            for idx, (y_top, y_bot) in self._section_headers.items():
                if y_top <= content_y < y_bot:
                    # Toggle this section
                    if idx in self._expanded_sections:
                        self._expanded_sections.discard(idx)
                    else:
                        self._expanded_sections.add(idx)
                    self._panel_dirty = True
                    self._panel_redraw = True
                    return  # don't select cell

        import h3
        # 1. Compute ray from camera through mouse position
        view = camera.view_matrix
        proj = camera.projection_matrix
        inv_vp = np.linalg.inv(proj @ view)

        # Screen to NDC
        x_ndc = (2.0 * screen_x / win_w) - 1.0
        y_ndc = 1.0 - (2.0 * screen_y / win_h)

        near_pt = inv_vp @ np.array([x_ndc, y_ndc, -1.0, 1.0])
        near_pt = near_pt[:3] / near_pt[3]
        far_pt = inv_vp @ np.array([x_ndc, y_ndc, 1.0, 1.0])
        far_pt = far_pt[:3] / far_pt[3]

        ray_origin = camera.position
        ray_dir = far_pt - near_pt
        ray_dir = ray_dir / np.linalg.norm(ray_dir)

        # 2. Ray-sphere intersection (sphere at origin, radius = self.radius)
        oc = ray_origin  # sphere center is (0,0,0)
        a = np.dot(ray_dir, ray_dir)
        b = 2.0 * np.dot(oc, ray_dir)
        c = np.dot(oc, oc) - self.radius * self.radius
        disc = b*b - 4.0*a*c
        if disc < 0:
            return  # miss

        t = (-b - math.sqrt(disc)) / (2.0 * a)
        if t < 0:
            t = (-b + math.sqrt(disc)) / (2.0 * a)
        if t < 0:
            return

        hit = ray_origin + t * ray_dir

        # 3. Convert hit point to lat/lon
        norm_hit = hit / np.linalg.norm(hit)
        lat = math.degrees(math.asin(norm_hit[1]))
        lon = math.degrees(math.atan2(norm_hit[2], norm_hit[0]))
        # 4. Find which H3 cell contains this point
        # Use resolution from first cell
        res = self.cells[0].resolution if self.cells else 2
        cell_id = h3.latlng_to_cell(lat, lon, res)

        # 5. Find cell data
        cell_data = None
        for c in self.cells:
            if c.h3_id == cell_id:
                cell_data = c
                break

        if cell_data is None:
            return

        self.selected_cell_id = cell_id
        self.selected_cell_data = cell_data
        self._panel_dirty = True

        # Sync features for the selected cell
        if self._feature_store is not None:
            self._feature_store.sync_cell(cell_data)

        # 6. Rebuild selection highlight geometry
        self._rebuild_selection_border(ctx)

    def _rebuild_selection_border(self, ctx) -> None:
        """Build or update the highlighted border VAO for the selected cell."""
        import h3
        # Clean old
        if self._sel_border_vao:
            self._sel_border_vao.release()
        if self._sel_border_vbo:
            self._sel_border_vbo.release()
        if self._sel_border_ibo:
            self._sel_border_ibo.release()
        self._sel_border_vao = None
        self._sel_border_vbo = None
        self._sel_border_ibo = None
        self._sel_border_num_indices = 0

        if not self.selected_cell_id:
            return

        bnd = h3.cell_to_boundary(self.selected_cell_id)
        if len(bnd) < 3:
            return

        verts = []
        idxs = []
        for i, b in enumerate(bnd):
            pos = _latlon_to_3d(b[0], b[1])
            verts.append((pos[0], pos[1], pos[2]))
            idxs.append(i)
        # Close the loop
        idxs.append(0)

        v_arr = np.array(verts, dtype=np.float32)
        i_arr = np.array(idxs, dtype=np.uint32)
        self._sel_border_vbo = ctx.buffer(v_arr.tobytes())
        self._sel_border_ibo = ctx.buffer(i_arr.tobytes())
        self._sel_border_num_indices = len(idxs)
        self._sel_border_program = ctx.program(
            vertex_shader=SEL_VERTEX_SHADER,
            fragment_shader=SEL_FRAGMENT_SHADER,
        )
        self._sel_border_vao = ctx.vertex_array(
            self._sel_border_program,
            [(self._sel_border_vbo, '3f', 'in_position')],
            index_buffer=self._sel_border_ibo,
        )

    def _render_info_panel(self, ctx, win_w: int, win_h: int) -> None:
        """Render the cell info panel — pixel-perfect, scrollable with scrollbar."""
        cell = self.selected_cell_data
        if cell is None:
            return

        if self._panel_dirty or self._panel_redraw:
            self._panel_dirty = False
            self._panel_redraw = False
            pw = win_w // 3
            ph = win_h // 3
            font_size = max(14, min(24, ph // 14))
            try:
                font = ImageFont.truetype("arial.ttf", font_size)
                font_big = ImageFont.truetype("arial.ttf", font_size + 2)
                font_small = ImageFont.truetype("arial.ttf", font_size - 2)
            except (OSError, IOError):
                font = ImageFont.load_default()
                font_big = font_small = font

            line_h = font_size + 9
            header_h = font_size + 14
            bg = (38, 34, 30, 240)
            img = Image.new('RGBA', (pw, ph), bg)
            draw = ImageDraw.Draw(img)

            y = 6

            def block_header(title, y):
                draw.line([(10, y+header_h-2), (pw-10, y+header_h-2)], fill=(55, 50, 45, 255), width=1)
                draw.text((14, y+2), title, fill=(210, 200, 185, 255), font=font_big)
                return y + header_h + 2

            def row(label, value, y, alt=False):
                bg_row = (48, 43, 38, 255) if alt else (42, 38, 34, 255)
                draw.rectangle([0, y, pw, y+line_h], fill=bg_row)
                draw.text((14, y+2), label, fill=(170, 160, 145, 255), font=font_small)
                draw.text((pw//2 + 10, y+2), str(value), fill=(235, 228, 218, 255), font=font)
                return y + line_h

            sc = self._panel_scroll
            rendered_any = False
            visible_y = y - sc  # scroll offset: negative = start above visible area

            sections = []

            # Build sections list
            secs = []
            def add_section(title, rows):
                secs.append((title, rows))

            gen_rows = []
            import h3
            from simulation.layer0.climate import norm_to_c

            latlng = h3.cell_to_latlng(cell.h3_id)
            lat_s = f"{latlng[0]:.2f}°{'N' if latlng[0]>=0 else 'S'}"
            lon_s = f"{latlng[1]:.2f}°{'E' if latlng[1]>=0 else 'W'}"

            # ── 1. GENERAL ──
            gen_rows = []
            gen_rows.append(("Coordinates", f"{lat_s}, {lon_s}"))
            gen_rows.append(("Elevation", f"{cell.elevation_mean*5000:.0f} m"))
            gen_rows.append(("Slope", f"{cell.slope[0]*100:.1f}%"))
            gen_rows.append(("Temperature", f"{norm_to_c(cell.temperature):.1f} °C"))
            gen_rows.append(("Precipitation", f"{cell.precipitation*2000:.0f} mm/yr"))
            gen_rows.append(("Seasonality", f"{cell.precip_seasonality*100:.0f}%"))
            cc = cell.climate_class or ""
            gen_rows.append(("Climate", f"{koppen_name(cc)} ({cc})" if cc else "N/A"))
            gen_rows.append(("Wind", f"({cell.prevailing_wind[0]:.2f}, {cell.prevailing_wind[1]:.2f})"))
            add_section("GENERAL", gen_rows)

            # ── 2. GEOLOGY ──
            geo_rows = []
            geo_rows.append(("Terrain", geology_name(cell.geological_type)))
            geo_rows.append(("Bedrock", getattr(cell, 'bedrock_class', 'unknown').replace('_', ' ').title()))
            geo_rows.append(("Crustal Age", f"{getattr(cell, 'crustal_age_myr', 0):.0f} Myr"))
            geo_rows.append(("Crust Thick", f"{getattr(cell, 'crustal_thickness_km', 0):.0f} km"))
            geo_rows.append(("Thermal Grad", f"{getattr(cell, 'thermal_gradient', 0):.0f} °C/km"))
            ts = cell.tectonic_stress
            ts_label = f"{'Low' if ts<0.3 else 'Moderate' if ts<0.6 else 'High' if ts<0.8 else 'Extreme'} ({ts:.2f})"
            geo_rows.append(("Tectonic Stress", ts_label))
            add_section("GEOLOGY", geo_rows)

            # ── 3. SOIL ──
            soil_rows = []
            soil_rows.append(("Fertility", f"{cell.soil_fertility*100:.0f}%"))
            soil_rows.append(("Depth", f"{cell.soil_depth:.2f}"))
            soil_rows.append(("Organic Matter", f"{cell.organic_matter*100:.1f}%"))
            soil_rows.append(("Clay/Sand/Silt", f"{cell.clay_content*100:.0f}/{cell.sand_content*100:.0f}/{cell.silt_content*100:.0f}"))
            soil_rows.append(("pH", f"{cell.soil_ph:.1f}"))
            soil_rows.append(("CEC", f"{cell.cation_exchange:.1f} cmol/kg"))
            add_section("SOIL", soil_rows)

            # ── 4. WATER ──
            water_rows = []
            water_rows.append(("Water Table", f"{cell.water_table_depth:.1f}"))
            water_rows.append(("Runoff Ratio", f"{cell.runoff_ratio*100:.0f}%"))
            water_rows.append(("Effective Precip", f"{cell.effective_precip:.3f}"))
            water_rows.append(("Hazard Level", f"{cell.hazard_level:.2f}"))
            add_section("WATER", water_rows)

            # ── 5. VEGETATION ──
            veg_rows = []
            veg_rows.append(("Cover", cell.vegetation_cover or "barren"))
            veg_rows.append(("Canopy", f"{cell.canopy_density*100:.0f}%"))
            veg_rows.append(("Biomass", f"{cell.biomass_kgm2:.1f} kg/m²"))
            veg_rows.append(("Interception", f"{cell.interception_coefficient*100:.0f}%"))
            add_section("VEGETATION", veg_rows)

            # ── 6. RESOURCES + FEATURES (hex-query based) ──
            res_rows = []
            feat_rows = []
            if self._feature_store is not None:
                hex_features = self._feature_store.features_in_hex(cell.h3_id)
            else:
                hex_features = []
            seen_types: dict = {}
            for feat in hex_features:
                nm = feat.name or feat.feature_id[:10]
                ft = feat.type.replace("_", " ").title()
                seen_types.setdefault(ft, 0)
                seen_types[ft] += 1
                if feat.type == "ore_deposit":
                    primary = feat.properties.get("primary_ore", "?")
                    grade = feat.properties.get("grade", 0)
                    depth = feat.properties.get("depth_top_m", 0)
                    ore_name = primary.replace("_", " ").title() if primary != "?" else "Unknown Ore"
                    res_rows.append((ore_name, f"Grade {grade*100:.1f}%, {depth}m deep"))
                elif feat.type == "special_resource_zone":
                    rpct = feat.properties.get("resource_pct", 0)
                    res_rows.append(("Magic Flux", f"{rpct:.0f}% concentration"))
                elif feat.type == "spring":
                    flow = feat.properties.get("flow_rate_ls", 0)
                    stemp = feat.properties.get("temperature_c", 10)
                    res_rows.append(("Spring", f"{flow:.1f} L/s, {stemp:.0f} °C"))
                elif feat.type == "lake":
                    vol = feat.properties.get("volume_m3", 0)
                    fill = feat.properties.get("fill_fraction", 0)
                    res_rows.append(("Lake", f"{fill*100:.0f}% full, {vol:.0f} m³"))
                elif feat.type == "river":
                    width = feat.properties.get("width_km", 0)
                    res_rows.append(("River", f"{width:.2f} km wide"))
                elif feat.type not in ("ore_deposit", "special_resource_zone", "spring", "lake", "river",
                                        "temperature_band", "soil_region", "geology_region"):
                    feat_rows.append((ft, nm))
            for ft, count in sorted(seen_types.items()):
                if count > 1 and ft not in ("Ore Deposit", "Special Resource Zone", "Spring", "Lake", "River",
                                             "Temperature Band", "Soil Region", "Geology Region"):
                    feat_rows.append((f"{ft} (×{count})", ""))
            if res_rows:
                add_section("RESOURCES", res_rows)
            if feat_rows:
                add_section("FEATURES", feat_rows)

            # Compute total content height (only expanded sections)
            total_h = 6
            for idx, (title, rows) in enumerate(secs):
                expanded = idx in self._expanded_sections
                total_h += header_h + 2
                if expanded:
                    total_h += len(rows) * line_h
            total_h += 8
            self._panel_content_height = total_h
            scroll_max = max(0, total_h - ph)
            self._panel_scroll = max(0, min(self._panel_scroll, scroll_max))
            sc = self._panel_scroll

            # Render visible portion
            draw_y = 6 - sc
            for idx, (title, rows) in enumerate(secs):
                expanded = idx in self._expanded_sections
                # Header (always visible, clickable)
                arrow = "v" if expanded else ">"
                if draw_y + header_h > 0 and draw_y < ph:
                    draw.line([(10, draw_y+header_h-2), (pw-10, draw_y+header_h-2)], fill=(55, 50, 45, 255), width=1)
                    draw.text((14, draw_y+2), f"{arrow} {title}", fill=(210, 200, 185, 255), font=font_big)
                header_y = draw_y  # store for click detection
                draw_y += header_h + 2
                if expanded:
                    for i, (label, value) in enumerate(rows):
                        if draw_y + line_h > 0 and draw_y < ph:
                            alt = i % 2 == 1
                            bg_row = (48, 43, 38, 255) if alt else (42, 38, 34, 255)
                            draw.rectangle([0, draw_y, pw, draw_y+line_h], fill=bg_row)
                            draw.text((14, draw_y+2), label, fill=(170, 160, 145, 255), font=font_small)
                            draw.text((pw//2 + 10, draw_y+2), str(value), fill=(235, 228, 218, 255), font=font)
                        draw_y += line_h
                # Store header screen Y for click detection
                # (relative to panel top, minus scroll)
                self._section_headers[idx] = (header_y + sc, header_y + sc + header_h + 2)

            # Draw scrollbar
            if total_h > ph:
                bar_w = 6
                thumb_h = max(20, int(ph * ph / total_h))
                bar_x = pw - bar_w - 4
                track_h = ph - thumb_h
                bar_y = int(sc / scroll_max * track_h) if scroll_max > 0 else 0
                draw.rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+thumb_h], fill=(100, 95, 90, 200))

            # Upload
            img_data = img.tobytes()
            if self._panel_texture:
                self._panel_texture.release()
            self._panel_texture = ctx.texture((pw, ph), 4, img_data)
            self._panel_texture.filter = (mgl.LINEAR, mgl.LINEAR)

        if self._panel_texture is None:
            return

        if self._panel_program is None:
            self._panel_program = ctx.program(
                vertex_shader=PANEL_VERTEX_SHADER,
                fragment_shader=PANEL_FRAGMENT_SHADER,
            )

        pw_disp = win_w // 3
        ph_disp = win_h // 3
        panel_w = 2.0 * pw_disp / win_w
        panel_h = 2.0 * ph_disp / win_h
        mx, my = -1.0, -1.0
        quad = np.array([
            mx, my, 0, 1,
            mx + panel_w, my, 1, 1,
            mx + panel_w, my + panel_h, 1, 0,
            mx, my + panel_h, 0, 0,
        ], dtype=np.float32)

        # Show full texture — no UV tricks, no stretching
        quad = np.array([
            mx, my, 0, 1,
            mx + panel_w, my, 1, 1,
            mx + panel_w, my + panel_h, 1, 0,
            mx, my + panel_h, 0, 0,
        ], dtype=np.float32)

        if self._panel_vbo is None:
            self._panel_vbo = ctx.buffer(quad.tobytes())
        else:
            self._panel_vbo.write(quad.tobytes())
        self._panel_vao = ctx.vertex_array(
            self._panel_program,
            [(self._panel_vbo, '2f 2f', 'in_position', 'in_uv')],
        )

        ctx.disable(mgl.DEPTH_TEST)
        self._panel_texture.use(0)
        self._panel_program['uTexture'].value = 0
        self._panel_vao.render(mgl.TRIANGLE_FAN, vertices=4)
        ctx.enable(mgl.DEPTH_TEST)

    def render(self, ctx, camera) -> None:
        if self._program is None:
            return

        # Compute view-projection matrix (transpose for OpenGL column-major)
        view = camera.view_matrix
        proj = camera.projection_matrix
        vp = (proj @ view).T  # transpose to column-major

        # ── Starfield (no depth test, behind everything) ──
        ctx.disable(mgl.DEPTH_TEST)
        self._star_program['uViewProj'].write(vp.astype(np.float32).tobytes())
        self._star_vao.render(mgl.TRIANGLES, vertices=self._num_stars)

        # ── Planet ──
        ctx.enable(mgl.DEPTH_TEST)
        ctx.disable(mgl.CULL_FACE)

        # Model matrix (identity for now, will rotate planet later)
        model = np.eye(4, dtype=np.float32)

        # Elevation scale: 0 — flat surface, height via color only
        elev_scale = 0.0

        self._program['uViewProj'].write(vp.astype(np.float32).tobytes())
        self._program['uModel'].write(model.tobytes())
        self._program['uRadius'].value = self.radius
        self._program['uElevScale'].value = elev_scale
        self._program['uSunDir'].value = tuple(self.sun_dir)
        self._program['uCamPos'].value = tuple(camera.position)
        self._program['uAtmosIntensity'].value = self.atmos_intensity
        self._program['uDistance'].value = camera.distance
        self._program['uTerrainTint'].value = (0.0, 0.0, 0.0)

        # Fill pass — smooth icosahedron surface
        if self._ico_vao:
            self._ico_vao.render(mgl.TRIANGLES, vertices=self._ico_num_indices)

        # Lake pass (only visible when close, very subtle)
        if self._lake_vao and camera.distance < 3.0:
            ctx.enable(mgl.BLEND)
            ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA
            self._lake_program['uViewProj'].write(vp.astype(np.float32).tobytes())
            self._lake_program['uRadius'].value = self.radius * 1.0005  # nearly flush
            self._lake_program['uDistance'].value = camera.distance
            self._lake_vao.render(mgl.TRIANGLES, vertices=self._lake_indices)
            ctx.disable(mgl.BLEND)

        # River pass
        if self._river_vao and camera.distance < 6.0:
            ctx.enable(mgl.BLEND)
            ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA
            self._river_program['uViewProj'].write(vp.astype(np.float32).tobytes())
            self._river_program['uRadius'].value = self.radius * 1.003
            self._river_vao.render(mgl.LINES, vertices=self._river_points)
            ctx.disable(mgl.BLEND)

        # Selection highlight
        if self.selected_cell_id and self._sel_border_vao:
            self._sel_border_program['uViewProj'].write(vp.astype(np.float32).tobytes())
            self._sel_border_program['uRadius'].value = self.radius * 1.002
            self._sel_border_vao.render(mgl.LINE_STRIP, vertices=self._sel_border_num_indices)

        # Hex border overlay — only visible close up
        if self._border_vao and camera.distance < 5.0:
            ctx.enable(mgl.BLEND)
            ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA
            self._border_program['uViewProj'].write(vp.astype(np.float32).tobytes())
            self._border_program['uRadius'].value = self.radius * 1.001
            self._border_program['uDistance'].value = camera.distance
            self._border_vao.render(mgl.LINES, vertices=self._border_num_indices)
            ctx.disable(mgl.BLEND)
        w, h = ctx.viewport[2], ctx.viewport[3]
        self._render_info_panel(ctx, w, h)

    def update(self, dt: float) -> None:
        pass

    def scroll_panel(self, delta: float) -> None:
        """Scroll the info panel by delta pixels and redraw."""
        if self.selected_cell_data is None:
            return
        self._panel_scroll -= int(delta * 30)
        self._panel_scroll = max(0, self._panel_scroll)
        self._panel_redraw = True

    def start_scrollbar_drag(self, screen_y: int, win_h: int) -> None:
        """Start dragging the scrollbar."""
        self._scrollbar_dragging = True
        self._scrollbar_drag_start_y = screen_y
        self._scrollbar_drag_start_scroll = self._panel_scroll

    def drag_scrollbar(self, screen_y: int, win_h: int) -> None:
        """Continue dragging the scrollbar."""
        if not getattr(self, '_scrollbar_dragging', False):
            return
        content_h = self._panel_content_height
        ph = win_h // 3
        scroll_max = max(0, content_h - ph)
        if scroll_max <= 0:
            return
        # Panel occupies bottom 1/3 of screen in NDC-relative coords
        panel_top_screen = win_h - ph
        t = (screen_y - panel_top_screen) / ph
        t = max(0.0, min(1.0, t))
        self._panel_scroll = int(t * scroll_max)
        self._panel_redraw = True

    def end_scrollbar_drag(self) -> None:
        """End scrollbar dragging."""
        self._scrollbar_dragging = False

    def cleanup(self) -> None:
        if self._star_vao:
            self._star_vao.release()
        if self._star_vbo:
            self._star_vbo.release()
        if self._star_program:
            self._star_program.release()
        if self._ico_vao:
            self._ico_vao.release()
        if self._ico_vbo:
            self._ico_vbo.release()
        if self._ico_ibo:
            self._ico_ibo.release()
        if self._program:
            self._program.release()
        if self._border_vao:
            self._border_vao.release()
        if self._border_vbo:
            self._border_vbo.release()
        if self._border_ibo:
            self._border_ibo.release()
        if self._border_program:
            self._border_program.release()
        if self._river_vao:
            self._river_vao.release()
        if self._river_vbo:
            self._river_vbo.release()
        if self._river_ibo:
            self._river_ibo.release()
        if self._river_program:
            self._river_program.release()
        if self._lake_vao:
            self._lake_vao.release()
        if self._lake_vbo:
            self._lake_vbo.release()
        if self._lake_ibo:
            self._lake_ibo.release()
        if self._lake_program:
            self._lake_program.release()
        if self._ore_vao:
            self._ore_vao.release()
        if self._ore_vbo:
            self._ore_vbo.release()
        if self._ore_ibo:
            self._ore_ibo.release()
        if self._ore_program:
            self._ore_program.release()
        if self._sel_border_vao:
            self._sel_border_vao.release()
        if self._sel_border_vbo:
            self._sel_border_vbo.release()
        if self._sel_border_ibo:
            self._sel_border_ibo.release()
        if self._sel_border_program:
            self._sel_border_program.release()
        if self._panel_texture:
            self._panel_texture.release()
        if self._panel_vbo:
            self._panel_vbo.release()
        if self._panel_program:
            self._panel_program.release()
