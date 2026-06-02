from __future__ import annotations

import json
import shutil
from pathlib import Path

from world import World, _json_error_snippet


def _validate_json_file(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        snippet = _json_error_snippet(text=raw, lineno=int(getattr(e, "lineno", 1) or 1))
        raise ValueError(
            "Malformed JSON in initial seed file (init/).\n"
            f"File: {path}\n"
            f"Error: {e.msg} (line {e.lineno}, column {e.colno})\n"
            "Common causes: trailing commas, comments, unquoted keys/strings.\n"
            "Context:\n"
            f"{snippet}"
        ) from e


def initialize_game_dir(*, init_root: str = "init", game_root: str = "game") -> None:
    """Create game/ and seed game/characters from init/characters.

    Copies any .json files found under init/characters/<Character>/ into
    game/characters/<Character>/ (same filenames). Existing files are NOT overwritten.
    """

    workspace = Path(__file__).resolve().parent
    init_path = (workspace / init_root).resolve()
    game_path = (workspace / game_root).resolve()

    # Ensure base folders exist.
    (game_path / "characters").mkdir(parents=True, exist_ok=True)

    init_chars = init_path / "characters"
    if not init_chars.exists() or not init_chars.is_dir():
        # Nothing to seed.
        return

    for char_dir in sorted([p for p in init_chars.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        src = char_dir
        dst = game_path / "characters" / char_dir.name
        dst.mkdir(parents=True, exist_ok=True)

        for json_file in src.glob("*.json"):
            target = dst / json_file.name
            if not target.exists():
                _validate_json_file(json_file)
                shutil.copy2(json_file, target)

    # Initialize world state files (idempotent).
    World(workspace_root=workspace).ensure_initialized()
