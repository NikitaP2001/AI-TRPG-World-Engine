from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_root(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve()


def _game_path(repo_root: Path) -> Path:
    return (repo_root / "game").resolve()


def _store_root(repo_root: Path) -> Path:
    return (repo_root / "backups" / "storylines").resolve()


def _lines_root(repo_root: Path) -> Path:
    return (_store_root(repo_root) / "lines").resolve()


def _index_path(repo_root: Path) -> Path:
    return (_store_root(repo_root) / "index.json").resolve()


def _default_index() -> Dict[str, Any]:
    return {
        "version": 1,
        "current_line_id": "",
        "lines": [],
    }


def _load_index(repo_root: Path) -> Dict[str, Any]:
    idx_path = _index_path(repo_root)
    if not idx_path.exists():
        return _default_index()
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return _default_index()
    if not isinstance(data, dict):
        return _default_index()
    if not isinstance(data.get("lines"), list):
        data["lines"] = []
    if not isinstance(data.get("current_line_id"), str):
        data["current_line_id"] = ""
    return data


def _save_index(repo_root: Path, index: Dict[str, Any]) -> None:
    root = _store_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    idx_path = _index_path(repo_root)
    tmp = idx_path.with_suffix(idx_path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(idx_path)


def _line_by_id(index: Dict[str, Any], line_id: str) -> Optional[Dict[str, Any]]:
    for line in index.get("lines") or []:
        if isinstance(line, dict) and str(line.get("id") or "") == str(line_id or ""):
            return line
    return None


def _make_line_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"line_{ts}_{uuid4().hex[:8]}"


def _safe_line_name(name: str) -> str:
    value = str(name or "").strip()
    if value:
        return value[:80]
    return "Story line"


def _line_snapshot_root(repo_root: Path, line_id: str) -> Path:
    return (_lines_root(repo_root) / line_id / "snapshots").resolve()


def _copy_game_to(snapshot_dir: Path, game_path: Path) -> None:
    if snapshot_dir.exists():
        raise RuntimeError(f"Snapshot already exists: {snapshot_dir}")
    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(game_path, snapshot_dir)


def _restore_game_from(snapshot_dir: Path, game_path: Path) -> None:
    if not snapshot_dir.exists():
        raise RuntimeError("Snapshot not found")

    if game_path.exists():
        shutil.rmtree(game_path)
    shutil.copytree(snapshot_dir, game_path)


def _line_summary(line: Dict[str, Any]) -> Dict[str, Any]:
    turns = line.get("turns") if isinstance(line.get("turns"), list) else []
    last_turn = turns[-1] if turns else None
    return {
        "id": str(line.get("id") or ""),
        "name": str(line.get("name") or "Story line"),
        "created_at": str(line.get("created_at") or ""),
        "updated_at": str(line.get("updated_at") or ""),
        "turn_count": len(turns),
        "last_turn": last_turn if isinstance(last_turn, dict) else None,
        "turns": turns,
    }


def ensure_storyline_store(repo_root: str | Path) -> Dict[str, Any]:
    root = _repo_root(repo_root)
    index = _load_index(root)

    lines = index.get("lines") if isinstance(index.get("lines"), list) else []
    if not lines:
        line_id = _make_line_id()
        line = {
            "id": line_id,
            "name": "Main story",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "turns": [],
        }
        index["lines"] = [line]
        index["current_line_id"] = line_id
        _save_index(root, index)
        return index

    current_id = str(index.get("current_line_id") or "")
    if not current_id or _line_by_id(index, current_id) is None:
        first_id = str(lines[0].get("id") or "") if isinstance(lines[0], dict) else ""
        index["current_line_id"] = first_id
        _save_index(root, index)

    return index


def list_story_lines(repo_root: str | Path) -> Dict[str, Any]:
    root = _repo_root(repo_root)
    index = ensure_storyline_store(root)
    out_lines: List[Dict[str, Any]] = []
    for line in index.get("lines") or []:
        if not isinstance(line, dict):
            continue
        out_lines.append(_line_summary(line))

    out_lines.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return {
        "current_line_id": str(index.get("current_line_id") or ""),
        "lines": out_lines,
    }


def create_story_line(repo_root: str | Path, *, name: str = "") -> Dict[str, Any]:
    root = _repo_root(repo_root)
    index = ensure_storyline_store(root)

    line_id = _make_line_id()
    line = {
        "id": line_id,
        "name": _safe_line_name(name),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "turns": [],
    }
    index.setdefault("lines", []).append(line)
    index["current_line_id"] = line_id

    # Seed with current game state so switching to the new line is immediate.
    game_path = _game_path(root)
    if game_path.exists():
        turn_id = f"seed_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        snapshot_rel = Path("lines") / line_id / "snapshots" / turn_id
        snapshot_abs = (_store_root(root) / snapshot_rel).resolve()
        _copy_game_to(snapshot_abs, game_path)
        line["turns"].append(
            {
                "id": turn_id,
                "label": "Initial state",
                "start_time": "",
                "end_time": "",
                "location": "",
                "created_at": _now_iso(),
                "snapshot": snapshot_rel.as_posix(),
            }
        )
        line["updated_at"] = _now_iso()

    _save_index(root, index)
    return _line_summary(line)


def record_turn_snapshot(
    repo_root: str | Path,
    *,
    start_time: str,
    end_time: str,
    location: str,
) -> Optional[Dict[str, Any]]:
    root = _repo_root(repo_root)
    index = ensure_storyline_store(root)

    current_id = str(index.get("current_line_id") or "")
    line = _line_by_id(index, current_id)
    if line is None:
        return None

    turns = line.get("turns") if isinstance(line.get("turns"), list) else []
    for t in turns:
        if not isinstance(t, dict):
            continue
        if (
            str(t.get("start_time") or "") == str(start_time or "")
            and str(t.get("end_time") or "") == str(end_time or "")
            and str(t.get("location") or "") == str(location or "")
        ):
            return t

    game_path = _game_path(root)
    if not game_path.exists():
        return None

    seq = len(turns) + 1
    turn_id = f"turn_{seq:06d}_{uuid4().hex[:6]}"
    snapshot_rel = Path("lines") / current_id / "snapshots" / turn_id
    snapshot_abs = (_store_root(root) / snapshot_rel).resolve()
    _copy_game_to(snapshot_abs, game_path)

    label = f"{start_time} → {end_time} @ {location}".strip()
    entry = {
        "id": turn_id,
        "label": label,
        "start_time": str(start_time or ""),
        "end_time": str(end_time or ""),
        "location": str(location or ""),
        "created_at": _now_iso(),
        "snapshot": snapshot_rel.as_posix(),
    }
    turns.append(entry)
    line["turns"] = turns
    line["updated_at"] = _now_iso()

    _save_index(root, index)
    return entry


def switch_story_line(repo_root: str | Path, *, line_id: str) -> Tuple[bool, str]:
    root = _repo_root(repo_root)
    index = ensure_storyline_store(root)
    line = _line_by_id(index, line_id)
    if line is None:
        return False, "Story line not found"

    turns = line.get("turns") if isinstance(line.get("turns"), list) else []
    if turns:
        last = turns[-1] if isinstance(turns[-1], dict) else None
        if isinstance(last, dict):
            snap_rel = str(last.get("snapshot") or "")
            if snap_rel:
                snap_abs = (_store_root(root) / snap_rel).resolve()
                _restore_game_from(snap_abs, _game_path(root))

    index["current_line_id"] = str(line_id or "")
    _save_index(root, index)
    return True, f"Switched to '{str(line.get('name') or line_id)}'"


def checkout_turn_by_key(
    repo_root: str | Path,
    *,
    start_time: str,
    end_time: str,
    location: str,
    drop_after: bool = True,
) -> Tuple[bool, str]:
    root = _repo_root(repo_root)
    index = ensure_storyline_store(root)
    current_id = str(index.get("current_line_id") or "")
    line = _line_by_id(index, current_id)
    if line is None:
        return False, "No active story line"

    turns = line.get("turns") if isinstance(line.get("turns"), list) else []
    target_idx = -1
    target_turn: Optional[Dict[str, Any]] = None
    fallback_idx = -1
    fallback_turn: Optional[Dict[str, Any]] = None
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        t_start = str(turn.get("start_time") or "")
        t_end = str(turn.get("end_time") or "")
        t_loc = str(turn.get("location") or "")

        if (
            t_start == str(start_time or "")
            and t_end == str(end_time or "")
            and t_loc == str(location or "")
        ):
            target_idx = i
            target_turn = turn
            break

        # Backward compatibility: older snapshots were saved with empty start_time.
        # If so, allow matching by (end_time, location).
        if (
            not t_start.strip()
            and t_end == str(end_time or "")
            and t_loc == str(location or "")
            and fallback_turn is None
        ):
            fallback_idx = i
            fallback_turn = turn

    if target_turn is None:
        if fallback_turn is not None:
            target_idx = fallback_idx
            target_turn = fallback_turn
        else:
            return False, "Turn snapshot not found in active story line"

    snap_rel = str(target_turn.get("snapshot") or "")
    if not snap_rel:
        return False, "Target turn has no snapshot"

    snap_abs = (_store_root(root) / snap_rel).resolve()
    _restore_game_from(snap_abs, _game_path(root))

    if drop_after and target_idx >= 0:
        for turn in turns[target_idx + 1 :]:
            if not isinstance(turn, dict):
                continue
            rel = str(turn.get("snapshot") or "")
            if not rel:
                continue
            p = (_store_root(root) / rel).resolve()
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        line["turns"] = turns[: target_idx + 1]

    line["updated_at"] = _now_iso()
    _save_index(root, index)
    return True, "Checked in to selected turn"


def delete_story_line(repo_root: str | Path, *, line_id: str) -> Tuple[bool, str]:
    root = _repo_root(repo_root)
    index = ensure_storyline_store(root)

    lines = [ln for ln in (index.get("lines") or []) if isinstance(ln, dict)]
    if len(lines) <= 1:
        return False, "Cannot delete the last story line"

    line = _line_by_id(index, line_id)
    if line is None:
        return False, "Story line not found"

    index["lines"] = [ln for ln in lines if str(ln.get("id") or "") != str(line_id or "")]

    line_dir = (_lines_root(root) / str(line_id or "")).resolve()
    if line_dir.exists():
        shutil.rmtree(line_dir, ignore_errors=True)

    current_was_deleted = str(index.get("current_line_id") or "") == str(line_id or "")
    replacement_id = ""
    if current_was_deleted:
        replacement = index["lines"][0] if index["lines"] else {}
        replacement_id = str(replacement.get("id") or "")
        index["current_line_id"] = replacement_id

    _save_index(root, index)

    if current_was_deleted and replacement_id:
        switch_story_line(root, line_id=replacement_id)
        return True, "Story line deleted and switched to another line"

    return True, "Story line deleted"
