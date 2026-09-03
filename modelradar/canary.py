"""canary -- is the primary still serving, and is there still credit to pay for it?

Two failure modes, not one. A paid primary on a finite prepaid balance can be
perfectly healthy and simply run out of money, and most agent frameworks fall
back on rate-limit and connection errors only -- NOT on "model does not exist"
and NOT on "insufficient credit". Without something like this, the day a model
is withdrawn or the balance empties, every agent starts hard-erroring.

THE RULE THAT MATTERS: unreachable is not dead. A transient 5xx, a timeout or a
DNS blip is not grounds for failing anything over. Only a definitive verdict --
model gone, key rejected, out of credit -- confirmed on N CONSECUTIVE runs,
moves anything. That rule is here because the opposite conflation once produced
a restart storm against a service that was never down.

The ladder is vendor-diverse and cheapest-first, and every rung is PROBED with
a real tool call before it is adopted, because provider metadata lies about
tool support. If no paid rung serves, it drops to a local model, which is slower
and dumber but cannot be withdrawn or run out of money.

Deterministic: it gathers facts and acts on them. It never asks a model anything.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from . import config as cfgmod
from .adapters import base as adapters
from .notify import log, notify
from .probe import probe


def credits(cfg, key):
    """(remaining_usd, detail), or (None, error).

    None means UNKNOWN, which is treated as transient. "I could not read the
    balance" must never be actioned as "the balance is zero".
    """
    req = urllib.request.Request(
        cfg["openrouter"]["credits"],
        headers={"Authorization": "Bearer " + key,
                 "User-Agent": cfg["user_agent"]})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=25))["data"]
    except Exception as e:                                    # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)
    total, used = float(d.get("total_credits", 0)), float(d.get("total_usage", 0))
    return total - used, "granted $%.2f used $%.4f" % (total, used)


def state_file(cfg) -> Path:
    return Path(cfg["state_dir"]) / "canary.json"


def get_state(cfg) -> dict:
    try:
        return json.loads(state_file(cfg).read_text())
    except Exception:                                         # noqa: BLE001
        return {}


def put_state(cfg, st) -> None:
    state_file(cfg).write_text(json.dumps(st, indent=1, sort_keys=True))


def switch(cfg, adapter, targets, provider, model, ctx, tag, dry):
    """Repoint every managed target that is not already there.

    Targets whose primary is on a DIFFERENT provider are left alone: this check
    only speaks for the provider it measured. A separate subscription endpoint
    is unaffected by this provider's balance or by its model catalogue, and
    dragging it along on that evidence would be a false inference.
    """
    changed = []
    for t in targets:
        cur = adapter.read(t)
        if cur is None:
            log("  %s: unreadable or absent -- skipped" % t)
            continue
        if cur.provider not in ("", provider):
            log("  %s: on provider %r -- left alone" % (t, cur.provider))
            continue
        if cur.provider == provider and cur.model == model:
            continue
        if dry:
            log("  DRY RUN would repoint %s -> %s/%s" % (t, provider, model))
            changed.append(t)
            continue
        adapter.write(t, adapters.Primary(
            provider=provider, model=model, context_length=ctx,
            free_fallback=cur.free_fallback, fallbacks=cur.fallbacks), tag)
        changed.append(t)
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="model-canary", description=__doc__.split("\n")[0])
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and report, change nothing")
    args = ap.parse_args(argv)

    cfg = cfgmod.load(args.config)
    c = cfg["canary"]
    envf = cfg.get("adapter", {}).get("env_file")
    key = cfgmod.secret("OPENROUTER_API_KEY", envf)
    if not key:
        log("no OPENROUTER_API_KEY -- nothing to check")
        return 1
    adapter = adapters.get(cfg["adapter"]["name"], cfg["adapter"])
    targets = c["targets"] or adapter.targets()
    st = get_state(cfg)

    # ---- balance first. A healthy model you cannot pay for is still an outage.
    bal, detail = credits(cfg, key)
    if bal is None:
        log("credit: UNKNOWN (%s) -- treated as transient, not as empty" % detail)
    else:
        log("credit remaining $%.4f (%s)" % (bal, detail))
        if bal < c["credit_floor_usd"]:
            local = c["local"]
            if not local.get("model"):
                notify(cfg, "**Credit below floor** ($%.2f < $%.2f) and no local "
                            "fallback is configured. Agents will start failing."
                            % (bal, c["credit_floor_usd"]))
                return 1
            moved = switch(cfg, adapter, targets, "local", local["model"], 0,
                           "credit-floor", args.dry_run)
            notify(cfg, "**Credit floor hit** - $%.4f left, floor $%.2f.\n"
                        "Reverted %s to local `%s`.%s"
                        % (bal, c["credit_floor_usd"], ", ".join(moved) or "nothing",
                           local["model"],
                           " (DRY RUN)" if args.dry_run else ""))
            if not args.dry_run:
                put_state(cfg, st)
            return 0
        if bal < c["credit_warn_usd"]:
            notify(cfg, "Credit low: $%.4f left (warn at $%.2f, floor $%.2f)."
                        % (bal, c["credit_warn_usd"], c["credit_floor_usd"]))

    # ---- is the live primary still serving?
    inc = adapter.read(targets[0]) if targets else None
    if not inc:
        log("no readable primary on %s -- nothing to probe"
            % (targets[0] if targets else "(no targets)"))
        return 0

    ok, kind, detail, lat, _cost = probe(
        cfg["openrouter"]["chat"], inc.model, key, cfg["user_agent"])
    if ok:
        if st.pop("hard_fails", None):
            log("primary %s recovered -- failure count reset" % inc.model)
            put_state(cfg, st)
        log("primary %s ok (%ss)" % (inc.model, lat))
        return 0
    if kind == "transient":
        # Explicitly NOT counted. This is the whole point of the three-way
        # classification: a blip must not accumulate toward a failover.
        log("primary %s transient (%s) -- not counted" % (inc.model, detail[:120]))
        return 0

    n = int(st.get("hard_fails", 0)) + 1
    st["hard_fails"] = n
    st["last_hard"] = detail[:300]
    if not args.dry_run:
        put_state(cfg, st)
    log("hard failure #%d for %s -- %s" % (n, inc.model, detail[:160]))
    if n < c["fails_before_failover"]:
        return 0

    # ---- walk the ladder. Probe each rung; adopt the first that truly serves.
    for rung in c["paid_ladder"]:
        model, ctx = rung["model"], int(rung.get("context_length") or 0)
        if model == inc.model:
            continue
        rok, _k, rdetail, rlat, _c2 = probe(
            cfg["openrouter"]["chat"], model, key, cfg["user_agent"])
        log("  ladder %-38s %s %s" % (model, "PASS" if rok else "FAIL", rdetail[:60]))
        if not rok:
            continue
        moved = switch(cfg, adapter, targets, "openrouter", model, ctx,
                       "canary-failover", args.dry_run)
        st["hard_fails"] = 0
        if not args.dry_run:
            put_state(cfg, st)
        notify(cfg, "**Primary failed over** - `%s` -> `%s` on %s.\n%s\n"
                    "Probed clean at %ss before the switch.%s"
                    % (inc.model, model, ", ".join(moved) or "nothing",
                       detail[:200], rlat, " (DRY RUN)" if args.dry_run else ""))
        return 0

    local = c["local"]
    if local.get("model"):
        moved = switch(cfg, adapter, targets, "local", local["model"], 0,
                       "canary-local", args.dry_run)
        st["hard_fails"] = 0
        if not args.dry_run:
            put_state(cfg, st)
        notify(cfg, "**No paid model served** - dropped %s to local `%s`.\n%s"
                    % (", ".join(moved) or "nothing", local["model"], detail[:200]))
        return 0

    notify(cfg, "**Primary is down and NOTHING served** - `%s`: %s\n"
                "No ladder rung passed and no local fallback is configured."
                % (inc.model, detail[:250]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
