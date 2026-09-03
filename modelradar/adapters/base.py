"""The adapter boundary.

Discovery, probing and the adoption gate are provider-agnostic. Actually
POINTING your agent at a different model is not: every framework stores that
somewhere different. That is the whole of what an adapter does.

A target is whatever unit your framework switches independently -- a profile,
an agent, a workspace, a single config file. The radar and the canary only ever
address targets by name.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Primary:
    """What a target is currently pointed at."""
    provider: str
    model: str
    context_length: int = 0
    # The free fallback, if the framework has a fallback chain. Used only to
    # avoid promoting a model into the primary slot while it is also the
    # fallback -- a fallback sharing the primary's failure mode is not one.
    free_fallback: str | None = None
    fallbacks: list = field(default_factory=list)


class Adapter:
    def targets(self) -> list[str]:
        """Every switchable target this adapter can see."""
        raise NotImplementedError

    def read(self, target: str) -> Primary | None:
        """The target's current primary, or None if it has none."""
        raise NotImplementedError

    def write(self, target: str, primary: Primary, tag: str) -> None:
        """Repoint the target. MUST back up whatever it overwrites, and MUST
        re-read the result to prove it still parses before returning."""
        raise NotImplementedError


def get(name: str, options: dict):
    if name == "jsonfile":
        from .jsonfile import JsonFileAdapter
        return JsonFileAdapter(options)
    if name == "hermes":
        from .hermes import HermesAdapter
        return HermesAdapter(options)
    raise SystemExit("unknown adapter %r (have: jsonfile, hermes)" % name)
