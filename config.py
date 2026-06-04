from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DeepSeekConfig:
	api_key: str
	base_url: str
	model: str


def load_deepseek_config() -> DeepSeekConfig:
	api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENROUTER_API_KEY")
	if not api_key:
		raise RuntimeError(
			"Missing DEEPSEEK_API_KEY. Set it in your environment or in a .env file."
		)

	return DeepSeekConfig(
		api_key=api_key,
		base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
		model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
	)
