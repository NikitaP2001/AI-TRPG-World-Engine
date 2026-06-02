from __future__ import annotations

from typing import Any, List, Tuple, Union


def _split_pointer(pointer: str) -> List[str]:
    if pointer is None:
        raise ValueError("Pointer is required")
    p = str(pointer).strip()
    if p == "":
        return []
    if not p.startswith("/"):
        raise ValueError("JSON pointer must start with '/'")

    parts = p.split("/")[1:]

    def unescape(token: str) -> str:
        return token.replace("~1", "/").replace("~0", "~")

    return [unescape(t) for t in parts]


def _is_int_token(token: str) -> bool:
    if token == "0":
        return True
    return token.isdigit() and not token.startswith("0")


def _is_append_token(token: str) -> bool:
    return token == "-"


def get_at_pointer(doc: Any, pointer: str) -> Any:
    cur = doc
    for token in _split_pointer(pointer):
        if isinstance(cur, list):
            if not _is_int_token(token):
                raise KeyError(f"Expected list index at '{token}'")
            idx = int(token)
            cur = cur[idx]
        elif isinstance(cur, dict):
            cur = cur[token]
        else:
            raise KeyError(f"Cannot traverse into non-container at '{token}'")
    return cur


def set_at_pointer(doc: Any, pointer: str, value: Any, *, create_missing: bool) -> Any:
    tokens = _split_pointer(pointer)
    if not tokens:
        return value

    cur = doc
    for i, token in enumerate(tokens[:-1]):
        next_token = tokens[i + 1]

        if isinstance(cur, dict):
            if token not in cur:
                if not create_missing:
                    raise KeyError(f"Missing key '{token}'")
                # Decide whether next should be list or dict.
                cur[token] = [] if _is_int_token(next_token) else {}
            cur = cur[token]
            continue

        if isinstance(cur, list):
            if _is_append_token(token):
                if not create_missing:
                    raise KeyError(f"Expected list index at '{token}'")
                cur.append(None)
                idx = len(cur) - 1
            else:
                if not _is_int_token(token):
                    raise KeyError(f"Expected list index at '{token}'")
                idx = int(token)
            if idx >= len(cur):
                if not create_missing:
                    raise KeyError(f"List index out of range at '{token}'")
                while len(cur) <= idx:
                    cur.append(None)
            if cur[idx] is None:
                if not create_missing:
                    raise KeyError(f"Missing list element at '{token}'")
                cur[idx] = [] if _is_int_token(next_token) else {}
            cur = cur[idx]
            continue

        raise KeyError(f"Cannot traverse into non-container at '{token}'")

    last = tokens[-1]
    if isinstance(cur, dict):
        if (last not in cur) and (not create_missing):
            raise KeyError(f"Missing key '{last}'")
        cur[last] = value
        return doc

    if isinstance(cur, list):
        if _is_append_token(last):
            if not create_missing:
                raise KeyError(f"Expected list index at '{last}'")
            cur.append(value)
            return doc
        if not _is_int_token(last):
            raise KeyError(f"Expected list index at '{last}'")
        idx = int(last)
        if idx >= len(cur):
            if not create_missing:
                raise KeyError(f"List index out of range at '{last}'")
            while len(cur) <= idx:
                cur.append(None)
        cur[idx] = value
        return doc

    raise KeyError(f"Cannot set on non-container at '{last}'")


def remove_at_pointer(doc: Any, pointer: str) -> Any:
    tokens = _split_pointer(pointer)
    if not tokens:
        raise ValueError("Cannot delete document root")

    cur = doc
    for token in tokens[:-1]:
        if isinstance(cur, dict):
            if token not in cur:
                raise KeyError(f"Missing key '{token}'")
            cur = cur[token]
            continue

        if isinstance(cur, list):
            if not _is_int_token(token):
                raise KeyError(f"Expected list index at '{token}'")
            idx = int(token)
            if idx >= len(cur):
                raise KeyError(f"List index out of range at '{token}'")
            cur = cur[idx]
            continue

        raise KeyError(f"Cannot traverse into non-container at '{token}'")

    last = tokens[-1]
    if isinstance(cur, dict):
        if last not in cur:
            raise KeyError(f"Missing key '{last}'")
        del cur[last]
        return doc

    if isinstance(cur, list):
        if not _is_int_token(last):
            raise KeyError(f"Expected list index at '{last}'")
        idx = int(last)
        if idx >= len(cur):
            raise KeyError(f"List index out of range at '{last}'")
        del cur[idx]
        return doc

    raise KeyError(f"Cannot delete on non-container at '{last}'")
