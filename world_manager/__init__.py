"""World Manager package — highest-level world agent.

Creates and maintains the world setting block (world name, rules, nature,
history). Handles subscription-based world events via ReAct loop.
"""

from world_manager.core import WorldManager

__all__ = ["WorldManager"]
