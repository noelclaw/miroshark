# Channel Notifications

MiroShark fires a notification the moment a simulation reaches a terminal
state (`simulation.completed` or `simulation.failed`). Five independent
channels run in parallel — each opt-in via its own env var (or env-var
pair):

| Channel  | Env var                                       | Format               | Use case                                                      |
| -------- | --------------------------------------------- | -------------------- | ------------------------------------------------------------- |
| Webhook  | `WEBHOOK_URL`                                 | Raw JSON `POST`      | Zapier / Make / n8n / IFTTT / custom listeners                |
| Discord  | `DISCORD_WEBHOOK_URL`                         | Discord rich embed   | Discord channels — coloured cards with belief % fields        |
| Slack    | `SLACK_WEBHOOK_URL`                           | Slack Block Kit      | Slack channels — header + block-bar fields + action button    |
| Email    | `SMTP_HOST` + `SMTP_TO`                       | `multipart/alternative` | Any inbox or mailing list — no platform account required   |
| Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`     | Bot API `sendMessage` (HTML) | Telegram chats, groups, or channels — bot card with belief bars + inline button |

Channels are independent. Set one, several, or all five — each fires
separately. Unset env vars are silently skipped, so existing
deployments that only use the generic webhook are unaffected by this
feature.

The SPA exposes a public probe at `GET /api/config/notifications`
returning `{webhook_configured, discord_configured, slack_configured,
email_configured, telegram_configured}` so the operator can confirm
channel status without opening the backend config.

## Generic webhook (existing, PR #46)

Already documented in [WEBHOOKS.md](./WEBHOOKS.md). Posts a JSON blob
matching the payload shape in
[`backend/app/services/webhook_service.py`](../backend/app/services/webhook_service.py).

## Discord rich embed

Set `DISCORD_WEBHOOK_URL` to a Discord incoming webhook URL:

```bash
# Discord → Server Settings → Integrations → Webhooks → New Webhook
# Copy the webhook URL ("https://discord.com/api/webhooks/000/xxx").
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/000/xxx
```

On every terminal-state transition, MiroShark POSTs a single embed:

* **Title** — scenario, truncated to 100 chars.
* **Description** — one-line status verb ("Simulation reached its
  terminal round." or "Simulation ended in a failure state.").
* **Color** — green / grey / red / amber depending on the dominant
  consensus stance (failed runs always amber).
* **Fields** — Bullish %, Neutral %, Bearish %, Quality, Rounds,
  Agents, Resolution (when set).
* **Thumbnail** — share card PNG (only when `PUBLIC_BASE_URL` is set
  so the embed can render an absolute URL).
* **URL** — share page link (`/share/<sim_id>`), absolute when
  `PUBLIC_BASE_URL` is set.
* **Footer / timestamp** — "MiroShark" + the dispatch timestamp.

Failed runs append an additional `Error` field with the truncated
exit-code message.

Discord deduplicates per `(sim_id, status)` pair in the dispatching
process — the simulation runner's two terminal code paths (exit-code
monitor + the `simulation_end` event in the action log) both call
into the notifier but Discord only sees one card per terminal state.

The endpoint is fire-and-forget: a slow Discord endpoint never delays
the simulation runner, and a 4xx logs a warning without raising.

## Slack Block Kit

Set `SLACK_WEBHOOK_URL` to a Slack Incoming Webhook URL:

```bash
# api.slack.com/apps → your app → Incoming Webhooks → Add New Webhook to Workspace
# Copy the webhook URL ("https://hooks.slack.com/services/T0/B0/abc").
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T0/B0/abc
```

On every terminal-state transition, MiroShark POSTs a Block Kit
message with four blocks:

* **Header** — scenario, truncated to 120 chars.
* **Context** — bold status verb + monospaced sim id.
* **Section** — `mrkdwn` fields:
  * Bullish / Neutral / Bearish with Unicode block-bars
    (`█████░░░░░ 52.0%`).
  * Quality health.
  * Scale (`N agents · N rounds`).
  * Resolution (when set).
* **Actions** — a single "View simulation" button linking to
  `/share/<sim_id>`. Only emitted when `PUBLIC_BASE_URL` is set
  (Slack rejects buttons whose URL isn't absolute).

Failed runs append an `Error` section with a fenced code block
containing the truncated exit-code message.

Same dedup posture and fire-and-forget guarantees as Discord.

## SMTP completion email

The one notification channel that requires no platform account, no
OAuth flow, and no incoming-webhook URL. Set `SMTP_HOST` plus a
comma-separated `SMTP_TO` recipient list and MiroShark sends a
`multipart/alternative` email (plain text + HTML) to every recipient
on every terminal-state transition:

```bash
# Minimal config — unauthenticated relay (self-hosted Postfix, LAN MX)
SMTP_HOST=relay.internal
SMTP_PORT=25
SMTP_TO=research@example.com

