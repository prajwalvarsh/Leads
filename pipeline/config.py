"""Load config/berlin.yaml and .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
DB_PATH = DATA / "leads.duckdb"
USER_AGENT = "HeidiCaseResearch/0.1"

load_dotenv(ROOT / ".env")


@lru_cache
def cfg() -> dict[str, Any]:
    with open(ROOT / "config" / "berlin.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def bezirke() -> list[str]:
    return list(cfg()["bezirke"].keys())


def ortsteile() -> list[tuple[str, str]]:
    """(bezirk, ortsteil) pairs."""
    return [(b, o) for b, ots in cfg()["bezirke"].items() for o in ots]
