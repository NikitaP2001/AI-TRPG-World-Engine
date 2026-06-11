"""World Manager tool definitions."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool


@tool
def world_setting_result(setting_json: str) -> str:
    """Submit the world setting block as a JSON string.

    This is a termination tool — calling it ends the current invocation.

    Args:
        setting_json: JSON with keys: world_essence (string),
            gurps_calibration (object), initial_world_time (string).
            See user instructions for full structure.
    """
    return "ok"
