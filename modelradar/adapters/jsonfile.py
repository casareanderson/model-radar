"""Reference adapter: a plain JSON file your own launcher reads.

This exists so the tools are usable without any particular framework, and so
the adapter contract has a worked example that is 60 lines rather than 200.

    {
      "agents": {
        "coder": {"provider": "openrouter", "model": "qwen/qwen3.7-flash",
                  "context_length": 1000000}
      }
    }
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .base import Adapter, Primary


class JsonFileAdapter(Adapter):
    def __init__(self, options: dict):
        self.path = Path(options.get("path", "models.json")).expanduser()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"agents": {}}
        return json.loads(self.path.read_text())

    def targets(self) -> list[str]:
        return sorted(self._load().get("agents", {}))

    def read(self, target: str) -> Primary | None:
        a = self._load().get("agents", {}).get(target)
        if not a:
            return None
        return Primary(provider=a.get("provider", ""), model=a.get("model", ""),
                       context_length=int(a.get("context_length") or 0),
                       free_fallback=a.get("free_fallback"),
                       fallbacks=a.get("fallbacks") or [])

    def write(self, target: str, primary: Primary, tag: str) -> None:
        d = self._load()
        if self.path.exists():
            shutil.copy2(self.path, "%s.bak-%s-%s"
                         % (self.path, tag, time.strftime("%Y%m%d%H%M")))
        d.setdefault("agents", {})[target] = {
            "provider": primary.provider,
            "model": primary.model,
            "context_length": primary.context_length,
            "free_fallback": primary.free_fallback,
            "fallbacks": primary.fallbacks,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2, sort_keys=True))
        json.loads(self.path.read_text())   # re-read or die
