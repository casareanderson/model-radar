# Setting up model-radar

About ten minutes, most of it waiting for probes. You get two commands:

- `model-radar` finds free models that really make tool calls.
- `model-canary` watches your paid primary model and your credit, and fails over when either goes.

The blocks marked as tested run on a fresh Ubuntu runner every week in
[`setup-check`](../.github/workflows/setup-check.yml). CI has no API key, so it proves the install, the config
and the no-key behaviour. The steps that call OpenRouter need your key and are marked as not tested.


![What a passing run looks like on a machine with no API key (excerpt from `setup-check` on 11 October 2026)](setup-run.png)

*What a passing run looks like on a machine with no API key (excerpt from `setup-check` on 11 October 2026).*

## What you need

- Python 3.11 or newer. It uses `tomllib` from the standard library, so there's nothing to `pip install`.
- An OpenRouter API key for the real thing. Probing free models costs nothing. Probing a paid primary costs one
  short completion per check.
- Optional: an OpenCode Zen key (`ZEN_API_KEY`, or `OPENAI_API_KEY` as a fallback) for the second source, a
  Discord webhook for alerts, and `PyYAML` if you'll use the `hermes` adapter.

```sh setup-check
python3 -c "import sys, tomllib; assert sys.version_info >= (3, 11); print(sys.version.split()[0], 'ok')"
```

If that fails with `ModuleNotFoundError: No module named 'tomllib'`, your Python is older than 3.11.

## Install

```sh setup-check
git clone -q https://github.com/casareanderson/model-radar.git
cd model-radar
cp config.example.toml config.toml
./bin/model-radar --help
./bin/model-canary --help
```

There's no installer. The two scripts in `bin/` run from the checkout; put it somewhere stable like
`/opt/model-radar` if you'll run it on a timer.

## First run without a key

Before you add a key, check it runs and fails safely. With no credentials both sources are skipped, and the
first run records an empty baseline:

```sh setup-check
env -u OPENROUTER_API_KEY -u ZEN_API_KEY -u OPENAI_API_KEY ./bin/model-radar --dry-run
```

```
[...] openrouter: no credential, skipped
[...] zen: no credential, skipped
[...] 0 new model id(s) since last run
[...] first run -- 0 id(s) recorded as the baseline, no alert sent
```

The canary refuses outright and exits `1`, which is what you want a timer to notice:

```sh setup-check
env -u OPENROUTER_API_KEY -u ZEN_API_KEY -u OPENAI_API_KEY ./bin/model-canary --dry-run || [ $? -eq 1 ]
```

```
[...] no OPENROUTER_API_KEY -- nothing to check
```

## First real run

Not tested in CI: this needs your key.

```
export OPENROUTER_API_KEY=sk-or-...
./bin/model-radar --dry-run
```

