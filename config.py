from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OpenRouterConfig:
	api_key: str
	base_url: str
	model: str
	site_url: Optional[str] = None
	app_name: Optional[str] = None


def load_openrouter_config() -> OpenRouterConfig:
	api_key = os.getenv("OPENROUTER_API_KEY")
	if not api_key:
		raise RuntimeError(
			"Missing OPENROUTER_API_KEY. Set it in your environment or in a .env file."
		)

	return OpenRouterConfig(
		api_key=api_key,
		base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
		model=os.getenv("OPENROUTER_MODEL", "tngtech/deepseek-r1t2-chimera:free"),
		site_url=os.getenv("OPENROUTER_SITE_URL") or None,
		app_name=os.getenv("OPENROUTER_APP_NAME") or None,
	)
