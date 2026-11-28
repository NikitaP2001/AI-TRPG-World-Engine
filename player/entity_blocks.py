"""A character's own sheet, carried as records that name their version.

The stats change during play — a wound, coin spent, gear dropped, a rewritten
fact — and the character has to know them. Every block that changed is written
into his own history as a record whose first line names the block and the
version of its content:

    [entity:Marek Stonehand/state@v:9f3c1a04]
    { ... }

The version is the content's own hash, so the rule stays exact rather than
clever — and the rule itself belongs to the pipeline, which owns entering
records: a record is written when its content differs from what stands there, and
is not written when it does not. Nothing here compares files or rewrites them,
and nothing is taken out: a block that changed earlier in the session keeps the
record it had, and falls out with a trim like every other record.

So the sheet is never duplicated, and a trim that removes the current record is
repaired at the next sync. The narrative description is not here at all: it does
does not change, so it is pinned above the history instead.

Run-dir dependent: reads the entity store through ``entity_core``.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: What a character is told about himself. Anything else in the entity record is
#: for the world's own machinery, not for him.
BLOCKS: Tuple[str, ...] = ("facts", "gurps", "state", "skills", "equipment")


def canonical(value: Any) -> str:
    """One text for one content, whatever order the keys arrived in."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def block_version(value: Any) -> str:
    """Same content, same version; different content, different version."""
    return hashlib.sha1(canonical(value).encode("utf-8")).hexdigest()[:8]


def render(value: Any) -> str:
    return value.strip() if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def marker(character_name: str, block: str, value: Any) -> str:
    return f"[entity:{character_name}/{block}@v:{block_version(value)}]"


def current_sheet(character_name: str) -> Dict[str, Any]:
    """What the world currently says this character is, block by block."""
    try:
        from world_manager.entity_core import get_entity
        entity = get_entity(character_name)
    except Exception:
        return {}
    if not isinstance(entity, dict):
        return {}
    sheet: Dict[str, Any] = {}
    for block in BLOCKS:
        value = entity.get(block)
        if value is None or value == "" or value == {} or value == []:
            continue
        sheet[block] = value
    return sheet


def sync_character_sheet(
    character_name: str,
    history_path: Path,
    limits: Any = None,
) -> Dict[str, Any]:
    """Write every block of his sheet that is not already what stands there.

    The pipeline does the work the version is for: a block whose content is what
    already stands in his memory is not written again, so the sheet follows the
    world without being restated every turn, and a block that was trimmed away is
    repaired at the next sync.

    Nothing is taken out. A block that changed during the session keeps the
    record it had, and a block the world no longer carries simply stops being
    written — what leaves his memory is what a trim takes.

    Returns what was done, so a caller can tell a quiet turn from a written one:
    ``{"written": [...], "dropped": N}`` (``dropped`` counts debris the pipeline
    cleared).
    """
    from history_pipeline import memory_at

    hist = memory_at(history_path, limits=limits)
    sheet = current_sheet(character_name)

    written: List[str] = []
    dropped = 0
    for block in BLOCKS:
        value = sheet.get(block)
        if value is None:
            continue
        # The block says what it is in its envelope — which fact it is a version
        # of, and of what kind — so the code need not read it back off the front
        # of the text.
        result = hist.record(f"{marker(character_name, block, value)}\n{render(value)}",
                             kind="sheet_block",
                             key=f"[entity:{character_name}/{block}]")
        if result.written:
            written.append(block)
        dropped += result.replaced + result.debris_dropped

    return {"written": written, "dropped": dropped}
