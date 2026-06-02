from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional


_THREAD_LOCK = threading.RLock()
_THREAD_HELD_COUNTS: dict[str, int] = {}


def _lock_path_for(path: Path) -> Path:
    p = Path(path)
    return p.with_suffix(p.suffix + ".lock")


def _lock_timeout_seconds() -> float:
    raw = (os.getenv("LLM_WORLD_FILE_LOCK_TIMEOUT") or "").strip()
    if not raw:
        return 30.0
    try:
        return max(1.0, float(raw))
    except Exception:
        return 30.0


def _lock_stale_seconds() -> float:
    raw = (os.getenv("LLM_WORLD_FILE_LOCK_STALE_SECONDS") or "").strip()
    if not raw:
        return 600.0
    try:
        return max(5.0, float(raw))
    except Exception:
        return 600.0


@contextmanager
def file_lock(path: Path):
    """Cross-process lock using sidecar `.lock` file.

    - Waits for lock to be released by other process/thread.
    - Reentrant for the same thread and path.
    - Removes stale lock files after configured age.
    """

    lock_path = _lock_path_for(path)
    lock_key = str(lock_path.resolve())

    with _THREAD_LOCK:
        held = int(_THREAD_HELD_COUNTS.get(lock_key, 0) or 0)
        if held > 0:
            _THREAD_HELD_COUNTS[lock_key] = held + 1
            try:
                yield
            finally:
                with _THREAD_LOCK:
                    left = int(_THREAD_HELD_COUNTS.get(lock_key, 1) or 1) - 1
                    if left <= 0:
                        _THREAD_HELD_COUNTS.pop(lock_key, None)
                    else:
                        _THREAD_HELD_COUNTS[lock_key] = left
            return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    timeout_s = _lock_timeout_seconds()
    stale_s = _lock_stale_seconds()
    deadline = time.time() + timeout_s
    acquired = False

    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                payload = f"pid={os.getpid()} tid={threading.get_ident()} ts={time.time()}\n"
                os.write(fd, payload.encode("utf-8", errors="ignore"))
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                mtime = lock_path.stat().st_mtime
                if (time.time() - mtime) > stale_s:
                    try:
                        lock_path.unlink()
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.05)

    if not acquired:
        raise TimeoutError(f"Timed out waiting for file lock: {lock_path}")

    with _THREAD_LOCK:
        _THREAD_HELD_COUNTS[lock_key] = int(_THREAD_HELD_COUNTS.get(lock_key, 0) or 0) + 1

    try:
        yield
    finally:
        with _THREAD_LOCK:
            left = int(_THREAD_HELD_COUNTS.get(lock_key, 1) or 1) - 1
            if left <= 0:
                _THREAD_HELD_COUNTS.pop(lock_key, None)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
            else:
                _THREAD_HELD_COUNTS[lock_key] = left


def json_error_snippet(*, text: str, lineno: int, context_lines: int = 2) -> str:
    lines = text.splitlines()
    if lineno < 1:
        lineno = 1
    start = max(1, lineno - context_lines)
    end = min(len(lines), lineno + context_lines)

    out: List[str] = []
    for i in range(start, end + 1):
        prefix = ">>" if i == lineno else "  "
        line_text = lines[i - 1] if 0 <= i - 1 < len(lines) else ""
        out.append(f"{prefix} {i:4d}: {line_text}")
    return "\n".join(out)


def read_json(path: Path) -> Any:
    with file_lock(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(
                "Failed to read JSON as UTF-8. "
                f"File: {path}\n"
                "Tip: save the file as UTF-8 (no BOM) and ensure it contains valid JSON."
            ) from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            snippet = json_error_snippet(text=raw, lineno=int(getattr(e, "lineno", 1) or 1))
            raise ValueError(
                "Malformed JSON file.\n"
                f"File: {path}\n"
                f"Error: {e.msg} (line {e.lineno}, column {e.colno})\n"
                "Common causes: trailing commas, comments, unquoted keys/strings.\n"
                "Context:\n"
                f"{snippet}"
            ) from e


def write_json(path: Path, data: Any) -> None:
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


class StorageLimitExceeded(ValueError):
    pass


def _format_kilobytes(size_bytes: int) -> str:
    return str((int(size_bytes) + 1023) // 1024)


def ensure_file_under_storage_limit(
    path: Path,
    *,
    hard_limit_bytes: int,
    target_limit_kb: int,
    storage_kind: str,
    pointer: str = "",
    new_size_bytes: Optional[int] = None,
) -> None:
    """Reject writes that would keep growing storage past the hard limit."""
    if not path.exists():
        return

    try:
        current_size = int(path.stat().st_size)
    except Exception:
        return

    hard_limit = int(hard_limit_bytes)
    if new_size_bytes is not None:
        projected_size = int(new_size_bytes)
        if projected_size < hard_limit:
            return
        if projected_size < current_size:
            return
        blocked_size = projected_size
    elif current_size >= hard_limit:
        blocked_size = current_size
    else:
        return

    if blocked_size >= hard_limit:
        raise StorageLimitExceeded(
            f"{storage_kind} storage exceed limit of {target_limit_kb}KB and is "
            f"{_format_kilobytes(blocked_size)}KB in size. "
            f"Update was by pointer: {pointer}"
        )


def append_jsonl(path: Path, obj: Any) -> None:
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8") if not path.exists() else None
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def parse_value(value: str) -> Any:
    """Best-effort parse: JSON if possible, otherwise treat as string."""

    if value is None:
        return None
    s = str(value)
    try:
        return json.loads(s)
    except Exception:
        return s


# Backward-compatible internal names for older imports within this repo.
_json_error_snippet = json_error_snippet
_read_json = read_json
_write_json = write_json
_append_jsonl = append_jsonl
_parse_value = parse_value
_file_lock = file_lock
