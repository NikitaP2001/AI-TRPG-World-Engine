"""PinnedBlockCache — mtime-based pinned block caching for agent context.

Cache invalidates per-entry by source file modification time (mtime),
NOT by messages.json trim counter. This means:
  - Changes to {agent}_summaries.json invalidate only that agent's block
  - Messages.json trims have NO effect on the cache
  - Each entry is invalidated independently

The cache is process-wide (``get_pinned_block_cache()``) because every agent
builds its context per call: a per-call cache would start cold every time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# PinnedBlockCache — mtime-based invalidation
# ---------------------------------------------------------------------------


class PinnedBlockCache:
    """Caches pinned SystemMessage blocks, invalidates by source file mtime.

    One entry per (block name, source path), each invalidated independently when
    its source file's modification time changes. This means changes to
    ``sm_summaries.json`` only invalidate the entry built from it — every other
    entry remains cached.

    The cache is in-memory and has no dependency on the trim counter or
    messages.json. After an application restart it is cold and all entries are
    rebuilt on first access.

    Usage::

        cache = get_pinned_block_cache()
        block = cache.get("sm", summaries_path, build_agent_summaries_block)
    """

    def __init__(self) -> None:
        self._cache: Dict[Any, str] = {}
        self._mtime: Dict[Any, float] = {}

    def get(
        self,
        key: str,
        source_path: Path,
        builder,
    ) -> Optional[str]:
        """Return cached block or rebuild if source file mtime changed.

        Args:
            key: Block name (e.g. ``"sm"``, ``"gm"``, ``"arc"``). The source
                 path is part of the cache key, so the same name read from two
                 different files is two different entries.
            source_path: Path to the source data file (e.g. ``sm_summaries.json``).
            builder: Callable(source_path) -> Optional[str]; called to build
                     the block when the cache misses.
        """
        cache_key = (str(key), str(source_path))
        current_mtime = source_path.stat().st_mtime if source_path.exists() else 0.0

        cached_mtime = self._mtime.get(cache_key)
        if cached_mtime != current_mtime:
            self._cache.pop(cache_key, None)
            self._mtime[cache_key] = current_mtime

        if cache_key not in self._cache:
            try:
                block = builder(source_path)
                if block:
                    self._cache[cache_key] = block
            except Exception:
                pass

        return self._cache.get(cache_key)


# ---------------------------------------------------------------------------
# Process-wide instance
# ---------------------------------------------------------------------------

_SHARED_CACHE: Optional[PinnedBlockCache] = None


def get_pinned_block_cache() -> PinnedBlockCache:
    """The cache that every agent's context build shares.

    One instance per process, so a block built during one invocation is still
    cached in the next and is rebuilt only when its source file changes. An
    agent that builds its context per call must use this, not its own instance,
    or every call would start cold.
    """
    global _SHARED_CACHE
    if _SHARED_CACHE is None:
        _SHARED_CACHE = PinnedBlockCache()
    return _SHARED_CACHE

