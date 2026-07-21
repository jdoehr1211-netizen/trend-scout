"""Config loading (settings.yaml + sources.yaml + .env)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

CONFIG_DIR = Path("config")


@dataclass
class Config:
    settings: dict[str, Any]
    sources: dict[str, Any]

    @property
    def seed_keywords(self) -> list[str]:
        return self.settings["seed_keywords"]

    @property
    def target_geos(self) -> list[str]:
        return self.settings.get("target_geos", [])

    def source(self, name: str) -> dict[str, Any]:
        return self.sources.get(name, {})


def load(config_dir: Path = CONFIG_DIR) -> Config:
    load_dotenv()
    settings = yaml.safe_load((config_dir / "settings.yaml").read_text(encoding="utf-8"))
    sources = yaml.safe_load((config_dir / "sources.yaml").read_text(encoding="utf-8"))
    return Config(settings=settings, sources=sources)
