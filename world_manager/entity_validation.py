"""Entity Validation — generic form validation.

ONLY form-level validation lives here:
  - Position format: coords lat/lon bounds, inside/near entity_id format
  - Facts safety: warn if mechanical data leaks into prose field

There is NO per-type validation ("location must have facts", "NPC must be
inside a location", etc.). Structural rules are the WM's responsibility
through base_type templates, not the engine's. Entity type is an arbitrary
string defined by the WM; the engine does not know or care which types exist.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════════════
# Position validation — generic form check for ALL entity types
# ═══════════════════════════════════════════════════════════════════════


def validate_position(position: Optional[Dict[str, Any]]) -> Optional[str]:
    """Check position format. Returns error string or None if valid.

    Valid forms:
      {"coords": [lat, lon]} or [lat, lon, z] — absolute, lat=-90..90, lon=-180..180
      {"inside": entity_id, "offset": [x, y, z]} — contained within another
      entity at local 3D coordinates (meters).
      {"near": entity_id, "offset": [x, y, z]} — approximately co-located,
      with offset relative to reference entity.

    This is a pure form check — validates structure regardless of entity
    type. Does not check whether referenced entity IDs exist.
    """
    if not position:
        return None

    valid_keys = {"inside", "near", "coords", "offset"}
    extra = set(position.keys()) - valid_keys
    if extra:
        return f"Unknown position key(s): {', '.join(sorted(extra))}. Use: inside, near, coords."

    # Mutually exclusive: only one of coords/inside/near
    mode_keys = {"coords", "inside", "near"}
    present = mode_keys & set(position.keys())
    if len(present) > 1:
        return ("Position must use exactly ONE form. "
                f"Got both: {', '.join(sorted(present))}. "
                "Use coords for absolute, or inside/near with offset for relative.")

    if "coords" in position:
        coords = position["coords"]
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            return "coords must be [lat, lon] or [lat, lon, z]."
        lat, lon = coords[0], coords[1]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return "coords values must be numbers."
        if lat < -90 or lat > 90:
            return f"Latitude must be between -90 and 90, got {lat}."
        if lon < -180 or lon > 180:
            return f"Longitude must be between -180 and 180, got {lon}."

    # inside/near: non-empty entity_id with offset [x,y,z]
    for key in ("inside", "near"):
        if key in position:
            ref = position[key]
            if not isinstance(ref, str) or not ref.strip():
                return f"{key} must be a non-empty entity_id (string)."
            offset = position.get("offset")
            if not offset or not isinstance(offset, (list, tuple)) or len(offset) != 3:
                return f"{key} requires offset as [x, y, z] (3 numbers, meters)."
            if not all(isinstance(v, (int, float)) for v in offset):
                return f"offset values for {key} must be numbers."

    return None


# ═══════════════════════════════════════════════════════════════════════
# Facts safety — generic prose hygiene (does not depend on entity type)
# ═══════════════════════════════════════════════════════════════════════


def validate_facts(facts: str, entity_id: str = "") -> List[str]:
    """Check facts for patterns that suggest mechanical data in wrong field.

    Returns warnings (non-blocking). Detects:
    - GURPS stat references (ST, DX, IQ, HT, HP, FP, etc.)
    - Inventory keywords (carries, wields, wears)
    - State values (hp, fp, conditions, injuries)
    """
    warnings: List[str] = []
    if not facts:
        return warnings
    gurps_match = re.search(
        r'\b(ST|DX|IQ|HT|HP|FP|ER|Will|Per|Basic_Move|Basic_Speed|DR|Dodge)\s*[:\-]?\s*\d+',
        facts, re.IGNORECASE)
    if gurps_match:
        warnings.append(
            f"Facts contains GURPS stat references (e.g. '{gurps_match.group(0)}'). "
            f"Put mechanical stats in gurps={{...}} instead.")
    inv_match = re.search(r'\b(inventory|carries|wields|wears|holds)\s*[:]', facts, re.IGNORECASE)
    if inv_match:
        warnings.append(
            f"Facts contains inventory keyword '{inv_match.group(0)}'. "
            f"Put equipment in equipment={{...}} instead.")
    state_match = re.search(r'\b(hp|fp|conditions?|injured?)\s*[:\-]?\s*\d+', facts, re.IGNORECASE)
    if state_match:
        warnings.append(
            f"Facts contains state value '{state_match.group(0)}'. "
            f"Put dynamic state in state={{...}} or use adjust_entity().")
    return warnings


# ═══════════════════════════════════════════════════════════════════════
# Connections validation — location connectivity graph
# ═══════════════════════════════════════════════════════════════════════


def validate_connections(connections: Optional[Dict[str, Any]]) -> Optional[str]:
    """Check connections format. Returns error string or None if valid.

    connections is a dict mapping connection names to descriptors:
      {"to_kitchen": {
          "target": entity_id,               # REQUIRED
          "exit_offset": [x, y, z],          # REQUIRED, meters
          "blocks_line_of_sight": bool,      # default false
          "description": str,                # optional
      }}
    """
    if not connections:
        return None
    if not isinstance(connections, dict):
        return "connections must be a dict mapping names to descriptors."

    valid_conn_keys = {"target", "exit_offset", "blocks_line_of_sight", "description"}
    for name, conn in connections.items():
        if not isinstance(name, str) or not name.strip():
            return f"Connection name must be a non-empty string, got {name!r}."
        if not isinstance(conn, dict):
            return f"Connection '{name}' must be a dict."
        extra = set(conn.keys()) - valid_conn_keys
        if extra:
            return f"Connection '{name}' has unknown key(s): {', '.join(sorted(extra))}."
        if "target" not in conn:
            return f"Connection '{name}' missing required 'target' (entity_id)."
        target = conn["target"]
        if not isinstance(target, str) or not target.strip():
            return f"Connection '{name}'.target must be a non-empty entity_id."
        if "exit_offset" not in conn:
            return f"Connection '{name}' missing required 'exit_offset' [x, y, z]."
        offset = conn["exit_offset"]
        if not isinstance(offset, (list, tuple)) or len(offset) != 3:
            return f"Connection '{name}'.exit_offset must be [x, y, z] (3 numbers)."
        if not all(isinstance(v, (int, float)) for v in offset):
            return f"Connection '{name}'.exit_offset values must be numbers."
        if "blocks_line_of_sight" in conn:
            if not isinstance(conn["blocks_line_of_sight"], bool):
                return f"Connection '{name}'.blocks_line_of_sight must be true or false."
        if "description" in conn:
            if not isinstance(conn["description"], str):
                return f"Connection '{name}'.description must be a string."
    return None
