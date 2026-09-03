"""radar -- find free tool-calling models, and optionally adopt one.

The origin: a free, 1M-context, tool-calling stealth model appeared on a
provider's list, ran an entire agent estate for nothing for six days, and was
withdrawn. Nobody announced either event. It was found by accident and lost to
a health check. This looks for the next one, on a schedule, and can promote it.

It ADOPTS only through a gate a marginal model cannot pass:

  * OpenRouter only. Other sources alert but never auto-adopt, because they
    reach the framework through a single-slot provider the writer does not own.
  * context at least adopt_min_ctx, and never smaller than the incumbent's.
  * THREE consecutive clean tool-call probes, spaced. One lucky 200 is how a
    model that tool-calls 60% of the time gets promoted to running everything.
  * latency within adopt_latency_factor of the incumbent MEASURED IN THE SAME
    RUN, and under an absolute ceiling regardless. Free is not a saving if
    every agent turn takes nine seconds.
  * a cooldown, so the primary cannot be swapped twice in a week.

If the incumbent itself will not probe cleanly this run there is no honest
baseline, so nothing is adopted.

Reverting is NOT this tool's job -- that is the canary, which probes the live
primary on a short cycle and walks a fallback ladder. A model is recorded once
probed, so one that was adopted and then failed away is never silently
re-adopted.

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


def fetch(url: str, key: str, ua: str, timeout: int = 30):
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + key, "User-Agent": ua})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


# ------------------------------------------------------------------ sources

def openrouter_candidates(cfg, key):
    """Free on BOTH prompt and completion, advertises tools, big enough.

    Metadata is authoritative on PRICE here and unreliable on CAPABILITY, so
    price is read and tool support is probed.
    """
    out = []
    for m in fetch(cfg["openrouter"]["models"], key, cfg["user_agent"])["data"]:
        p = m.get("pricing") or {}
        try:
            if float(p.get("prompt", 1)) != 0 or float(p.get("completion", 1)) != 0:
                continue
        except (TypeError, ValueError):
            continue
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        ctx = m.get("context_length") or 0
        if ctx < cfg["radar"]["min_ctx"]:
            continue
        out.append({"id": m["id"], "ctx": ctx, "created": m.get("created") or 0,
                    "source": "openrouter"})
    return out


def zen_candidates(cfg, key):
    """A source that publishes NO pricing at all.

    Price cannot be read, so billing becomes the detector: on an unfunded
    workspace a paid model returns a credit error and a free one returns 200.
    We additionally require the response's own cost field to be zero, so this
    keeps working if the workspace is ever funded and that signal disappears.
    Cheap: only ids never seen before are probed.
    """
    return [{"id": m["id"], "ctx": None, "created": m.get("created") or 0,
             "source": "zen"}
            for m in fetch(cfg["zen"]["models"], key, cfg["user_agent"])["data"]]


# ------------------------------------------------------------------- state

def state_file(cfg) -> Path:
    return Path(cfg["state_dir"]) / "radar.json"


def get_state(cfg) -> dict:
    try:
        return json.loads(state_file(cfg).read_text())
    except Exception:                                         # noqa: BLE001
        return {"seen": {}}


def put_state(cfg, st) -> None:
    state_file(cfg).write_text(json.dumps(st, indent=1, sort_keys=True))


# ---------------------------------------------------------------- adoption

def confirm(cfg, model, key):
    """N consecutive clean probes. Returns (ok, worst_latency, why).

    Any transient counts as a FAILURE here. For a candidate about to run the
    estate, contention is a disqualifier, not noise -- the opposite of how the
    same signal is treated during discovery, and deliberately so.
    """
    r = cfg["radar"]
    worst = 0.0
    for i in range(r["adopt_confirms"]):
        if i:
            time.sleep(r["adopt_confirm_gap_s"])
        ok, _kind, note, lat, _cost = probe(
            cfg["openrouter"]["chat"], model, key, cfg["user_agent"])
        log("    confirm %d/%d %-38s %s %ss %s"
            % (i + 1, r["adopt_confirms"], model,
               "BUSY" if ok is None else ("PASS" if ok else "FAIL"), lat, note[:60]))
        if not ok:
            return False, worst, ("busy/rate-limited" if ok is None else note[:80])
        worst = max(worst, lat)
    return True, worst, "ok"


def try_adopt(cfg, adapter, hits, key, st, dry):
    """Returns (adopted_hit, targets, reason_if_not)."""
    r = cfg["radar"]
    targets = r["adopt_targets"]
    if not targets:
        return None, [], "no adopt_targets configured"

    or_hits = [h for h in hits if h["source"] == "openrouter"
               and (h["ctx"] or 0) >= r["adopt_min_ctx"]]
    if not or_hits:
        return None, [], ("no OpenRouter hit at or above %dk ctx"
                          % (r["adopt_min_ctx"] // 1000))

    last = st.get("last_adopt_ts") or 0
    age_d = (time.time() - last) / 86400.0
    if last and age_d < r["adopt_cooldown_days"]:
        return None, [], ("cooldown -- last adoption was %.1f days ago, minimum %d"
                          % (age_d, r["adopt_cooldown_days"]))

    inc = adapter.read(targets[0])
    if not inc or inc.provider != "openrouter":
        return None, [], ("%s is not on an OpenRouter primary (a failover may be "
                          "in effect) -- not adopting over a revert" % targets[0])

    # Baseline in THIS run, same network, same endpoint. A latency constant
    # measured last week compares against conditions that no longer exist.
    iok, _k, inote, ilat, _c = probe(
        cfg["openrouter"]["chat"], inc.model, key, cfg["user_agent"])
    if not iok:
        return None, [], ("incumbent %s would not probe cleanly (%s) -- no honest "
                          "baseline, not adopting" % (inc.model, inote[:60]))
    log("  incumbent %s baseline %ss" % (inc.model, ilat))
    ceiling = min(r["adopt_latency_abs_max_s"], ilat * r["adopt_latency_factor"])

    or_hits.sort(key=lambda h: (-(h["ctx"] or 0), h["latency"]))
    why = []
    for h in or_hits:
        if (h["ctx"] or 0) < inc.context_length:
            why.append("%s: %dk ctx is under the incumbent's %dk"
                       % (h["id"], (h["ctx"] or 0) // 1000, inc.context_length // 1000))
            continue
        if h["latency"] > ceiling:
            why.append("%s: %ss first probe is over the %.1fs ceiling"
                       % (h["id"], h["latency"], ceiling))
            continue
        log("  gate: confirming %s (%d consecutive probes)" % (h["id"], r["adopt_confirms"]))
        ok, worst, note = confirm(cfg, h["id"], key)
        if not ok:
            why.append("%s: failed confirmation -- %s" % (h["id"], note))
            continue
        if worst > ceiling:
            why.append("%s: worst confirm latency %ss over the %.1fs ceiling"
                       % (h["id"], worst, ceiling))
            continue

        done = []
        for t in targets:
            cur = adapter.read(t)
            if cur is None:
                log("  %s: no such target -- skipped" % t)
                continue
            # Never leave the free fallback pointing at the model just promoted:
            # a fallback sharing the primary's failure mode is not a fallback.
            fbs = [e for e in (cur.fallbacks or [])
                   if e.get("model") != h["id"]]
            new = adapters.Primary(
                provider="openrouter", model=h["id"],
                context_length=h["ctx"] or 0,
                free_fallback=(None if cur.free_fallback == h["id"]
                               else cur.free_fallback),
                fallbacks=fbs)
            if dry:
                log("  DRY RUN would repoint %s -> %s" % (t, h["id"]))
                done.append(t)
                continue
            try:
                adapter.write(t, new, "radar-adopt")
                done.append(t)
            except Exception as e:                            # noqa: BLE001
                # A half-applied swap is worse than none, but each write backs
                # up its own file, so report loudly and STOP rather than press on.
                log("  %s: WRITE FAILED (%s) -- stopping, %s already changed"
                    % (t, e, done or "nothing"))
                break
        if not done:
            return None, [], "%s passed every gate but no target was repointed" % h["id"]
        if not dry:
            st["last_adopt_ts"] = time.time()
            st.setdefault("adopted", []).append(
                {"id": h["id"], "ts": time.time(), "targets": done,
                 "displaced": inc.model, "latency": worst, "ctx": h["ctx"]})
        return dict(h, confirm_latency=worst, displaced=inc.model,
                    baseline=ilat), done, None
    return None, [], "; ".join(why) or "no candidate cleared the gate"


# -------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="model-radar", description=__doc__.split("\n")[0])
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and report, write no config and no state")
    ap.add_argument("--no-adopt", action="store_true",
                    help="alert only, never repoint anything")
    args = ap.parse_args(argv)

    cfg = cfgmod.load(args.config)
    envf = cfg.get("adapter", {}).get("env_file")
    or_key = cfgmod.secret("OPENROUTER_API_KEY", envf)
    zen_key = cfgmod.secret("ZEN_API_KEY", envf) or cfgmod.secret("OPENAI_API_KEY", envf)
    adapter = adapters.get(cfg["adapter"]["name"], cfg["adapter"])

    st = get_state(cfg)
    seen = st.setdefault("seen", {})
    first_run = not seen

    cands = []
    for name, fn, key in (("openrouter", openrouter_candidates, or_key),
                          ("zen", zen_candidates, zen_key)):
        if not key:
            log("%s: no credential, skipped" % name)
            continue
        try:
            got = fn(cfg, key)
            log("%s: %d candidate(s) listed" % (name, len(got)))
            cands += got
        except Exception as e:                                # noqa: BLE001
            # A source being unreachable is not evidence of anything.
            log("%s: source unreachable (%s) -- skipped, not treated as news"
                % (name, type(e).__name__))

    fresh = [c for c in cands if c["id"] not in seen]
    log("%d new model id(s) since last run" % len(fresh))
    fresh.sort(key=lambda c: -c["created"])   # a stealth preview is a recent id

    by_source, deferred, retry, hits = {}, 0, 0, []
    cap = cfg["radar"]["max_probes_per_source"]
    for c in fresh:
        n = by_source.get(c["source"], 0)
        if n >= cap:
            # Deliberately NOT recorded as seen: an unprobed model must stay a
            # candidate, or a per-run cap buries it forever.
            deferred += 1
            continue
        by_source[c["source"]] = n + 1
        src = cfg["openrouter"] if c["source"] == "openrouter" else cfg["zen"]
        key = or_key if c["source"] == "openrouter" else zen_key
        ok, _kind, note, lat, cost = probe(src["chat"], c["id"], key, cfg["user_agent"])
        free = ok and (c["source"] == "openrouter"
                       or str(cost) in ("0", "0.0", "0.00", "None"))
        log("  probe %-42s %s %ss %s"
            % (c["id"], "BUSY" if ok is None else ("PASS" if ok else "FAIL"),
               lat, note[:70]))
        if ok is None:
            # Unreachable != dead. Leave it unseen so a later run retries it,
            # or a rate-limited free model is buried on first contact.
            retry += 1
            continue
        seen[c["id"]] = {"ts": time.time(), "ok": ok, "note": note[:120],
                         "ctx": c["ctx"], "latency": lat}
        if ok and free:
            hits.append(dict(c, latency=lat, cost=cost))

    if retry:
        log("%d candidate(s) were busy (429/5xx/timeout) -- left unseen for a "
            "later run rather than written off" % retry)
    if deferred:
        log("%d candidate(s) deferred past the %d/source probe cap -- they stay "
            "unseen and get probed next run" % (deferred, cap))
    if not args.dry_run:
        put_state(cfg, st)

    if first_run:
        log("first run -- %d id(s) recorded as the baseline, no alert sent" % len(seen))
        return 0
    if not hits:
        log("nothing new worth alerting on")
        return 0

    head = cfg["radar"]["headline_ctx"]
    lines, top = [], False
    for h in hits:
        ctx = h["ctx"]
        big = ctx is not None and ctx >= head
        top = top or big or ctx is None
        lines.append("- `%s` (%s) - %s ctx, %ss, free%s"
                     % (h["id"], h["source"],
                        ("%dk" % (ctx // 1000)) if ctx else "unknown",
                        h["latency"], " - **headline class**" if big else ""))
    found = "\n".join(lines)

    if args.no_adopt or not cfg["radar"]["adopt"]:
        won, tgts, why = None, [], "adoption disabled"
    else:
        won, tgts, why = try_adopt(cfg, adapter, hits, or_key, st, args.dry_run)
        if not args.dry_run:
            put_state(cfg, st)

    if won:
        notify(cfg, "**Free model ADOPTED as primary**\n%s\n\n"
                    "`%s` -> **`%s`** on %s%s.\n"
                    "Cleared the gate: %d consecutive tool-call probes, worst %ss "
                    "against the incumbent's %ss in the same run, %dk ctx."
                    % (found, won["displaced"], won["id"], ", ".join(tgts),
                       " (DRY RUN - nothing written)" if args.dry_run else "",
                       cfg["radar"]["adopt_confirms"], won["confirm_latency"],
                       won["baseline"], (won["ctx"] or 0) // 1000))
        return 0

    notify(cfg, "**New free tool-calling model%s spotted**\n%s\n\n**Not adopted:** %s."
                % ("s" if len(hits) > 1 else "", found, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
