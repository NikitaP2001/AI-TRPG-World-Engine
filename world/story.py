"""PinnedBlockCache — mtime-based pinned block caching for agent context.

Cache invalidates per-key by source file modification time (mtime),
NOT by messages.json trim counter. This means:
  - Changes to {agent}_summaries.json invalidate only the "sm"/"gm"/"wm" key
  - Messages.json trims have NO effect on the cache
  - Each key is invalidated independently
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# PinnedBlockCache — mtime-based invalidation
# ---------------------------------------------------------------------------


class PinnedBlockCache:
    """Caches pinned SystemMessage blocks, invalidates by source file mtime.

    Each cache key is invalidated independently when its *source_path* file
    modification time changes. This means changes to ``sm_summaries.json``
    only invalidate the ``"sm"`` key — other keys remain cached.

    The cache is entirely in-memory and has no dependency on the trim counter
    or messages.json. After an application restart, the cache is cold and
    all keys are rebuilt on first access.

    Usage::

        cache = PinnedBlockCache()
        block = cache.get("sm", summaries_path, build_agent_summaries_block)
    """

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}
        self._mtime: Dict[str, float] = {}

    def get(
        self,
        key: str,
        source_path: Path,
        builder,
    ) -> Optional[str]:
        """Return cached block or rebuild if source file mtime changed.

        Args:
            key: Cache key (e.g. ``"sm"``, ``"gm"``, ``"wm"``).
            source_path: Path to the source data file (e.g. ``sm_summaries.json``).
            builder: Callable(source_path) -> Optional[str]; called to build
                     the block when the cache misses.
        """
        current_mtime = source_path.stat().st_mtime if source_path.exists() else 0.0

        cached_mtime = self._mtime.get(key)
        if cached_mtime != current_mtime:
            self._cache.pop(key, None)
            self._mtime[key] = current_mtime

        if key not in self._cache:
            try:
                block = builder(source_path)
                if block:
                    self._cache[key] = block
            except Exception:
                pass

        return self._cache.get(key)

