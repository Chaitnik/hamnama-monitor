# HamNama Uptime Monitor

A tiny, serverless uptime monitor for HamNama that runs entirely inside
GitHub Actions on a 5-minute schedule. No VPS, no Docker, no database
server. It checks `https://hamnama.net` and `https://hamnama.net/api/health`
with real HTTP requests from the GitHub-hosted runner, and notifies
subscribers over Telegram when the site goes down or recovers.

## How it works, in short

Every 5 minutes GitHub Actions spins up a fresh Ubuntu runner, checks out
this repo, and runs `monitor.py` once. That single run:

1. Loads `state.json` (subscribers, failure counters, last results) from the
   repo checkout.
2. Makes real HTTP GET requests to both URLs and records status code, final
   URL, redirects, timing, and any error.
3. Updates consecutive failure/success counters and decides whether to send
   a DOWN or recovery alert.
4. Fetches any pending Telegram messages (`/start`, `/status`) via
   `getUpdates` and replies to them.
5. Writes the updated `state.json` and the workflow commits it back to the
   repository so the next run (a brand new, unrelated runner) has continuity.

The process then exits. Nothing is "always running."

## 1. Create the Telegram bot

1. Open a chat with [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot` and follow the prompts (choose a name and a unique
   username ending in `bot`).
3. BotFather replies with an API token that looks like
   `123456789:AAH...`. This is your `TELEGRAM_BOT_TOKEN`. Keep it secret —
   anyone with this token can send messages as your bot.
4. Optionally set a description/about text with `/setdescription` and
   `/setabouttext`.

You do **not** need to enable inline mode, set a webhook, or configure
anything else — this bot only ever uses `getUpdates` and `sendMessage`.

## 2. Add the required GitHub Secret

In your repository: **Settings → Secrets and variables → Actions → New
repository secret**.

| Secret name          | Value                                   |
|-----------------------|------------------------------------------|
| `TELEGRAM_BOT_TOKEN`  | The token BotFather gave you             |

No other secrets are required. Persistence uses the repository itself (see
below), via the automatically provided `GITHUB_TOKEN`, so nothing extra
needs to be configured for that.

The token is only ever read from the environment (`TELEGRAM_BOT_TOKEN`) and
is never printed to logs or written into `state.json`.

## 3. Push this project to a repository

Copy all of these files into a new (or existing) GitHub repository:

```
monitor.py
requirements.txt
state.json
.gitignore
README.md
.github/workflows/monitor.yml
```

Commit and push to the default branch (e.g. `main`).

## 4. Enable the GitHub Action

Once pushed, the workflow `HamNama Uptime Monitor` appears under the
**Actions** tab and starts running automatically on its 5-minute schedule
(GitHub only activates scheduled workflows once they exist on the default
branch — no extra toggle is needed, but if Actions were previously disabled
for the repo, enable them under **Settings → Actions → General**).

### Manually trigger a check

Go to **Actions → HamNama Uptime Monitor → Run workflow**. This runs
`workflow_dispatch`, the exact same code path as the scheduled run — useful
for testing before waiting for the next 5-minute tick.

## 5. Subscribe to alerts

In Telegram, open a chat with your bot and send:

```
/start
```

The bot registers your chat ID as a subscriber (persisted in `state.json`)
and replies with a welcome message that includes the **current status
immediately**, so you're not left wondering whether things are okay after
subscribing. Sending `/start` again is harmless — you won't be registered
twice, and you'll just get the welcome message with the latest status again.

## 6. Check current status any time

Send:

```
/status
```

The bot replies with the most recently persisted check: overall status,
per-endpoint HTTP status/timing/redirects/errors, consecutive failure count,
incident start and approximate downtime if currently unhealthy, and the last
successful/failed check timestamps. If no check has run yet, it says so
explicitly instead of showing stale or fake data.

## Alerting behavior

The monitor tracks three independent streak counters — consecutive
`HEALTHY`, consecutive `DEGRADED`, and consecutive `DOWN` runs — and
remembers the last status subscribers were actually alerted about
(`alert_state`). A Telegram alert fires only on a **confirmed transition**
away from `alert_state`, once the relevant streak threshold is met:

- **🔴 DOWN alert**: sent once, the first time *both* endpoints have failed
  for `MONITOR_FAILURE_THRESHOLD` consecutive runs (default **2**, i.e.
  roughly 10 minutes of continuous failure at the 5-minute schedule). This
  avoids paging everyone over a single transient network blip.
- **🟡 DEGRADED alert**: sent once per transition, after
  `MONITOR_DEGRADED_THRESHOLD` consecutive runs (default **1**) where
  *exactly one* of the two endpoints is failing — e.g. the API returns 500
  while the homepage loads fine, or vice versa. This is deliberately fast
  and low-threshold, since a partial outage is worth flagging quickly even
  though it isn't a full DOWN.
- **🟢 Recovery alert**: sent once, after `MONITOR_RECOVERY_THRESHOLD`
  consecutive fully-`HEALTHY` runs (default **1**) following a DOWN or
  DEGRADED alert. Recovery only fires on genuine `HEALTHY` — going from
  DOWN to DEGRADED is treated as its own DEGRADED alert, never silently
  counted as "recovered."
- Below a threshold, no alert is sent — only `/status` reflects the
  in-progress streak. If a DOWN or DEGRADED streak heals before crossing
  its threshold, it's forgotten without ever having alerted anyone.
- Alert text is built strictly from what was actually observed (HTTP status
  codes, exception messages). The monitor never invents a root cause (e.g.
  it will never claim "the backend crashed" — it reports "HTTP 502" or
  "DNS resolution failed" etc., exactly as observed).

All three thresholds are configurable without code changes via environment
variables, settable in the workflow file:

```yaml
env:
  MONITOR_FAILURE_THRESHOLD: "2"
  MONITOR_DEGRADED_THRESHOLD: "1"
  MONITOR_RECOVERY_THRESHOLD: "1"
  MONITOR_TIMEOUT_SECONDS: "10"
```

## What "unhealthy" means

- **`/api/health`**: any HTTP 2xx response is healthy. If the body is JSON,
  a bounded summary (top-level keys / length / truncated raw body) is
  recorded for `/status`, but no particular schema is assumed or required.
- **Homepage (`https://hamnama.net`)**: requested with redirects followed
  (like `curl -L`). The *final* response after following redirects must be
  2xx. A reachable TCP connection is not suffient — the actual HTTP status
  is what's checked. This catches Nginx 502/504/500, "connection refused",
  DNS failures, TLS failures, and redirect loops/failures — not just "is the
  box pingable."
- **Overall status** combines both:
  - `HEALTHY` — both endpoints passed.
  - `DEGRADED` — exactly one passed.
  - `DOWN` — both failed.

Sustained `DOWN` and sustained `DEGRADED` each trigger their own alert (see
"Alerting behavior" above) — a broken API behind a working homepage is
reported to subscribers too, just with a 🟡 message instead of a 🔴 one.

## Persistent state

GitHub Actions runners are ephemeral — every run starts from a clean
checkout with no memory of previous runs. This project persists state as a
single JSON file, `state.json`, committed back into the same Git repository
at the end of each run (only when it actually changed).

`state.json` contains: registered Telegram subscriber chat IDs, the
Telegram `getUpdates` offset (so old commands aren't reprocessed), the
latest and previous check results, consecutive failure/success counters,
the current incident's start time, whether a DOWN alert has already been
sent for the current incident, and the last successful/failed check
timestamps.

This is intentionally simple: no external database, no additional secrets,
no third-party storage service. Limitations to be aware of:

- **Not atomic across concurrent runs.** The workflow uses a
  `concurrency` group (`hamnama-monitor`) so GitHub queues runs rather than
  overlapping them, and the commit step does a `git pull --rebase` before
  pushing as a second safety net. Under normal 5-minute scheduling this is
  never actually contended.
- **Every state change is a Git commit.** Over a year this is roughly
  100,000 small commits to `state.json`. This is harmless for a small repo
  but is worth knowing; squash or `git gc` the repo's history occasionally
  if that bothers you.
- **`[skip ci]` is used in the commit message** so pushing the state update
  doesn't itself trigger other workflows you might add later that run on
  `push`.
- **Requires `contents: write` permission** for the workflow's `GITHUB_TOKEN`
  (already configured in `monitor.yml`). If your organization restricts the
  default token's permissions repo-wide, grant this workflow write access
  explicitly under **Settings → Actions → General → Workflow permissions**.

## Telegram polling model

This bot never runs a long-lived polling loop. Instead, each 5-minute
Action run calls Telegram's `getUpdates` once with the persisted `offset`
(the ID of the last update already processed), handles any new `/start` or
`/status` messages, advances the offset past them, and exits. Because the
offset is persisted in `state.json`, a command is processed exactly once
even though the process handling it is a brand-new one each time — and
commands sent between runs simply wait in Telegram's queue until the next
run picks them up (typically within 5 minutes).

## Reliability behavior

- If one endpoint's request throws an exception, it's recorded as a failure
  for that endpoint only; the other endpoint is still checked, and Telegram
  commands are still processed.
- If Telegram is unreachable or returns an error, the health check itself
  still completes and is still persisted; a Telegram failure never marks
  HamNama itself as unhealthy, and never crashes the run. It's logged to
  the workflow's stderr instead.
- If a `sendMessage` call fails for one subscriber (e.g. they blocked the
  bot), that failure is caught and logged; it does not stop delivery to
  other subscribers or abort the run.

## GitHub Actions limitations (outside this project's control)

- GitHub does not guarantee scheduled workflows fire at the exact minute —
  under platform load, cron-triggered runs can be delayed by several
  minutes, or in rare cases skipped. This means alert latency can be worse
  than "5 minutes" during GitHub-side incidents.
- GitHub may disable scheduled workflows automatically after **60 days of
  repository inactivity** (no pushes/commits). Pushing any commit (including
  the automated state commits this monitor makes) resets that clock, so an
  actively-alerting monitor keeps itself enabled; a repo that goes fully
  quiet with the monitor also silent could stop scheduling. If you see the
  schedule stop, re-enable it manually from the Actions tab.
- Each run has a hard `timeout-minutes: 5` set in the workflow so a stuck
  run can't linger into the next scheduled one.

## Testing it end-to-end

1. Deploy as above and confirm `/status` returns a `HEALTHY` report after
   the first run (or trigger one manually).
2. To simulate an outage without touching HamNama, temporarily point the
   monitor at a URL you control that returns an error, or temporarily break
   connectivity to `hamnama.net` (e.g. via a firewall rule) if you manage
   that infrastructure — but the simplest safe test is:
   - Temporarily edit `API_HEALTH_URL` or `HOMEPAGE_URL` in `monitor.py` (in
     a throwaway branch, not `main`) to point at `https://httpstat.us/502`
     or a nonexistent hostname, then run `workflow_dispatch` twice in a row
     (or wait for two scheduled runs) — you should receive a 🔴 DOWN alert
     after the second consecutive failure.
   - Revert the URL and trigger the workflow twice more — you should
     receive a 🟢 recovery alert after the second consecutive healthy run.
3. Send `/status` at any point during the test to see the live counters
   (`consecutive_failures`, incident start, downtime estimate) update.

Do not run this kind of test against the real `hamnama.net` URLs, since it
would (correctly) alert your real subscribers.
