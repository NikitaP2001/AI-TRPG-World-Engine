# orchestrator.py — Top-level re-export for backward compatibility.
# Game engine orchestrator now lives in engine/orchestrator.py.
# This file will be removed after all consumers are updated.

from engine.orchestrator import Orchestrator, GameOrchestrator  # noqa: F401
ConsoleApp = GameOrchestrator

