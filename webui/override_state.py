from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class PendingPrompt:
    character_name: str
    scene_location: str
    world_time: str
    character_input: str
    gm_correction_notice: str
    current_intent: str
    created_at: str


@dataclass
class PendingDecision:
    character_name: str
    intent: str
    thoughts: str
    created_at: str


class OverrideStore:
    """Stores a single, optional, one-turn human override.

    Design goal: default behavior unchanged; override only engages when armed.
    """

    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.path = os.path.join(repo_root, "game", "user_inputs", "override_state.json")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalize_character_input_text(value: str) -> str:
        text = str(value or "")
        stripped = text.strip()

        # Legacy format stored character input as JSON object:
        # {"scene_description": "..."}
        if stripped.startswith("{") and '"scene_description"' in stripped:
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    scene_text = obj.get("scene_description")
                    if isinstance(scene_text, str) and scene_text.strip():
                        text = scene_text
            except Exception:
                pass

        # Handle literal escaped newlines from older payloads.
        if "\\n" in text and "\n" not in text:
            text = text.replace("\\n", "\n")

        return text.strip()

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return {
            "armed_character": "",
            "pending_prompt": None,
            "pending_decision": None,
        }

    def save(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)

    def arm(self, character_name: str) -> None:
        data = self.load()
        data["armed_character"] = (character_name or "").strip()
        # do not clear pending_prompt/decision; they may be in-progress
        self.save(data)

    def disarm(self) -> None:
        data = self.load()
        data["armed_character"] = ""
        data["pending_prompt"] = None
        data["pending_decision"] = None
        self.save(data)

    def set_pending_prompt(
        self,
        *,
        character_name: str,
        scene_location: str,
        world_time: str,
        character_input: str,
        gm_reality_notice: str = "",
        current_intent: str = "",
    ) -> PendingPrompt:
        data = self.load()
        p = {
            "character_name": character_name,
            "scene_location": scene_location,
            "world_time": world_time,
            "character_input": character_input,
            "gm_reality_notice": str(gm_reality_notice or ""),
            "current_intent": str(current_intent or ""),
            "created_at": self._now(),
        }
        data["pending_prompt"] = p
        data["pending_decision"] = None
        self.save(data)
        return PendingPrompt(**p)

    def get_pending_prompt(self) -> Optional[PendingPrompt]:
        data = self.load()
        p = data.get("pending_prompt")
        if not isinstance(p, dict):
            return None
        try:
            raw_character_input = str(
                p.get("character_input")
                or p.get("character_observation")
                or p.get("gm_visible_context")
                or ""
            )
            return PendingPrompt(
                character_name=str(p.get("character_name") or ""),
                scene_location=str(p.get("scene_location") or ""),
                world_time=str(p.get("world_time") or ""),
                character_input=self._normalize_character_input_text(raw_character_input),
                gm_reality_notice=str(p.get("gm_reality_notice") or p.get("gm_notice") or ""),
                current_intent=str(p.get("current_intent") or p.get("last_intent") or ""),
                created_at=str(p.get("created_at") or ""),
            )
        except Exception:
            return None

    def set_pending_decision(
        self,
        *,
        character_name: str,
        intent: str,
        thoughts: str,
    ) -> PendingDecision:
        data = self.load()
        d = {
            "character_name": character_name,
            "intent": str(intent or ""),
            "thoughts": str(thoughts or ""),
            "created_at": self._now(),
        }
        data["pending_decision"] = d
        # Clear pending_prompt so the auto-advance loop no longer sees
        # "waiting for human input" and actually proceeds to consume the decision.
        data["pending_prompt"] = None
        self.save(data)
        return PendingDecision(**d)

    def get_pending_decision(self) -> Optional[PendingDecision]:
        data = self.load()
        d = data.get("pending_decision")
        if not isinstance(d, dict):
            return None
        try:
            return PendingDecision(
                character_name=str(d.get("character_name") or ""),
                intent=str(d.get("intent") or ""),
                thoughts=str(d.get("thoughts") or ""),
                created_at=str(d.get("created_at") or ""),
            )
        except Exception:
            return None

    def consume_pending_decision(self) -> None:
        """Clear pending decision/prompt while keeping armed_character unchanged."""
        data = self.load()
        data["pending_decision"] = None
        data["pending_prompt"] = None
        self.save(data)

    def armed_character(self) -> str:
        data = self.load()
        return str(data.get("armed_character") or "").strip()
