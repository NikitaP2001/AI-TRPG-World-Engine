"""Console entry point for the LLM World game.

Temporary console interface. Streams agent calls during turn execution
and displays character actions + SM narration at turn end.
"""

from __future__ import annotations

import os
import sys
from dotenv import load_dotenv

from orchestrator import GameOrchestrator
from openrouter_langchain_logging import reset_logs_if_enabled


def main() -> int:
    """Run the console game UI."""

    # Ensure .env is applied and reset logs on each start (when enabled).
    load_dotenv(override=True)
    reset_logs_if_enabled()

    # Enable stream echo so agent calls are visible in console
    os.environ["LLM_WORLD_STREAM_ECHO"] = "1"

    try:
        app = GameOrchestrator()
    except Exception as e:  # noqa: BLE001
        print(f"Startup error: {e}")
        print("Tip: copy .env.example to .env and set DEEPSEEK_API_KEY.")
        return 1

    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
