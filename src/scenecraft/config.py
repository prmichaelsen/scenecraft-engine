"""SceneCraft configuration — persistent settings stored at $XDG_CONFIG_HOME/scenecraft/config.json."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


CONFIG_DIR = _config_home() / "scenecraft"
CONFIG_FILE = CONFIG_DIR / "config.json"
_LEGACY_CONFIG_FILE = Path.home() / ".scenecraft" / "config.json"


def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load config from disk. Returns empty dict if no config exists.

    Falls back to the legacy ~/.scenecraft/config.json location and migrates it.
    """
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    if _LEGACY_CONFIG_FILE.exists():
        with open(_LEGACY_CONFIG_FILE) as f:
            data = json.load(f)
        _ensure_config_dir()
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return data
    return {}


def save_config(config: dict):
    """Write config to disk."""
    _ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_projects_dir() -> Path | None:
    """Get the configured projects directory, or None if not set."""
    config = load_config()
    raw = config.get("projects_dir")
    if raw:
        return Path(raw).expanduser()
    return None


def set_projects_dir(path: str | Path):
    """Set the projects directory and persist to config."""
    p = Path(path).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    config = load_config()
    config["projects_dir"] = str(p)
    save_config(config)
    return p


def resolve_work_dir(cli_override: str | None = None) -> Path | None:
    """Resolve the work directory from CLI override or config.

    Priority:
    1. CLI --work-dir flag (if provided)
    2. Config file projects_dir
    3. None (caller should prompt)
    """
    if cli_override:
        return Path(cli_override)
    return get_projects_dir()