You get a count of candidates, one `probe` line per model with `PASS`, `FAIL` or `BUSY`, and a closing line, like
[the screenshot in the README](../README.md#model-radar). `BUSY` means the model was rate-limited or timed out:
it isn't counted as a verdict and is tried again next run. `--dry-run` writes no config and no state, so every
dry run counts as a first run and sends no alert.

To start the real record, run it once without `--dry-run`. With `adopt = false` (the default) it never touches
your agents' config:

```
./bin/model-radar --no-adopt
```

That run saves what it saw to `~/.local/state/model-radar/radar.json`. From then on it only probes models it
hasn't seen, and alerts when a new free one makes a real tool call.

## Set up the canary

The canary needs something to watch. The `jsonfile` adapter reads and writes a plain JSON file that your own
launcher reads:

```
mkdir -p ~/.config/model-radar
cat > ~/.config/model-radar/models.json <<'EOF'
{"agents": {"coder": {"provider": "openrouter", "model": "qwen/qwen3.7-flash", "context_length": 1000000}}}
EOF
./bin/model-canary --dry-run
```

It prints your remaining credit and `primary <model> ok (<n>s)`, or what it would fail over to. Change the model
to whatever your agent really uses.

Then fill in the fallbacks in `config.toml`:

- `[canary] paid_ladder`: cheaper paid models to try, in order. Each one is probed before it's used.
- `[canary.local]`: a local model, for example an Ollama endpoint, used when credit drops below
  `credit_floor_usd` or nothing on the ladder answers.

If you run [hermes-agent](https://hermes-agent.nousresearch.com), set `adapter.name = "hermes"` (and install
`PyYAML`) to have it repoint hermes profiles directly. Every write backs up the old file and re-reads the new one.

## Configure

Settings come from built-in defaults, then a TOML file: `--config PATH`, or `$MODELRADAR_CONFIG`, or
`./config.toml`. Secrets never come from the TOML file.

| environment variable | needed | what it does |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | listing, probing and the credit balance |
| `ZEN_API_KEY` (falls back to `OPENAI_API_KEY`) | no | the OpenCode Zen source; skipped without it |
| `MODELRADAR_DISCORD_WEBHOOK` | no | Discord alerts; the variable's name is set by `notify.discord_webhook_env` |
| `MODELRADAR_CONFIG` | no | path to the TOML file |

The settings people change first:

| key | default | what it does |
|---|---|---|
| `radar.min_ctx` | `200000` | smallest context a candidate may have |
| `radar.max_probes_per_source` | `8` | probes per source per run; the rest wait for the next run |
| `radar.adopt` | `false` | let it repoint an agent to a free model at all; read the gate in the README first |
| `canary.fails_before_failover` | `2` | consecutive hard failures before failing over; timeouts and 429s never count |
| `canary.credit_floor_usd` / `credit_warn_usd` | `1.50` / `4.00` | below the floor, go local; below warn, alert |
| `adapter.env_file` | unset | read keys from this file when they aren't in the environment |

Every key is in [config.example.toml](../config.example.toml) and the [README](../README.md#configuration).

## Run it on a timer

The canary every few hours, the radar daily:

```cron
30 4 * * *  cd /opt/model-radar && ./bin/model-radar  >> /var/log/model-radar.log 2>&1
0 */3 * * * cd /opt/model-radar && ./bin/model-canary >> /var/log/model-canary.log 2>&1
```

Cron doesn't see your shell's `export`s. Put the key in the crontab's environment, or set `adapter.env_file` to a
file only you can read (`chmod 600`).

Watch the canary's exit code as well as its alerts. A broken webhook only logs to stdout, and a failed alert never
stops the check.

## Common problems

| you see | what it means | what to do |
|---|---|---|
| `openrouter: no credential, skipped` | no `OPENROUTER_API_KEY` in the environment it ran in | export it, or set `adapter.env_file` |
| `no OPENROUTER_API_KEY -- nothing to check` (exit 1) | same, from the canary | as above |
| a model probes `FAIL ... no tool_call` | it answered in prose instead of calling the tool | that's a real result: it can't drive an agent |
| `403` on a free model | some free models only serve "agentic harnesses" | a real result too; it's recorded and not retried |
| lots of `BUSY` | free pools are rate-limited | normal; they're retried next run |
| `error 1010` from Cloudflare | a request without a browser-like user agent | keep `user_agent` at its default |
| canary exits 1 with credit below the floor | no `canary.local` model configured to fall back to | add one, or top up |
| `ModuleNotFoundError: No module named 'yaml'` | the `hermes` adapter needs PyYAML | `pip install PyYAML`, or use `jsonfile` |

## Update

```
cd /opt/model-radar
git pull
```

Your `config.toml`, `~/.local/state/model-radar/` and `~/.config/model-radar/models.json` aren't in git, so they
stay as they are.

## Uninstall

Remove the cron lines, then:

```
rm -rf /opt/model-radar ~/.local/state/model-radar ~/.config/model-radar
```

Model ids that were adopted or failed over to stay in your agents' config; each change left a `<file>.bak-<tag>-<timestamp>` copy
next to the file it changed.
