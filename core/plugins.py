"""Zero-overhead extension slots for optional vision and game-brain plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class VisionLocatorPlugin(ABC):
    """Optional visual element locator (e.g. OmniParser V2 on local GPU)."""

    @abstractmethod
    def locate(self, image_bytes: bytes, query: str) -> Optional[Tuple[int, int, float]]:
        """Return (x, y, confidence) in image coordinates, or None if not found."""


class GameBrainPlugin(ABC):
    """Optional high-frequency local game policy brain (0-token realtime loop)."""

    @abstractmethod
    def step(self, hwnd: int, instruction: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a high-level tactical instruction and return structured status."""


class PluginRegistry:
    """Lightweight registry that incurs zero memory/startup cost when unused."""

    def __init__(self) -> None:
        self.vision_locators: List[VisionLocatorPlugin] = []
        self.game_brains: Dict[str, GameBrainPlugin] = {}

    def register_vision(self, plugin: VisionLocatorPlugin) -> None:
        self.vision_locators.append(plugin)

    def register_game_brain(self, name: str, plugin: GameBrainPlugin) -> None:
        self.game_brains[name] = plugin
