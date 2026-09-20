# model-radar

Two small, deterministic tools for people running LLM agents against hosted
model APIs:

- **`model-radar`** — finds free, *genuinely tool-calling* models and can promote
  one to primary, behind a gate a marginal model cannot pass.
- **`model-canary`** — checks the live primary is still serving **and** that
  there is still credit to pay for it, and fails over when there isn't.

Neither asks a model anything. They gather facts and act on them.

---

## Why this exists

A free, 1M-context, tool-calling model appeared on a provider's list with no
announcement. It ran an entire agent estate for nothing for six days. Then it
was withdrawn, also with no announcement, and everything started hard-erroring.

Both events were invisible. That is the actual problem: **the model you depend
on can appear and disappear without anyone telling you**, and most agent
frameworks fall back on rate-limit and connection errors only — not on *"model
does not exist"* and not on *"insufficient credit"*.

While this README was being written, the provider region-gated the model behind
it. The canary caught it on two consecutive checks and moved the estate to the
next rung of the ladder before anyone noticed:

```
[18:00:03] go: hard failure #1 -- HTTP 403: RegionError ...
[21:00:02] go: hard failure #2 -- HTTP 403: RegionError ...
[21:00:03] Primary failed over -- deepseek-v4-flash -> deepseek-v4-flash-0731
```

## The three ideas worth stealing

Even if you never run this, these are the parts that took real incidents to learn.

### 1. A 200 is not a capability test

Provider metadata lies. On OpenRouter, *every* free model advertises tool
support; several then return `403 only available on agentic harnesses` the
moment you use one. So the probe doesn't ping — it asks for a **specific tool
call with a specific argument** and checks what came back:

```
probe nemotron-3-ultra-free        FAIL 25.51s  no tool_call
probe mimo-v2.5-free               PASS  4.34s  ok
probe ling-3.0-flash-fin-free      PASS  1.36s  ok
```

`nemotron-3-ultra-free` answers perfectly well. It just answers in **prose**,
which is useless to an agent. Price comes from metadata; capability is measured.

### 2. Unreachable is not dead

Every probe returns one of **three** outcomes, not two: `ok`, `hard`,
`transient`. A 429 or a 5xx means the provider is busy — it is *not* a verdict.

Conflating those two costs you in both directions. During discovery, treating a
429 as a failure buries a good free model on first contact, so busy candidates
are deliberately **left unrecorded** and retried next run. During monitoring,
treating a blip as death fails a healthy primary over on one bad minute — this
rule is in here because the opposite once produced a restart storm against a
service that was never down. Only a definitive verdict, confirmed on **N
consecutive runs**, moves anything.

The one place that inverts: during *adoption confirmation*, a transient counts
as a failure. For a candidate about to run everything, contention is a
disqualifier, not noise.

### 3. Free is not a saving if it's slow

The adoption gate, all of which must hold:

| Gate | Why |
|---|---|
| Context ≥ threshold, and ≥ the incumbent's | A smaller window is a downgrade, whatever it costs |
| **Three consecutive** clean probes, spaced | One lucky 200 is how a model that tool-calls 60% of the time gets promoted |
| Latency within a factor of the incumbent, **measured in the same run** | A constant from last week compares against conditions that no longer exist |
| An absolute latency ceiling | The first real run found 6.2s and 9.1s candidates against an incumbent's 1.5s |
| A cooldown | The primary must not be swapped twice in a week |
| Incumbent probes clean this run | If it doesn't, there is no honest baseline, so nothing is adopted |

Adoption is **off by default**, and is meant to be pointed at your *less*
sensitive agents. Where prompts go is the whole question — an agent holding
credentials or authority should stay a human decision.

## Install

Python 3.11+ (uses `tomllib`). `PyYAML` only if you use the Hermes adapter.

```bash
git clone https://github.com/casareanderson/model-radar
cd model-radar
cp config.example.toml config.toml     # edit it
export OPENROUTER_API_KEY=sk-or-...

./bin/model-radar  --dry-run
./bin/model-canary --dry-run
```

Then run them on a timer — the canary often (every few hours), the radar daily.

**`--dry-run` on both probes and reports but writes nothing.** Start there.

## Adapters

Discovery, probing and the gate are provider-agnostic. Actually *pointing your
agent* at a different model is not — every framework stores that somewhere
different. That is all an adapter does.

- **`jsonfile`** — a plain JSON file your launcher reads. Works out of the box,
  and is a 60-line worked example of the contract.
- **`hermes`** — drives [hermes-agent](https://hermes-agent.nousresearch.com)
  profiles, where this was built and runs.

Writing your own means three methods: `targets()`, `read()`, `write()`. A `write`
must back up what it overwrites and re-read the result to prove it still parses.

## What it will not do

- It will not rescue you from a provider that vanishes with no fallback
  configured. Configure the local rung.
- It will not tell you a model is *good*. It measures tool-calling, latency and
  context. Quality is yours to judge.
- Credit checking is OpenRouter-specific — it is the balance endpoint that makes
  a floor possible at all.
- **A failed alert never takes down the check that produced it**, which also
  means a broken webhook fails quietly to stdout. Watch the exit codes too.

## Licence

MIT.

---

## The write-up

What running agents across your own hardware, OpenRouter and a coding
subscription actually costs, measured on a live estate — including the deadlock
that hangs a load for nine minutes with nothing in the log:

**[Token Routing →](https://asareanderson.gumroad.com/l/esuwce)** (£18) ·
[2-page cheat sheet](https://asareanderson.gumroad.com/l/ufdbr) (£4)

More field notes from the same estate: **[dev.to/c1-anderson](https://dev.to/c1-anderson)**
