"""Adapter for Hermes (hermes-agent), where this tooling was built and runs.

A Hermes target is a PROFILE: `global`, or a named profile under
profiles/<name>/. Each has its own config.yaml with a `model:` block and an
optional `fallback_providers:` chain.

Two traps are encoded here, both paid for the hard way:

  * The YAML loader REFUSES duplicate keys. A duplicate silently wins in
    SafeLoader, and one did exactly that to `disabled_toolsets` -- the list
    parsed as empty and a toolset trim silently did nothing for days.

  * `providers.custom` is ONE SLOT per config scope. So a local-Ollama entry in
    the fallback chain must carry its own per-entry `base_url`; it cannot rely
    on the provider-level one, which may point somewhere else entirely (an
    OpenAI-compatible subscription endpoint, say). Getting this wrong sends
    your local model name to a remote endpoint, which fails confusingly.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import yaml

from .base import Adapter, Primary


class _NoDupLoader(yaml.SafeLoader):
    pass


def _no_dup(loader, node, deep=False):
    keys = set()
    for k, _ in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in keys:
            raise ValueError("duplicate key in YAML: %r" % (key,))
        keys.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_NoDupLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup)


class HermesAdapter(Adapter):
    def __init__(self, options: dict):
        self.home = Path(options.get("home", "~/.hermes")).expanduser()
        self.local_base_url = options.get("local_base_url", "")
        self.local_model = options.get("local_model", "")

    def path(self, target: str) -> Path:
        return (self.home / "config.yaml" if target == "global"
                else self.home / "profiles" / target / "config.yaml")

    def targets(self) -> list[str]:
        out = ["global"] if (self.home / "config.yaml").exists() else []
        pdir = self.home / "profiles"
        if pdir.is_dir():
            out += sorted(p.name for p in pdir.iterdir()
                          if (p / "config.yaml").exists())
        return out

    def read(self, target: str) -> Primary | None:
        p = self.path(target)
        if not p.exists():
            return None
        cfg = yaml.load(p.read_text(), Loader=_NoDupLoader) or {}
        m = cfg.get("model") or {}
        if not m.get("default"):
            return None
        free_fb = None
        chain = cfg.get("fallback_providers") or []
        for e in chain:
            if e.get("provider") == "openrouter" and str(e.get("model", "")).endswith(":free"):
                free_fb = e.get("model")
        return Primary(provider=m.get("provider", ""), model=m["default"],
                       context_length=int(m.get("context_length") or 0),
                       free_fallback=free_fb, fallbacks=chain)

    def write(self, target: str, primary: Primary, tag: str) -> None:
        p = self.path(target)
        cfg = yaml.load(p.read_text(), Loader=_NoDupLoader) or {}
        shutil.copy2(p, "%s.bak-%s-%s" % (p, tag, time.strftime("%Y%m%d%H%M")))

        cfg["model"] = {"provider": primary.provider, "default": primary.model,
                        "context_length": primary.context_length}
        if primary.fallbacks:
            cfg["fallback_providers"] = primary.fallbacks
        p.write_text(yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False))
        yaml.load(p.read_text(), Loader=_NoDupLoader)   # re-read or die

    def local_fallback_entry(self) -> dict:
        """The local-model chain entry, carrying its own base_url. See the
        one-slot trap in the module docstring."""
        return {"provider": "custom", "model": self.local_model,
                "base_url": self.local_base_url}
