"""The live capability probe, shared by the radar and the canary.

WHY A TOOL CALL AND NOT A PING: provider metadata lies. Every free model on
OpenRouter advertises tool support; several return 403 "only available on
agentic harnesses" the moment you use one. A 200 from /chat/completions proves
the model answers, not that it can drive an agent. So the probe asks for a
specific tool call with a specific argument and checks it came back correctly.
A model that replies in prose cannot run an agent, however fast it is.

WHY THREE OUTCOMES AND NOT TWO: `transient` exists because unreachable is not
dead. A 429 or a 5xx says the provider is busy; treating that as a verdict
either buries a good free model on first contact, or -- on the canary side --
fails a healthy primary over on one bad minute. That distinction came out of a
restart storm caused by exactly this conflation.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

TOOL = [{"type": "function", "function": {
    "name": "get_weather",
    "description": "Get the current weather for a city",
    "parameters": {"type": "object",
                   "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}]

CITY = "Reykjavik"
PROMPT = "What is the weather in %s? Use the tool." % CITY

# Substrings that mean the model/key is definitively unusable, not merely busy.
HARD_MARKERS = (
    "does not exist", "not supported", "no endpoints", "invalid model",
    "no allowed providers", "insufficient credit", "requires more credits",
    "user not found", "invalid api key", "no auth credentials",
)


def classify(status: int, body: str) -> str:
    """'transient' or 'hard'. Only 'hard' is grounds for acting."""
    if status in (408, 409, 429) or status >= 500:
        return "transient"
    low = body.lower()
    if any(m in low for m in HARD_MARKERS):
        return "hard"
    # 401/403/404 with an unrecognised body: definitive by status.
    return "hard" if status in (400, 401, 403, 404) else "transient"


def probe(url: str, model: str, key: str, user_agent: str, timeout: int = 60):
    """Returns (ok, kind, detail, latency_s, cost).

    ok is True | False | None, where None means transient -- see the module
    docstring. Callers that must not act on noise check `ok is None` first.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": TOOL, "tool_choice": "auto", "max_tokens": 512,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "User-Agent": user_agent})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        raw = e.read()[:200].decode(errors="replace")
        kind = classify(e.code, raw)
        detail = "HTTP %s: %s" % (e.code, raw)
        return (None if kind == "transient" else False), kind, detail, 0.0, None
    except Exception as e:                                    # noqa: BLE001
        return None, "transient", "%s: %s" % (type(e).__name__, e), 0.0, None

    dt = round(time.time() - t0, 2)
    cost = d.get("cost")
    if cost is None:
        cost = (d.get("usage") or {}).get("cost")
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    calls = msg.get("tool_calls") or []
    if not calls:
        return False, "hard", "no tool_call", dt, cost
    fn = calls[0].get("function", {})
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except Exception:                                         # noqa: BLE001
        return False, "hard", "unparseable tool arguments", dt, cost
    if fn.get("name") != "get_weather" or \
            CITY.lower() not in str(args.get("city", "")).lower():
        return False, "hard", "wrong tool call", dt, cost
    return True, "ok", "ok", dt, cost
