"""Console entry point for the LLM World game."""

from __future__ import annotations

from dotenv import load_dotenv

from console_app import ConsoleApp
from openrouter_langchain_logging import reset_logs_if_enabled


def main() -> int:
    """Run the console game UI."""

    # Ensure .env is applied and reset logs on each start (when enabled).
    load_dotenv(override=True)
    reset_logs_if_enabled()

    try:
        app = ConsoleApp()
    except Exception as e:  # noqa: BLE001
        print(f"Startup error: {e}")
        print("Tip: copy .env.example to .env and set OPENROUTER_API_KEY.")
        return 1

    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
