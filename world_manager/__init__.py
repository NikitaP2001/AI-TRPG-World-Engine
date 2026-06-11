"""World Manager package — highest-level world agent.

Creates and maintains the world setting block (world name, rules, nature,
history). In the future handles macro/meso layer simulation.
"""

from world_manager.core import WorldManager

__all__ = ["WorldManager"]
