from __future__ import annotations

from harnesshub.providers.agy import AgyAdapter
from harnesshub.providers.base import ProviderAdapter
from harnesshub.providers.claude import ClaudeAdapter
from harnesshub.providers.codex import CodexAdapter

ADAPTERS: dict[str, ProviderAdapter] = {
    "agy": AgyAdapter(),
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
}

__all__ = ["ADAPTERS", "AgyAdapter", "ClaudeAdapter", "CodexAdapter", "ProviderAdapter"]
