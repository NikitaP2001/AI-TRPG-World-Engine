"""JsonFileBackend — EntityBackend implementation backed by a single JSON file.

Storage: game/entity_store/entities.json (id-keyed dict in a single JSON file).
This is extracted from the original entity_store.py module-level functions.
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from world_manager._backend_base import EntityBackend
from world_manager.entity_validation import validate_facts as _validate_facts
from world.io import StorageLimitExceeded


# ---------------------------------------------------------------------------
# Generic storage helpers
# ---------------------------------------------------------------------------


def _lru_evict(items: List[Any], max_bytes: int) -> List[Any]:
    """Evict lowest-priority, oldest items until serialized size is under max_bytes.

    Priority rules (per item dict):
      - `priority` (int, default 1): lower = evicted first. priority >= 3 protected.
      - `last_used_tick` (int, default 0): lower (older) = evicted first.

    Returns the trimmed list. Evicted items are discarded (lossy).
    """
    while True:
        raw = json.dumps(items, ensure_ascii=False)
        if len(raw.encode("utf-8")) <= max_bytes:
            break
        candidates = [i for i in items if isinstance(i, dict) and i.get("priority", 1) < 3]
        if not candidates:
            break
        candidates.sort(key=lambda x: (x.get("priority", 1), x.get("last_used_tick", 0)))
        victim = candidates[0]
        items = [i for i in items if i is not victim]
    return items


def _soft_trim(value: Any, max_bytes: int) -> Any:
    """Trim a value to fit within max_bytes by truncating the serialized form.

    For strings: truncate at byte boundary.
    For dicts/lists: not trimmable — caller should handle.
    Returns trimmed value, or None if not trimmable.
    """
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    return None  # non-trivial types not auto-trimmed


# ---------------------------------------------------------------------------
# Field-config defaults (applied automatically, keyed by form, not by type)
# ---------------------------------------------------------------------------

# Default byte budgets applied when no _field_config exists on the entity's
# template. These are form-based (narrative text, stat block, growing list),
# not type-based — they apply regardless of type string.
#
# Player entities (determined by game/players/{id}/ folder existence) use
# the player defaults; all others use the generic defaults.
_PLAYER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "facts": {"budget": {"soft": 10240, "hard": 13312}},
    "state": {"budget": {"hard": 2048}},
    "skills": {"budget": {"hard": 4096}, "lru": {"budget_bytes": 2048}},
    "equipment": {"budget": {"hard": 2048}, "lru": {"budget_bytes": 1024}},
}

_GENERIC_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "facts": {"budget": {"soft": 4096, "hard": 6144}},
    "gurps": {"budget": {"hard": 1024}},
    "stats": {"budget": {"hard": 1024}},
    "skills": {"budget": {"hard": 1024}},  # no LRU by default
    "equipment": {"budget": {"hard": 1024}},  # no LRU by default
    "state": {"budget": {"hard": 1024}},
    "scripts": {"budget": {"count": 10}},
    "effects": {"budget": {"count": 10}},
}

# Storage-managed keys — managed by backend, not by user data
_STORAGE_KEYS = {"id", "type", "created_at", "updated_at", "deleted_at", "delete_reason",
                 "_field_config", "_stripped"}

_INVOCATION_LIMIT = 25
_invocation_counter: int = 0


# ---------------------------------------------------------------------------
# WorldTime helper — returns current game world time as string
# ---------------------------------------------------------------------------
def _world_time_str() -> str:
    """Return current WorldTime as string, or 'unknown' if unavailable."""
    try:
        from world import World
        wt = World().get_world_time()
        return wt.to_string() if hasattr(wt, 'to_string') else str(wt)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_3D_CACHE: Dict[str, List[float]] = {}


def _latlon_to_3d(lat: float, lon: float) -> List[float]:
    key = f"{lat:.4f}_{lon:.4f}"
    if key in _3D_CACHE:
        return _3D_CACHE[key]
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    v = [math.cos(lat_r) * math.cos(lon_r), math.sin(lat_r), math.cos(lat_r) * math.sin(lon_r)]
    _3D_CACHE[key] = v
    return v


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    a = max(0.0, min(1.0, a))
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_value(value: str) -> Any:
    """Parse a string value into Python type (int, float, bool, list, dict, or str)."""
    v = str(value).strip()
    if not v:
        return v
    # bool
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    # null
    if v.lower() in ("null", "none"):
        return None
    # int
    try:
        return int(v)
    except ValueError:
        pass
    # float
    try:
        return float(v)
    except ValueError:
        pass
    # list (comma-separated)
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if inner:
            return [_parse_value(x.strip()) for x in inner.split(",") if x.strip()]
        return []
    # dict (simple key=value)
    if v.startswith("{") and v.endswith("}"):
        inner = v[1:-1].strip()
        if inner:
            result = {}
            for pair in inner.split(","):
                pair = pair.strip()
                if "=" in pair:
                    k, val = pair.split("=", 1)
                    result[k.strip()] = _parse_value(val.strip())
            return result
        return {}
    return v


# ---------------------------------------------------------------------------
# JsonFileBackend
# ---------------------------------------------------------------------------


class JsonFileBackend(EntityBackend):
    """EntityBackend backed by a single JSON file.
    
    In-memory cache with atomic save via .tmp rename.
    """

    def __init__(self, path: Path = Path("game") / "entity_store" / "entities.json"):
        self._path = path
        self._dir = path.parent
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._dirty: bool = False
        self._id_counter: int = 0

    # ── Storage management ──

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Dict[str, Any]]:
        """Load all entities from disk into cache."""
        if self._cache:
            return self._cache
        self._ensure_dir()
        if self._path.exists():
            try:
                raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    self._cache = data
            except Exception:
                self._cache = {}
        else:
            self._cache = {}
        return self._cache

    def _save(self) -> None:
        self._ensure_dir()
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)
        self._dirty = False

    def _touch(self) -> None:
        self._dirty = True

    def _flush(self) -> None:
        if self._dirty:
            self._save()

    def _generate_id(self) -> str:
        self._id_counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"ent_{ts}_{self._id_counter:04d}"

    # ── Facts validation (delegated to entity_validation.py) ──

    def validate_facts(self, facts: str, entity_id: str = "") -> List[str]:
        """Check facts for patterns that suggest mechanical data in wrong field."""
        return _validate_facts(facts, entity_id)

    # ── Entity CRUD ──

    # ── Invocation rate limit ──

    @classmethod
    def reset_invocation_counter(cls) -> None:
        """Call at the start of each agent invocation."""
        global _invocation_counter
        _invocation_counter = 0

    @classmethod
    def _check_invocation_limit(cls) -> None:
        global _invocation_counter
        _invocation_counter += 1
        if _invocation_counter > _INVOCATION_LIMIT:
            cls.reset_invocation_counter()
            raise StorageLimitExceeded(
                f"Entity creation rate limit exceeded: max {_INVOCATION_LIMIT} "
                f"entities per invocation."
            )

    # ── base_type cycle detection ──

    @staticmethod
    def _detect_cycle(base_type: str, entity_id: str, store: Dict) -> None:
        """Raise StorageLimitExceeded if base_type creates a cycle."""
        if not base_type or base_type not in store:
            return
        visited = {entity_id}
        current = base_type
        while current:
            if current in visited:
                cycle = " → ".join(visited | {current})
                raise ValueError(
                    f"base_type inheritance cycle detected: {cycle}"
                )
            visited.add(current)
            tmpl = store.get(current)
            if not tmpl:
                break
            current = tmpl.get("base_type", "") or ""

    # ── Field-config helpers ──

    def _get_field_config(self, entity: Dict) -> Dict[str, Any]:
        """Resolve effective field config from entity's template chain."""
        config: Dict[str, Any] = {}
        # Determine if this is player-controlled (folder exists)
        from pathlib import Path as _P
        eid = entity.get("id", "")
        is_player = (_P("game") / "players" / eid).is_dir() if eid else False

        # Start with form-based defaults
        base = _PLAYER_DEFAULTS if is_player else _GENERIC_DEFAULTS
        for field, rules in base.items():
            config[field] = dict(rules)

        # Overlay from entity's own _field_config
        own_config = entity.get("_field_config", {}) or {}
        for field, rules in own_config.items():
            if field in config:
                config[field].update(rules)
            else:
                config[field] = dict(rules)

        # Walk base_type chain for inherited _field_config
        bt = entity.get("base_type", "")
        store = self.load()
        visited = set()
        while bt and bt in store and bt not in visited:
            visited.add(bt)
            tmpl = store[bt]
            tmpl_config = tmpl.get("_field_config", {}) or {}
            for field, rules in tmpl_config.items():
                if field in config:
                    config[field].update(rules)
                else:
                    config[field] = dict(rules)
            bt = tmpl.get("base_type", "") or ""
        return config

    def _enforce_budget(self, field: str, value: Any, cfg: Dict) -> Optional[str]:
        """Check and optionally trim field value against budget. Returns error or None."""
        budget = cfg.get("budget", {})
        if not budget:
            return None

        # Count-based budget
        count_budget = budget.get("count")
        if count_budget is not None and isinstance(value, (list, tuple)):
            if len(value) > count_budget:
                return f"Field '{field}' exceeds count limit of {count_budget}"

        # Byte budget
        hard = budget.get("hard")
        soft = budget.get("soft")
        if hard is None and soft is None:
            return None
        serialized = json.dumps(value, ensure_ascii=False)
        size = len(serialized.encode("utf-8"))

        if hard is not None and size > hard:
            return f"Field '{field}' exceeds hard limit of {hard} bytes ({size} bytes)"
        if soft is not None and size > soft:
            return None  # Will be soft-trimmed in caller
        return None

    # ── Entity CRUD ──

    def set(self, id: str = "", type: str = "", **record: Any) -> Dict[str, Any]:
        """Create or update an entity.

        The `record` dict is pre-filtered by entity_core to contain only
        non-empty values. Backend adds storage-level fields, enforces
        byte/LRU budgets via _field_config (or form-based defaults),
        handles base_type inheritance, and captures overflow into
        _stripped fallback.

        base_type: resolved before write to apply template field config.
        _field_config: if present on entity or template, overrides defaults.
        _stripped: overflow/narrative-strip fallback, merged back on get().
        """
        store = self.load()
        existing = store.get(id) if id else None
        is_new = not id or existing is None

        if not id:
            id = self._generate_id()

        # Rate limit check (creates only)
        if is_new:
            self._check_invocation_limit()

        fact_warnings = self.validate_facts(record.get("facts", ""), id)

        # cycle detection on base_type
        base_type = record.get("base_type", "")
        if base_type:
            self._detect_cycle(base_type, id, store)

        if is_new:
            entity = dict(record)
            entity["id"] = id
            entity["type"] = type or record.get("type", "")

            # base_type inheritance
            if base_type and base_type in store:
                tmpl = store[base_type]
                _skip_inherit = _STORAGE_KEYS | {"base_type", "id", "type"}
                for field in tmpl:
                    if field not in _skip_inherit:
                        if field not in entity or not entity[field]:
                            entity[field] = tmpl[field]
                for s in tmpl.get("scripts") or []:
                    existing_scripts = entity.setdefault("scripts", [])
                    if s not in existing_scripts:
                        existing_scripts.append(s)
                # Inherit _field_config from template
                tmpl_config = tmpl.get("_field_config")
                if tmpl_config and "_field_config" not in entity:
                    entity["_field_config"] = dict(tmpl_config)

            entity["created_at"] = _world_time_str()
            entity["updated_at"] = _world_time_str()
            entity["deleted_at"] = None
            store[id] = entity
            operation = "created"
        else:
            entity = store[id]
            # Merge: non-dict fields overwrite, dict fields update.
            # gurps is always fully replaced (not merged) to avoid key conflicts
            # like lowercase st/dx persisting alongside uppercase ST/DX.
            for k, v in record.items():
                if k in _STORAGE_KEYS:
                    continue  # backend-managed, not user data
                if k == "gurps":
                    entity[k] = v  # full replace, not merge
                elif isinstance(v, dict) and isinstance(entity.get(k), dict):
                    entity[k].update(v)
                else:
                    entity[k] = v
            entity["updated_at"] = _world_time_str()
            if entity.get("deleted_at"):
                del entity["deleted_at"]
            operation = "updated"

        # ── Apply field budgets (soft = trim with _stripped fallback) ──
        cfg = self._get_field_config(entity)
        stripped = entity.get("_stripped", {}) or {}
        for field, rules in cfg.items():
            if field not in entity:
                continue
            val = entity[field]

            # Count budget
            count_budget = rules.get("budget", {}).get("count")
            if count_budget is not None and isinstance(val, list):
                if len(val) > count_budget:
                    excess = val[count_budget:]
                    entity[field] = val[:count_budget]
                    if excess:
                        stripped.setdefault(field, []).extend(excess)

            # Byte budget — hard check (error) and soft check (trim)
            hard = rules.get("budget", {}).get("hard")
            soft = rules.get("budget", {}).get("soft")
            if hard or soft:
                serialized = json.dumps(val, ensure_ascii=False)
                size = len(serialized.encode("utf-8"))
                if hard and size > hard:
                    self._touch()
                    self._flush()
                    raise StorageLimitExceeded(
                        f"Field '{field}' exceeds hard limit of {hard} bytes "
                        f"({size} bytes)"
                    )
                if soft and size > soft:
                    # Trim: keep the first soft bytes, capture remainder
                    trimmed = _soft_trim(val, soft)
                    if trimmed is not None:
                        stripped_val = entity.get(field)
                        entity[field] = trimmed
                        stripped.setdefault(field, stripped_val)

            # LRU eviction
            lru_budget = rules.get("lru", {}).get("budget_bytes")
            if lru_budget is not None and isinstance(val, list):
                lru_items = _lru_evict(val, lru_budget)
                if len(lru_items) < len(val):
                    evicted = val[len(lru_items):]
                    entity[field] = lru_items
                    # LRU is lossy by default
                    if rules.get("lru", {}).get("fallback"):
                        stripped.setdefault(field, []).extend(evicted)

        if stripped:
            entity["_stripped"] = stripped

        self._touch()
        self._flush()

        inherited_from = record.get("base_type", "")
        inherited_scripts = 0
        if inherited_from and inherited_from in store:
            inherited_scripts = len(store[inherited_from].get("scripts") or [])

        result = {
            "entity_id": id, "operation": operation,
            "inherited_from": inherited_from,
            "tags_total": entity.get("tags", []),
            "scripts_inherited": inherited_scripts,
            "warnings": fact_warnings,
        }
        return result

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        store = self.load()
        entity = store.get(id)
        if entity is None or entity.get("deleted_at"):
            return None
        result = dict(entity)
        # Merge _stripped fallback: empty fields get their stripped value back
        stripped = result.pop("_stripped", None) or {}
        for field, fallback in stripped.items():
            if field in result and not result[field]:
                result[field] = fallback
            elif field not in result:
                result[field] = fallback
        return result

    def get_facts(self, id: str) -> Optional[str]:
        entity = self.get(id)
        if entity is None:
            return None
        return entity.get("facts", "")

    def set_facts(self, id: str, facts: str) -> Dict[str, Any]:
        store = self.load()
        entity = store.get(id)
        if entity is None or entity.get("deleted_at"):
            return {"entity_id": id, "operation": "error", "warnings": ["Entity not found"]}
        fact_warnings = self.validate_facts(facts, id)
        entity["facts"] = facts
        entity["updated_at"] = _world_time_str()
        self._touch()
        self._flush()
        result = {"entity_id": id, "operation": "facts_updated"}
        if fact_warnings:
            result["warnings"] = fact_warnings
        return result

    def get_state(self, id: str) -> Optional[Dict[str, Any]]:
        entity = self.get(id)
        if entity is None:
            return None
        return {
            "id": entity["id"],
            "gurps": entity.get("gurps", {}),
            "state": entity.get("state", {}),
            "skills": entity.get("skills", {}),
            "equipment": entity.get("equipment", {}),
            "stats": entity.get("stats", {}),
            "active_effects": entity.get("effects", []),
        }

    def delete(self, id: str, reason: str = "") -> Dict[str, Any]:
        store = self.load()
        entity = store.get(id)
        if entity is None:
            return {"entity_id": id, "status": "not_found"}
        entity["deleted_at"] = _world_time_str()
        entity["delete_reason"] = reason
        self._touch()
        self._flush()
        return {"entity_id": id, "status": "deleted"}

    # ── Query ──

    def _get_position_coords(self, position: dict) -> Optional[tuple]:
        if not position:
            return None
        coords = position.get("coords")
        if coords and len(coords) >= 2:
            return (float(coords[0]), float(coords[1]))
        inside = position.get("inside")
        if inside:
            parent = self.load().get(inside)
            if parent:
                return self._get_position_coords(parent.get("position", {}))
        near = position.get("near")
        if near:
            target = self.load().get(near)
            if target:
                return self._get_position_coords(target.get("position", {}))
        return None

    def _entity_matches_facts_search(self, entity: dict, query: str) -> bool:
        facts = (entity.get("facts") or "").lower()
        query_lower = query.strip().lower()
        if not query_lower:
            return True
        words = query_lower.split()
        return all(w in facts for w in words)

    def query(self, type_filter: str = "", base_type: str = "",
              tags: Optional[List[str]] = None,
              position: Optional[Dict[str, Any]] = None,
              radius: float = -1.0,
              facts_search: str = "",
              include_templates: bool = False,
              limit: int = 50) -> List[Dict[str, Any]]:
        store = self.load()
        results = []
        tag_set = set(tags or [])
        pos_coords = self._get_position_coords(position or {}) if position else None

        for entity in store.values():
            if entity.get("deleted_at"):
                continue
            if type_filter and entity.get("type", "") != type_filter:
                continue
            if base_type and entity.get("base_type", "") != base_type:
                continue
            if tag_set:
                entity_tags = set(entity.get("tags", []))
                if not tag_set.issubset(entity_tags):
                    continue
            if pos_coords and radius > 0:
                epos = entity.get("position", {})
                ecoords = self._get_position_coords(epos)
                if ecoords:
                    dist = _haversine_km(pos_coords[0], pos_coords[1], ecoords[0], ecoords[1])
                    if dist > radius:
                        continue
                else:
                    continue
            if facts_search and not self._entity_matches_facts_search(entity, facts_search):
                continue
            if not include_templates:
                etype = entity.get("type", "")
                if etype.endswith("_template") or etype.endswith("_type"):
                    continue

            results.append({
                "id": entity["id"],
                "type": entity.get("type", ""),
                "base_type": entity.get("base_type", ""),
                "facts": entity.get("facts", ""),
                "gurps": entity.get("gurps", {}),
                "state": entity.get("state", {}),
                "skills": entity.get("skills", {}),
                "equipment": entity.get("equipment", {}),
                "stats": entity.get("stats", {}),
                "tags": entity.get("tags", []),
                "position": entity.get("position", {}),
                "active_effects": entity.get("effects", []),
                "script_count": len(entity.get("scripts", [])),
            })
            if len(results) >= limit:
                break
        return results

    # ── Tags ──

    def add_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        store = self.load()
        entity = store.get(entity_id)
        if entity is None or entity.get("deleted_at"):
            return {"entity_id": entity_id, "tags_added": [], "tags_total": []}
        existing = set(entity.get("tags", []))
        for t in tags:
            existing.add(t)
        entity["tags"] = sorted(existing)
        self._touch()
        self._flush()
        return {"entity_id": entity_id, "tags_added": tags, "tags_total": entity["tags"]}

    def remove_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        store = self.load()
        entity = store.get(entity_id)
        if entity is None or entity.get("deleted_at"):
            return {"entity_id": entity_id, "tags_removed": [], "tags_total": []}
        existing = set(entity.get("tags", []))
        removed = []
        for t in tags:
            if t in existing:
                existing.remove(t)
                removed.append(t)
        entity["tags"] = sorted(existing)
        self._touch()
        self._flush()
        return {"entity_id": entity_id, "tags_removed": removed, "tags_total": entity["tags"]}

    # ── Effects ──

    def set_effect(self, entity_id: str, effect_id: str = "",
                   stat_deltas: Optional[Dict[str, Any]] = None,
                   per_tick: bool = True,
                   duration_ticks: int = -1,
                   description: str = "",
                   aura_radius: float = -1.0) -> Dict[str, Any]:
        store = self.load()
        entity = store.get(entity_id)
        if entity is None or entity.get("deleted_at"):
            return {"effect_id": "", "entity_id": entity_id, "expires_at_tick": None,
                    "warnings": ["Entity not found"]}
        eid = effect_id or uuid.uuid4().hex[:12]
        entry = {
            "effect_id": eid, "stat_deltas": stat_deltas or {},
            "per_tick": per_tick, "duration_ticks": duration_ticks,
            "description": description, "expires_at_tick": None,
            "aura_radius": aura_radius if aura_radius > 0 else 0,
            "created_at_tick": 0,  # filled by engine on first tick
        }
        effects = entity.setdefault("effects", [])
        for i, ef in enumerate(effects):
            if ef.get("effect_id") == eid:
                effects[i] = entry
                break
        else:
            effects.append(entry)
        self._touch()
        self._flush()
        return {"effect_id": eid, "entity_id": entity_id, "expires_at_tick": None, "warnings": []}

    def remove_effect(self, entity_id: str, effect_id: str) -> Dict[str, Any]:
        store = self.load()
        entity = store.get(entity_id)
        if entity is None or entity.get("deleted_at"):
            return {"entity_id": entity_id, "effect_id": effect_id, "status": "not_found"}
        effects = entity.get("effects", [])
        entity["effects"] = [ef for ef in effects if ef.get("effect_id") != effect_id]
        self._touch()
        self._flush()
        return {"entity_id": entity_id, "effect_id": effect_id, "status": "removed"}

    # ── Scripts ──

    def attach_script(self, entity_id: str, script_id: str = "",
                      trigger_type: str = "",
                      trigger_params: Optional[Dict[str, Any]] = None,
                      script: str = "", comment: str = "") -> Dict[str, Any]:
        store = self.load()
        entity = store.get(entity_id)
        if entity is None or entity.get("deleted_at"):
            return {"script_id": "", "entity_id": entity_id, "trigger_type": trigger_type,
                    "warnings": ["Entity not found"]}
        sid = script_id or uuid.uuid4().hex[:12]
        entry = {
            "script_id": sid, "trigger_type": trigger_type,
            "trigger_params": trigger_params or {},
            "script": script, "comment": comment,
        }
        scripts = entity.setdefault("scripts", [])
        for i, s in enumerate(scripts):
            if s.get("script_id") == sid:
                scripts[i] = entry
                break
        else:
            scripts.append(entry)
        self._touch()
        self._flush()
        return {"script_id": sid, "entity_id": entity_id, "trigger_type": trigger_type, "warnings": []}

    def detach_script(self, entity_id: str, script_id: str) -> Dict[str, Any]:
        store = self.load()
        entity = store.get(entity_id)
        if entity is None or entity.get("deleted_at"):
            return {"entity_id": entity_id, "script_id": script_id, "status": "not_found"}
        scripts = entity.get("scripts", [])
        entity["scripts"] = [s for s in scripts if s.get("script_id") != script_id]
        self._touch()
        self._flush()
        return {"entity_id": entity_id, "script_id": script_id, "status": "removed"}

    # ── adjust_entity ──

    def adjust(self, entity_id: str, *,
               hp: Optional[int] = None,
               fp: Optional[int] = None,
               er: Optional[int] = None,
               add_condition: str = "",
               remove_condition: str = "",
               add_injury: str = "",
               remove_injury: str = "",
               add_item: str = "",
               remove_item: str = "",
               currency_delta: Optional[Dict[str, int]] = None,
               add_ability: str = "",
               remove_ability: str = "",
               add_skill: str = "",
               remove_skill: str = "",
               current_state: str = "",
               gurps: Optional[Dict[str, Any]] = None,
               position: Optional[Dict[str, Any]] = None,
               connections: Optional[Dict[str, Any]] = None,
               skills: Optional[Dict[str, Any]] = None,
               equipment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        store = self.load()
        entity = store.get(entity_id)
        if entity is None or entity.get("deleted_at"):
            return {"entity_id": entity_id, "error": "Entity not found"}

        mutations: List[str] = []

        state_block = entity.setdefault("state", {})
        if hp is not None:
            old = state_block.get("hp")
            state_block["hp"] = max(0, hp)
            mutations.append(f"hp: {old} -> {hp}")
        if fp is not None:
            old = state_block.get("fp")
            state_block["fp"] = max(0, fp)
            mutations.append(f"fp: {old} -> {fp}")
        if er is not None:
            old = state_block.get("er")
            state_block["er"] = max(0, er)
            mutations.append(f"er: {old} -> {er}")

        if add_condition:
            conds = state_block.setdefault("conditions", [])
            if add_condition not in conds:
                conds.append(add_condition)
                mutations.append(f"+condition: {add_condition}")
        if remove_condition:
            conds = state_block.setdefault("conditions", [])
            if remove_condition in conds:
                conds.remove(remove_condition)
                mutations.append(f"-condition: {remove_condition}")

        if add_injury:
            injs = state_block.setdefault("injuries", [])
            if add_injury not in injs:
                injs.append(add_injury)
                mutations.append(f"+injury: {add_injury}")
        if remove_injury:
            injs = state_block.setdefault("injuries", [])
            if remove_injury in injs:
                injs.remove(remove_injury)
                mutations.append(f"-injury: {remove_injury}")

        equip_block = entity.setdefault("equipment", {})
        # Position: full replace (not merge) to avoid contradictory combinations
        if position is not None:
            entity["position"] = position
            mutations.append(f"position: {json.dumps(position, ensure_ascii=False)}")

        # Connections: full replace
        if connections is not None:
            from world_manager.entity_validation import validate_connections
            err = validate_connections(connections)
            if err:
                return {"entity_id": entity_id, "error": f"Invalid connections: {err}"}
            entity["connections"] = connections
            mutations.append(f"connections: {len(connections)} link(s)")

        # GURPS block: full replace
        if gurps is not None:
            entity["gurps"] = gurps
            mutations.append(f"gurps: {len(gurps)} entries")

        # Skills block: full replace
        if skills is not None:
            entity["skills"] = skills
            mutations.append(f"skills: {len(skills)} entries")

        # Equipment block: full replace
        if equipment is not None:
            entity["equipment"] = equipment
            mutations.append(f"equipment: {len(equipment)} entries")

        if add_item:
            inv = equip_block.setdefault("inventory", [])
            if add_item not in inv:
                inv.append(add_item)
                mutations.append(f"+item: {add_item}")
        if remove_item:
            inv = equip_block.setdefault("inventory", [])
            if remove_item in inv:
                inv.remove(remove_item)
                mutations.append(f"-item: {remove_item}")

        if currency_delta:
            currency = equip_block.setdefault("currency", {})
            for coin, delta in currency_delta.items():
                old_val = currency.get(coin, 0)
                new_val = max(0, old_val + delta)
                currency[coin] = new_val
                mutations.append(f"currency.{coin}: {old_val} -> {new_val}")

        skills_block = entity.setdefault("skills", {})
        if add_ability:
            abils = skills_block.setdefault("passive_abilities", [])
            if add_ability not in abils:
                abils.append(add_ability)
                mutations.append(f"+ability: {add_ability}")
        if remove_ability:
            abils = skills_block.setdefault("passive_abilities", [])
            if remove_ability in abils:
                abils.remove(remove_ability)
                mutations.append(f"-ability: {remove_ability}")

        if add_skill:
            cached = skills_block.setdefault("cached_skills", [])
            if add_skill not in cached:
                cached.append(add_skill)
                mutations.append(f"+skill: {add_skill}")
        if remove_skill:
            cached = skills_block.setdefault("cached_skills", [])
            if remove_skill in cached:
                cached.remove(remove_skill)
                mutations.append(f"-skill: {remove_skill}")

        if current_state:
            state_block["current_state"] = current_state
            mutations.append(f"current_state: {current_state}")

        entity["updated_at"] = _world_time_str()
        self._touch()
        self._flush()

        sb = entity.get("state", {})
        return {
            "entity_id": entity_id,
            "mutations_applied": mutations,
            "state_summary": {
                "hp": sb.get("hp"), "fp": sb.get("fp"),
                "conditions": len(sb.get("conditions", [])),
                "injuries": len(sb.get("injuries", [])),
            },
        }
