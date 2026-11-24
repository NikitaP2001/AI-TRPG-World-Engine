"""Effect Engine — lands an effect's change once per its period, until it ends.

An effect is stored on an entity as:
  {"effect_id", "stat_deltas": {"hp": -1}, "period": 3600, "duration": 21600,
   "started_at": <world seconds>, "applied_at": <world seconds>,
   "expires_at": <world seconds or null>, "description": "bleeding"}

``period`` is the time between applications and ``duration`` how long the effect
lasts; both are seconds of world time, and period 0 means a change that lands
once, now. World time moves in jumps, so a pass lands **every period that has
elapsed** since the last one, in a single write: an effect left running for a day
lands a day's worth at once. That is the whole of "it kept going while nobody was
looking".

The engine reads every entity, lands what is due, and writes back what changed.
The cycle calls it once per pass, with the world time the cycle has reached.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from world_manager._json_backend import JsonFileBackend


def _apply_deltas(
    entity: Dict[str, Any],
    deltas: Dict[str, Any],
    times: int,
) -> Dict[str, Any]:
    """Move each stat by its delta taken *times* over, in one write.

    A day's worth of bleeding landing at once is the same arithmetic as landing
    it hour by hour, except that a limit — a stat that cannot go below zero — is
    met once instead of at every step. Nothing reads the moment a limit was met,
    so the aggregate is not the lesser account: it is the whole of it.
    """
    applied: Dict[str, Any] = {}
    for stat_key, delta in (deltas or {}).items():
        try:
            total = delta * times
        except TypeError:
            continue
        if stat_key in entity.get("gurps", {}):
            old = entity["gurps"].get(stat_key, 0)
            entity["gurps"][stat_key] = old + total
            applied[stat_key] = total
        elif stat_key in entity.get("state", {}):
            old = entity["state"].get(stat_key, 0)
            new_val = max(0, old + total)
            entity["state"][stat_key] = new_val
            applied[stat_key] = new_val - old
        elif stat_key in entity.get("stats", {}):
            old = entity["stats"].get(stat_key, 0)
            new_val = max(0, old + total)
            entity["stats"][stat_key] = new_val
            applied[stat_key] = new_val - old
        else:
            entity.setdefault("stats", {})[stat_key] = max(0, total)
            applied[stat_key] = total
    return applied


def process_entity_effects(
    entity: Dict[str, Any],
    now: int,
    world_time: int = 0,
) -> List[Dict[str, Any]]:
    """Land every application that is due on one entity.

    Args:
        entity: Entity dict (mutated in place where effects land).
        now: World time now, in seconds.
        world_time: Unused; kept so every caller passes the same pair.

    Returns:
        One entry per effect that acted or ended:
        [{"effect_id", "applied": {"hp": -24}, "times": 24, "expired": False}]
    """
    effects = entity.get("effects", [])
    if not effects:
        return []

    results: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []

    for effect in effects:
        effect_id = effect.get("effect_id", "")
        deltas = effect.get("stat_deltas", {}) or {}
        period = int(effect.get("period") or 0)
        started_at = int(effect.get("started_at") or 0) or now
        effect["started_at"] = started_at
        applied_at = int(effect.get("applied_at") or started_at)
        effect["applied_at"] = applied_at
        description = effect.get("description", "")

        expires_at = effect.get("expires_at")
        # An effect cannot act past its own end, whatever the world reached.
        until = now if expires_at is None else min(now, int(expires_at))
        expired = expires_at is not None and now >= int(expires_at)

        if period <= 0:
            # No period: the change lands once and the effect has done its work.
            results.append({
                "effect_id": effect_id,
                "applied": _apply_deltas(entity, deltas, 1),
                "times": 1, "expired": expired, "description": description,
            })
            continue

        times = (until - applied_at) // period
        if times > 0:
            results.append({
                "effect_id": effect_id,
                "applied": _apply_deltas(entity, deltas, times),
                "times": times, "expired": False, "description": description,
            })
            effect["applied_at"] = applied_at + times * period

        if expired:
            results.append({
                "effect_id": effect_id, "applied": {}, "times": 0,
                "expired": True, "description": description,
            })
            continue
        kept.append(effect)

    entity["effects"] = kept
    return results


def process_all_entities(
    now: int,
    world_time: int = 0,
    backend: Optional[JsonFileBackend] = None,
) -> Dict[str, Any]:
    """Land what is due on every entity in the JSON backend.

    Args:
        now: World time now, in seconds.
        world_time: Unused; kept so every caller passes the same pair.
        backend: JsonFileBackend instance. If None, creates default.

    Returns:
        Summary dict with entities_scanned, effects_applied, effects_expired.
    """
    if backend is None:
        backend = JsonFileBackend()

    store = backend.load()
    total_applied = 0
    total_expired = 0
    periods_landed = 0
    changed = False

    for entity_id, entity in store.items():
        if entity.get("deleted_at"):
            continue

        results = process_entity_effects(entity, now, world_time)
        for r in results:
            if r.get("expired"):
                total_expired += 1
            elif r.get("applied"):
                total_applied += 1
                periods_landed += int(r.get("times") or 0)

        if results:
            changed = True

    if changed:
        backend._touch()
        backend._flush()

    return {
        "world_time": now,
        "entities_scanned": len(store),
        "effects_applied": total_applied,
        "periods_landed": periods_landed,
        "effects_expired": total_expired,
    }
