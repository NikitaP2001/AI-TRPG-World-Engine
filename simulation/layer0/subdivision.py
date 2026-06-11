"""Layer 0 — Lazy subdivision of H3 cells.

Cells subdivide on demand when:
  1. A placed feature is smaller than the cell's resolution
  2. The generation detects high internal variance
  3. Explicitly requested

Subdivision creates 7 children at resolution+1 via h3.cell_to_children().
The parent is marked subdivided; higher layers read from children.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import h3
import numpy as np

from .cell_model import CellData


# ======================================================================
# Subdivision tree node
# ======================================================================


@dataclass
class SubdivNode:
    """One cell in the subdivision tree.

    Internal nodes (is_subdivided=True) have 7 children at resolution+1.
    Leaf nodes (is_subdivided=False) hold the actual CellData.
    """
    h3_id: str
    resolution: int
    cell_data: Optional[CellData] = None
    children: List[SubdivNode] = field(default_factory=list)
    parent: Optional[SubdivNode] = None

    @property
    def is_subdivided(self) -> bool:
        return len(self.children) > 0

    @property
    def leaf_data(self) -> Optional[CellData]:
        """Return CellData from this node or its first leaf child."""
        if self.cell_data is not None:
            return self.cell_data
        if self.children:
            return self.children[0].leaf_data
        return None

    def all_leaves(self) -> List[SubdivNode]:
        """Return all leaf nodes in this subtree."""
        if not self.children:
            return [self]
        result: List[SubdivNode] = []
        for c in self.children:
            result.extend(c.all_leaves())
        return result


# ======================================================================
# SubdivisionManager
# ======================================================================


class SubdivisionManager:
    """Manages the H3 subdivision tree for a world.

    Usage:
      mgr = SubdivisionManager(base_cells, base_resolution)
      mgr.subdivide_high_variance(elevation_map, threshold=0.15)
      all_cells = mgr.flatten()  # flat list of leaf CellData
    """

    def __init__(
        self,
        base_cells: List[CellData],
        max_depth: int = 4,
    ) -> None:
        self.max_depth = max_depth
        self._roots: Dict[str, SubdivNode] = {}
        self._by_id: Dict[str, SubdivNode] = {}

        for cell in base_cells:
            node = SubdivNode(
                h3_id=cell.h3_id,
                resolution=cell.resolution,
                cell_data=cell,
            )
            self._roots[cell.h3_id] = node
            self._by_id[cell.h3_id] = node

        self._base_resolution = base_cells[0].resolution if base_cells else 0

    # ── Lookup ───────────────────────────────────────────────────────

    def get_node(self, h3_id: str) -> Optional[SubdivNode]:
        return self._by_id.get(h3_id)

    def get_cell(self, h3_id: str) -> Optional[CellData]:
        node = self._by_id.get(h3_id)
        return node.cell_data if node else None

    def has(self, h3_id: str) -> bool:
        return h3_id in self._by_id

    # ── Subdivision ──────────────────────────────────────────────────

    def subdivide(
        self,
        parent_id: str,
        populate_fn: Optional[Callable[[str, int, SubdivNode], Optional[CellData]]] = None,
    ) -> List[SubdivNode]:
        """Subdivide a leaf cell into 7 children at resolution+1.

        Args:
            parent_id: H3 ID of the cell to subdivide.
            populate_fn: Optional callback(child_h3_id, child_res, parent_node)
                         that returns a CellData for the child, or None for default.

        Returns:
            List of new child SubdivNode objects.
        """
        parent_node = self._by_id.get(parent_id)
        if parent_node is None or parent_node.is_subdivided:
            return []

        parent_res = parent_node.resolution
        if parent_res >= 15:  # H3 max resolution
            return []

        child_res = parent_res + 1
        child_ids = list(h3.cell_to_children(parent_id, child_res))

        children: List[SubdivNode] = []
        for cid in child_ids:
            child = SubdivNode(h3_id=cid, resolution=child_res, parent=parent_node)
            if populate_fn:
                child.cell_data = populate_fn(cid, child_res, parent_node)
            children.append(child)
            self._by_id[cid] = child

        parent_node.children = children
        parent_node.cell_data = None  # Move data to children
        return children

    def subdivide_high_variance(
        self,
        field: str = "elevation",
        threshold: float = 0.15,
        max_depth_per_cell: int = 2,
        variance_fn: Optional[Callable[[str, List[CellData]], float]] = None,
    ) -> int:
        """Subdivide cells where internal variance exceeds threshold.

        Iteratively finds leaf cells with high variance in the given field
        and subdivides them. Runs breadth-first up to max_depth_per_cell
        levels below base resolution.

        Args:
            field: CellData attribute name to check variance on.
            threshold: Variance threshold for subdivision.
            max_depth_per_cell: How many levels to subdivide each base cell.
            variance_fn: Custom variance function(child_h3_ids_at_one_level_down)

        Returns:
            Number of new cells created.
        """
        created = 0

        for depth in range(max_depth_per_cell):
            # Get current leaves at this depth
            current_leaves = [n for n in self._by_id.values()
                              if not n.is_subdivided
                              and n.resolution == self._base_resolution + depth]

            if not current_leaves:
                break

            to_subdivide: List[SubdivNode] = []
            for leaf in current_leaves:
                if leaf.cell_data is None:
                    continue
                val = getattr(leaf.cell_data, field, 0.0)

                # Estimate variance by looking at neighbours
                neighbours = h3.grid_ring(leaf.h3_id, 1) or []
                n_vals: List[float] = []
                for nh in neighbours:
                    n_node = self._by_id.get(nh)
                    if n_node and n_node.cell_data is not None:
                        n_vals.append(getattr(n_node.cell_data, field, val))

                if not n_vals:
                    continue

                local_var = np.var([val] + n_vals)
                if local_var > threshold:
                    to_subdivide.append(leaf)

            for leaf in to_subdivide:
                parent_data = leaf.cell_data  # save before subdivide clears it
                children = self.subdivide(leaf.h3_id)
                if children and parent_data is not None:
                    self._populate_children(children, parent_data)
                    created += len(children)

            if not to_subdivide:
                break

        return created

    def _populate_children(
        self,
        children: List[SubdivNode],
        parent_data: CellData,
    ) -> None:
        """Fill children with values derived from parent."""
        if not children:
            return

        rng = random.Random(hash(parent_data.h3_id) & 0xFFFFFFFF)
        for child in children:
            # Small random perturbation for elevation
            perturb = rng.gauss(0.0, 0.02)
            child_el = max(0.0, min(1.0, parent_data.elevation + perturb))

            child.cell_data = CellData(
                h3_id=child.h3_id,
                resolution=child.resolution,
                parent_h3_id=parent_data.h3_id,
                is_subdivided=False,
                elevation=child_el,
                slope=parent_data.slope,
                geological_type=parent_data.geological_type,
                terrain_type=list(parent_data.terrain_type),
                flow_direction=-1,
                flow_accumulation=0.0,
                river_flag=False,
                water_body_type=parent_data.water_body_type,
                drainage_basin_id=parent_data.drainage_basin_id,
                water_table_depth=parent_data.water_table_depth,
                temperature=parent_data.temperature + rng.gauss(0.0, 0.01),
                temp_seasonal_range=parent_data.temp_seasonal_range,
                precipitation=parent_data.precipitation + rng.gauss(0.0, 0.01),
                precip_seasonality=parent_data.precip_seasonality,
                climate_class=parent_data.climate_class,
                prevailing_wind=parent_data.prevailing_wind,
                soil_fertility=parent_data.soil_fertility + rng.gauss(0.0, 0.02),
                hazard_level=parent_data.hazard_level,
                special_resource_flux=list(parent_data.special_resource_flux),
                tectonic_stress=parent_data.tectonic_stress,
                anchor_feature_ids=list(parent_data.anchor_feature_ids),
            )

    # ── Flatten ──────────────────────────────────────────────────────

    def flatten(self) -> List[CellData]:
        """Return CellData for all leaf cells (flat list, all resolutions)."""
        result: List[CellData] = []
        for node in self._by_id.values():
            if not node.is_subdivided and node.cell_data is not None:
                # Mark as subdivided if its parent is subdivided
                if node.parent and node.parent.is_subdivided:
                    p = node.parent
                    while p:
                        if p.is_subdivided:
                            node.cell_data.is_subdivided = True
                            break
                        p = p.parent
                result.append(node.cell_data)
        return result

    @property
    def total_cells(self) -> int:
        return sum(1 for n in self._by_id.values() if not n.is_subdivided)

    @property
    def total_nodes(self) -> int:
        return len(self._by_id)

    def stats(self) -> Dict[str, int]:
        leaves = [n for n in self._by_id.values() if not n.is_subdivided]
        res_counts: Dict[int, int] = {}
        for leaf in leaves:
            res_counts[leaf.resolution] = res_counts.get(leaf.resolution, 0) + 1
        return {
            "total_nodes": self.total_nodes,
            "total_cells": self.total_cells,
            "resolutions": res_counts,
        }
