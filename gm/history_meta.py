"""Backward-compat re-export — ``GMHistoryMeta`` moved to ``engine.history_meta``."""

from __future__ import annotations

from engine.history_meta import HistoryMeta as GMHistoryMeta

__all__ = ["GMHistoryMeta"]
