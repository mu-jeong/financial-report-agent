"""Generate .env.example from the central configuration specs."""

from __future__ import annotations

from pathlib import Path

from src.configs.settings import BASE_DIR, render_env_example


def main() -> None:
    env_example_path = BASE_DIR / ".env.example"
    env_example_path.write_text(render_env_example(), encoding="utf-8")
    print(f"Wrote {env_example_path}")


if __name__ == "__main__":
    main()
