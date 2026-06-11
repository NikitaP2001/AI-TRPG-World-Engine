"""Scene base class — all visual scenes inherit from this."""

from __future__ import annotations
from abc import ABC, abstractmethod


class Scene(ABC):
    """Base class for all renderable scenes.

    A scene owns its OpenGL resources and implements
    render(), update(), and cleanup().
    """

    @abstractmethod
    def setup(self, ctx) -> None:
        """Create OpenGL resources (VAOs, shaders, buffers)."""
        ...

    @abstractmethod
    def render(self, ctx, camera) -> None:
        """Render one frame."""
        ...

    @abstractmethod
    def update(self, dt: float) -> None:
        """Update scene state (animations, LOD, data loading)."""
        ...

    @abstractmethod
    def cleanup(self) -> None:
        """Free OpenGL resources."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable scene name for UI."""
        ...
