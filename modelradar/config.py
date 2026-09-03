"""Configuration. Everything site-specific lives here, nothing is hardcoded.

Loads, in order of increasing precedence:
  1. the defaults below
  2. a TOML file (--config, or $MODELRADAR_CONFIG, or ./config.toml)
  3. environment variables, for anything secret

Secrets are NEVER read from the TOML file. API keys come from the environment
or from an env-file the adapter points at, so the config can be committed.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "state_dir": "~/.local/state/model-radar",
    "openrouter": {
        "models": "https://openrouter.ai/api/v1/models",
        "chat": "https://openrouter.ai/api/v1/chat/completions",
        "credits": "https://openrouter.ai/api/v1/credits",
    },
    "zen": {
        "models": "https://opencode.ai/zen/v1/models",
        "chat": "https://opencode.ai/zen/v1/chat/completions",
    },
    # Cloudflare 403s the default python-urllib user agent (error 1010).
    "user_agent": "curl/8.5.0",
    "radar": {
        "min_ctx": 200_000,
        "headline_ctx": 500_000,
        "max_probes_per_source": 8,
        "adopt": False,
        "adopt_targets": [],
        "adopt_min_ctx": 500_000,
        "adopt_confirms": 3,
        "adopt_confirm_gap_s": 8,
        "adopt_latency_factor": 2.5,
        "adopt_latency_abs_max_s": 5.0,
        "adopt_cooldown_days": 7,
    },
    "canary": {
        "targets": [],
        "fails_before_failover": 2,
        "credit_floor_usd": 1.50,
        "credit_warn_usd": 4.00,
        "paid_ladder": [],
        "local": {"base_url": "", "model": ""},
        "est_in_tokens": 0,
        "est_out_tokens": 0,
    },
    "adapter": {"name": "jsonfile", "path": "~/.config/model-radar/models.json"},
    "notify": {"discord_webhook_env": "MODELRADAR_DISCORD_WEBHOOK"},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(path: str | None = None) -> dict:
    path = path or os.environ.get("MODELRADAR_CONFIG") or "config.toml"
    p = Path(path).expanduser()
    cfg = _merge(DEFAULTS, tomllib.loads(p.read_text()) if p.exists() else {})
    cfg["_path"] = str(p) if p.exists() else "(defaults only)"
    cfg["state_dir"] = str(Path(cfg["state_dir"]).expanduser())
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def secret(name: str, env_file: str | None = None) -> str | None:
    """A credential, from the environment first, then an optional env-file.

    The env-file path exists because agent frameworks commonly keep their keys
    in one; it is read, never written, and its values are never logged.
    """
    if os.environ.get(name):
        return os.environ[name]
    if env_file:
        p = Path(env_file).expanduser()
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None
