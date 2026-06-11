# orchestrator.py — Game engine orchestrator

# Backward-compatible alias: re-exports GameOrchestrator + ConsoleApp from the
# real implementation in console_app.py.
# Consumers should eventually import GameOrchestrator directly from here.
#
# Composed modules (in engine/):
#   story_tracker.py  — StoryTracker: story progress queries
#   gm_context.py     — GMContextManager: GM history bootstrap, scene-pick injection
#
# Scene Manager (separate agent):
#   scene_manager/core.py — SceneManager: scene lifecycle (world planning, narration, correction)
#   scene_manager/tools.py — SM tool definitions (gm_plan_world, gm_turn_narration, etc.)
#
# Scheduler (top-level package):
#   scheduler/core.py — TickScheduler, Job, Trigger types
#   scheduler/jobs.py — Single registry of ALL scheduled tasks (the schedule at a glance)

from console_app import GameOrchestrator  # noqa: F401

# Legacy name kept for compatibility
ConsoleApp = GameOrchestrator