# Authenticated config (Gmail / SendGrid / Mailgun / any hosted relay)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=miroshark-bot@example.com
SMTP_PASSWORD=<gmail-app-password>     # NOT your regular password
SMTP_FROM=alerts@miroshark.app         # optional — defaults to miroshark-notify@<host>
SMTP_TO=research@example.com,ops@example.com
```

Body structure (both parts):

* **Subject** — `[MiroShark] <Direction>: <Scenario>` where
  `<Direction>` is one of `Bullish` / `Neutral` / `Bearish` /
  `Failed`. Inbox filters can triage on this alone — no body parse
  needed.
* **Plain-text part** — Scenario header, then key/value pairs:
  `Status`, `Bullish` / `Neutral` / `Bearish` (each with the same
  Unicode block bar Slack uses — `█████░░░░░ 52.0%`), `Quality`,
  `Scale`, `Outcome`, and an absolute `View:` URL. Reads cleanly in
  mutt / Apple Mail / Outlook list-view previews.
* **HTML part** — Same fields in a single `<table>` (the only
  layout Outlook / Gmail / Apple Mail render consistently), with
  inline-CSS colour swatches matching the Discord embed border, a
  consensus-coloured top border, and a "View simulation →" button
  CTA (only when `PUBLIC_BASE_URL` is set so the URL is absolute).
* **Headers** — `X-MiroShark-Sim-Id: <sim_id>` and
  `X-MiroShark-Event: simulation.{completed,failed}` so server-side
  filters (Sieve / Gmail filter / Outlook rule) can route without
  scanning the subject.

Failure runs append the truncated exit-code message as an `Error`
section (HTML: amber-bordered code block; plain: `Error:` block).

### Transport selection

The dispatcher picks the SMTP class by port:

| Port | Transport     | When to use                                                       |
| ---- | ------------- | ----------------------------------------------------------------- |
| 465  | `SMTP_SSL`    | Implicit TLS (legacy SMTPS).                                      |
| 587  | `SMTP` + STARTTLS | Submission port — the modern default; what Gmail / SendGrid expect. |
| 25   | `SMTP` (plain)    | Internal LAN relays — set `SMTP_USE_TLS=false`.                  |

If STARTTLS fails on port 587 *and* credentials are configured, the
dispatcher refuses to send rather than leak credentials in cleartext.
On unauthenticated runs (no `SMTP_USER`/`SMTP_PASSWORD`), a failed
STARTTLS falls back to plaintext so a LAN relay that doesn't speak
TLS still gets the message.

### Gmail recipe

1. Enable 2-Step Verification on the sender Google account.
2. Account → Security → App Passwords → generate one for "Mail."
3. `SMTP_USER=<gmail-address>`, `SMTP_PASSWORD=<16-char-app-password>`,
   `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`.
4. Set `SMTP_FROM` to the same address as `SMTP_USER` so the
   "From" header passes Gmail's outbound sender check.

### Test snippet

```python
# Verify a relay is reachable without touching MiroShark
import smtplib, ssl
with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as conn:
    conn.starttls(context=ssl.create_default_context())
    conn.login("you@gmail.com", "<app-password>")
    print("OK")
