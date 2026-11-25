from __future__ import annotations

import copy
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

BUILTIN_CATALOG_PATH = Path(__file__).with_name("models_catalog.toml")


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    return data if isinstance(data, dict) else {}


def _merge_catalog(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    if isinstance(override.get("aliases"), dict):
        merged.setdefault("aliases", {}).update(
            {str(k).lower(): str(v) for k, v in override["aliases"].items()}
        )
    if isinstance(override.get("defaults"), dict):
        for prov, vals in override["defaults"].items():
            if isinstance(vals, list):
                merged.setdefault("defaults", {})[str(prov)] = [str(v) for v in vals]
    if isinstance(override.get("labels"), dict):
        for prov, mapping in override["labels"].items():
            if isinstance(mapping, dict):
                merged.setdefault("labels", {}).setdefault(str(prov), {}).update(
                    {str(k): str(v) for k, v in mapping.items()}
                )
    if isinstance(override.get("agy"), dict):
        pool = override["agy"].get("pool_models")
        if isinstance(pool, list):
            merged.setdefault("agy", {})["pool_models"] = [str(v) for v in pool]
    if isinstance(override.get("substitutes"), dict):
        for prov, mapping in override["substitutes"].items():
            if isinstance(mapping, dict):
                target = merged.setdefault("substitutes", {}).setdefault(str(prov), {})
                for key, chain in mapping.items():
                    if isinstance(chain, list):
                        target[str(key)] = [str(v) for v in chain]
    return merged


@lru_cache(maxsize=8)
def load_models_catalog(override_path: Path | None = None) -> dict[str, Any]:
    base = _read_toml(BUILTIN_CATALOG_PATH)
    if override_path and override_path.is_file():
        try:
            extra = _read_toml(override_path)
            return _merge_catalog(base, extra)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return base


_BUILTIN = load_models_catalog()

MODEL_ALIASES: dict[str, str] = {str(k): str(v) for k, v in _BUILTIN.get("aliases", {}).items()}
DEFAULT_CODEX_MODELS: tuple[str, ...] = tuple(
    str(x) for x in _BUILTIN.get("defaults", {}).get("codex", [])
)
DEFAULT_CLAUDE_MODELS: tuple[str, ...] = tuple(
    str(x) for x in _BUILTIN.get("defaults", {}).get("claude", [])
)
DEFAULT_AGY_MODELS: tuple[str, ...] = tuple(
    str(x) for x in _BUILTIN.get("defaults", {}).get("agy", [])
)
AGY_MODEL_LABELS: dict[str, str] = {
    str(k): str(v) for k, v in _BUILTIN.get("labels", {}).get("agy", {}).items()
}
CODEX_MODEL_LABELS: dict[str, str] = {
    str(k): str(v) for k, v in _BUILTIN.get("labels", {}).get("codex", {}).items()
}
CLAUDE_MODEL_LABELS: dict[str, str] = {
    str(k): str(v) for k, v in _BUILTIN.get("labels", {}).get("claude", {}).items()
}
AGY_POOL_MODELS: tuple[str, ...] = tuple(
    str(x) for x in _BUILTIN.get("agy", {}).get("pool_models", [])
)
CLAUDE_SUBSTITUTE_CHAINS: dict[str, tuple[str, ...]] = {
    str(k): tuple(str(x) for x in v)
    for k, v in _BUILTIN.get("substitutes", {}).get("claude", {}).items()
}


def catalog_for_root(root: Path | None = None) -> dict[str, Any]:
    if root is None:
        return _BUILTIN
    override = root / "models_catalog.toml"
    return load_models_catalog(override if override.is_file() else None)
