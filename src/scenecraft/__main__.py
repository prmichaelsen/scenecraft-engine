"""Allow running as `python -m scenecraft`.

Loads environment variables from .env files BEFORE any scenecraft module is
imported, so modules reading os.environ at load time (e.g. oauth_client,
chat) see the configured values.

Load order (later entries override earlier ones):
  1. CWD/.env               — project-local dev settings
  2. ~/.config/scenecraft/.env — persistent user settings
"""

import os
from pathlib import Path


def _load_env_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    # 1) Current working directory
    load_dotenv(".env", override=False)

    # 2) ~/.config/scenecraft/.env
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    user_env = Path(xdg) / "scenecraft" / ".env"
    if user_env.exists():
        load_dotenv(str(user_env), override=False)


_load_env_files()


from scenecraft.cli import main  # noqa: E402

main()
