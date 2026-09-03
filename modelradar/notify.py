"""Alerting. Stdout always; Discord too, if a webhook is configured.

The webhook URL is read from an ENVIRONMENT VARIABLE NAMED IN THE CONFIG, never
from the config itself -- a webhook URL is a credential (anyone holding it can
post to the channel) and configs get committed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def notify(cfg: dict, msg: str) -> None:
    log(msg.replace("\n", " | "))
    url = os.environ.get(cfg.get("notify", {}).get("discord_webhook_env", ""))
    if not url:
        return
    try:
        body = json.dumps({"content": msg[:1900]}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:                                    # noqa: BLE001
        # An alerting failure must never take down the check that produced it.
        log("notify failed: %s: %s" % (type(e).__name__, e))
