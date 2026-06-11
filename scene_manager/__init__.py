"""Scene Manager package — reactive, GM-dependent agent.

Manages scene lifecycle: world planning, character execution, turn narration.
Does NOT pick scenes — receives pre-chosen scenes from the Game Master.
"""

from scene_manager.core import SceneManager

__all__ = ["SceneManager"]