```

Dedup posture is identical to the Discord / Slack notifiers — the
runner's two terminal code paths both fire, but the per-process
`(sim_id, status)` set ensures the inbox sees exactly one message
per terminal state.

## Telegram Bot

Set `TELEGRAM_BOT_TOKEN` to a Bot API token and `TELEGRAM_CHAT_ID` to
the chat / group / channel the bot should post into:

```bash
# 1. On Telegram, talk to @BotFather, send /newbot, follow the
#    prompts. Copy the "123456789:AAEh…" token.
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE

# 2. Send the bot at least one message in the destination chat,
#    group, or channel, then read the chat id from:
#      https://api.telegram.org/bot<TOKEN>/getUpdates
#    For a private chat the id is positive (your user id); for a
#    group / supergroup it's negative ("-100…"); for a public
#    channel the bot is admin of you can also use "@channelname".
TELEGRAM_CHAT_ID=-100123456789
```

On every terminal-state transition, MiroShark calls Bot API
`sendMessage` with `parse_mode=HTML` and `disable_web_page_preview=true`:

* **Header** — scenario in bold, truncated to 200 chars.
* **Status line** — italicised status verb + monospaced sim id.
* **Belief bars** — `Bullish` / `Neutral` / `Bearish` lines with the
  same Unicode block-bar Slack uses (`█████░░░░░ 52.0%`), only when
  a trajectory was available.
* **Key/value block** — `Quality`, `Scale` (`N agents · N rounds`),
  `Outcome` (when set).
* **Direction tag** — explicit `Bullish` / `Bearish` / `Failed`
  label so the first notification-preview line is informative even
  on Android lock screens that clip after one line.
* **Inline-keyboard button** — a single "View simulation" button
  linking to `/share/<sim_id>`. Only emitted when `PUBLIC_BASE_URL`
  is set (Telegram rejects buttons whose `url` isn't absolute).

Failed runs append an `Error` block in a fenced `<pre>` so multiline
stack traces render cleanly on every Telegram client.

Every text segment is HTML-escaped through `html.escape()` before
splicing — Telegram rejects the whole message with HTTP 400 if any
HTML tag fails to parse, so a scenario containing a stray `<` (e.g.
`"Will TVL <$1B by EOY?"`) doesn't silently kill the notification.

Same dedup posture and fire-and-forget guarantees as the other
channels — the runner's two terminal code paths both fire, but the
per-process `(sim_id, status)` set ensures the chat sees exactly one
message per terminal state.

### Test snippet

```bash
# Verify a bot token + chat id work without touching MiroShark
TOKEN="123456789:AAEh…"
CHAT="-100123456789"
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\": \"${CHAT}\", \"text\": \"OK\"}"
# {"ok":true,"result":{…}}
```

## Picking the right channel

* **Discord** — community-facing. Use when the audience wants the
  result *visually*: belief percentages, the share-card thumbnail,
  a tap-through to the simulation. Best for distribution channels
  ("here's what just simulated for you").
* **Slack** — ops-facing. Use when the audience wants the result
  *operationally*: a quick read of the bars, an explicit action
  button, the sim id in monospaced font. Best for engineering /
  research channels.
* **Email** — universal. Use when the audience doesn't live in a
  chat tool (research teams, hedge-fund back-office, analysts) or
  when the operator wants a permanent searchable record that
  doesn't depend on a third-party SaaS retention policy. The one
  channel that works without anyone signing up for anything new.
* **Telegram** — messaging-native. Use when the audience already
  lives on Telegram — most of MiroShark's crypto-launch /
  political-debate / breaking-news audience does. The fastest path
  to push notifications on a phone for a research team that doesn't
  use Slack.
* **Generic webhook** — automation-facing. Use when the result
  needs to land in a workflow tool (Zapier / Make / n8n) that
  unpacks the JSON itself.

## Sandbox note

Pure stdlib (`urllib.request` + `json` + `os` + `html` + `hmac` for
the webhook / Discord / Slack / Telegram channels; `smtplib` +
`email.mime` + `ssl` for email). No new dependencies. The HMAC
signing scheme on the generic webhook (`X-MiroShark-Signature`,
PR #79) applies only to that channel — Discord, Slack, Email, and
Telegram use the platforms' own authentication (URL-as-secret for
Discord / Slack; SMTP auth + STARTTLS for email; bot-token-in-URL
for Telegram) and ignore signature headers.
