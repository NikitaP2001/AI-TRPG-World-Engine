"""Effect Engine — applies per_tick stat deltas, expires effects, handles auras.

Effects are stored on entity records as:
  {"effect_id", "stat_deltas": {"HP": -2}, "per_tick": true,
   "duration_ticks": 5, "description": "bleeding", "expires_at_tick": null}

This engine reads ALL entities from the backend, processes their effects,
and writes back changes. Called once per world tick.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from world_manager._json_backend import JsonFileBackend


def process_entity_effects(
    entity: Dict[str, Any],
    current_tick: int,
    world_time: int = 0,
) -> List[Dict[str, Any]]:
    """Process all active effects on a single entity.

    Args:
        entity: Entity dict (mutated in place if effects apply).
        current_tick: Current world tick number.
        world_time: Current world time (unused, for future expansion).

    Returns:
        List of effect application records:
        [{"effect_id": "...", "applied": {"hp": -2}, "expired": False}, ...]
    """
    effects = entity.get("effects", [])
    if not effects:
        return []

    results: List[Dict[str, Any]] = []
    active: List[Dict[str, Any]] = []

    for effect in effects:
        effect_id = effect.get("effect_id", "")
        stat_deltas = effect.get("stat_deltas", {}) or {}
        per_tick = bool(effect.get("per_tick", True))
        duration_ticks = int(effect.get("duration_ticks", -1))
        created_at_tick = int(effect.get("created_at_tick", 0))

        # Initialize created_at_tick on first encounter
        if created_at_tick == 0:
            created_at_tick = current_tick
            effect["created_at_tick"] = created_at_tick

        # Compute expiry
        expires_at = effect.get("expires_at_tick")
        if expires_at is None and duration_ticks > 0:
            expires_at = created_at_tick + duration_ticks
            effect["expires_at_tick"] = expires_at

        # Check expiry
        if expires_at is not None and current_tick >= expires_at:
            results.append({
                "effect_id": effect_id,
                "applied": {},
                "expired": True,
                "description": effect.get("description", ""),
            })
            continue  # Skip this effect — it's expired

        # Apply per_tick deltas
        applied: Dict[str, Any] = {}
        if per_tick and stat_deltas:
            for stat_key, delta in stat_deltas.items():
                # Check gurps first, then state, then stats
                if stat_key in entity.get("gurps", {}):
                    old = entity["gurps"].get(stat_key, 0)
                    entity["gurps"][stat_key] = old + delta
                    applied[stat_key] = delta
                elif stat_key in entity.get("state", {}):
                    old = entity["state"].get(stat_key, 0)
                    new_val = max(0, old + delta)
                    entity["state"][stat_key] = new_val
                    applied[stat_key] = new_val - old
                elif stat_key in entity.get("stats", {}):
                    old = entity["stats"].get(stat_key, 0)
                    new_val = max(0, old + delta)
                    entity["stats"][stat_key] = new_val
                    applied[stat_key] = new_val - old
                else:
                    # Unknown stat — store in stats
                    entity.setdefault("stats", {})[stat_key] = max(0, delta)
                    applied[stat_key] = delta

        if applied or per_tick:
            results.append({
                "effect_id": effect_id,
                "applied": applied,
                "expired": False,
                "description": effect.get("description", ""),
            })

        # Keep this effect for next tick (if not expired and not one-shot)
        if not (expires_at is not None and current_tick >= expires_at - 1):
            # If this is the last tick before expiry, keep it so expiry is
            # detected next tick. Otherwise keep it.
            active.append(effect)

    entity["effects"] = active
    return results


def process_all_entities(
    current_tick: int,
    world_time: int = 0,
    backend: Optional[JsonFileBackend] = None,
) -> Dict[str, Any]:
    """Process effects on ALL entities in the JSON backend.

    Args:
        current_tick: Current world tick number.
        world_time: Current world time.
        backend: JsonFileBackend instance. If None, creates default.

    Returns:
        Summary dict with total_entities, effects_applied, effects_expired.
    """
    if backend is None:
        backend = JsonFileBackend()

    store = backend.load()
    total_applied = 0
    total_expired = 0
    total_aura_applications = 0
    changed = False

    for entity_id, entity in store.items():
        if entity.get("deleted_at"):
            continue

        # Process own effects
        results = process_entity_effects(entity, current_tick, world_time)
        for r in results:
            if r.get("expired"):
                total_expired += 1
            elif r.get("applied"):
                total_applied += 1

        # Process aura effects (aura_radius > 0)
        for effect in list(entity.get("effects", [])):
            aura_radius = effect.get("aura_radius", 0)
            if aura_radius and aura_radius > 0:
                # Find nearby entities and apply the stat_deltas to them
                stat_deltas = effect.get("stat_deltas", {}) or {}
                if not stat_deltas:
                    continue
                for other_id, other_entity in store.items():
                    if other_id == entity_id or other_entity.get("deleted_at"):
                        continue
                    # Simple proximity: check if both have position.coords
                    epos = entity.get("position", {})
                    opos = other_entity.get("position", {})
                    ecoords = epos.get("coords")
                    ocoords = opos.get("coords")
                    if ecoords and ocoords and len(ecoords) >= 2 and len(ocoords) >= 2:
                        dist = ((ecoords[0] - ocoords[0])**2 +
                                (ecoords[1] - ocoords[1])**2) ** 0.5
                        if dist <= aura_radius:
                            for sk, sd in stat_deltas.items():
                                if sk in other_entity.get("state", {}):
                                    old = other_entity["state"].get(sk, 0)
                                    other_entity["state"][sk] = max(0, old + sd)
                                    total_aura_applications += 1

        if results:
            changed = True

    if changed:
        backend._touch()
        backend._flush()

    return {
        "tick": current_tick,
        "entities_scanned": len(store),
        "effects_applied": total_applied,
        "effects_expired": total_expired,
        "aura_applications": total_aura_applications,
    }
