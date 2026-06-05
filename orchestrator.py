# orchestrator.py — Game engine orchestrator

# Backward-compatible alias: re-exports GameOrchestrator + ConsoleApp from the
# real implementation in console_app.py.
# Consumers should eventually import GameOrchestrator directly from here.

from console_app import GameOrchestrator  # noqa: F401

# Legacy name kept for compatibility
ConsoleApp = GameOrchestrator
