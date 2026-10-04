# Troubleshooting

Start with `python scripts/doctor.py --online` — it checks most of
what is below and names the failing item. Logs: `docker compose logs -f bot`, or
`logs/uvicorn.log` on Windows.

## The bot never replies

| Symptom / log line | Cause | Fix |
| --- | --- | --- |
| Nothing in the log at all when you message it | The bot is not receiving updates | Polling mode: check `[telegram] long polling started` appears at startup and the token is right (`doctor.py --online`). Webhook mode: `PUBLIC_BASE_URL` must be https and reachable, and `TELEGRAM_WEBHOOK_SECRET` set |
| `message from unknown chat (...) - ignoring` | You are not a user — unknown chats are ignored on purpose | Send `/id` to the bot to see your chat id, then `python scripts/create_admin.py <chat id> "<name>"` |
| `403` on `POST /telegram/webhook` | The secret header did not match | `TELEGRAM_WEBHOOK_SECRET` differs from the one registered; restart the bot so it re-registers the webhook |
| `getUpdates failed (409)` | A webhook is registered, or a second copy is polling | Run a single instance; polling mode deletes a stale webhook at startup |
| `getUpdates failed (401)` | Wrong or revoked bot token | Copy the token again from @BotFather |
| 502 for ~30s after a restart (webhook mode) | The tunnel is re-establishing | Wait; it is normal |
| Works, then stops after the machine sleeps | The host suspended the process | Disable sleep on the host (Windows: Settings → Power) |
| `another instance already holds the single-instance lock` | A second copy is already running (the bot refuses to start twice on purpose) | Stop the old process (`docker compose down`, or `scripts/stop_assistant.ps1` on Windows) |

## Messages to family/contacts or alerts never arrive

A Telegram bot can only message people who pressed **Start** in the bot, and not people who blocked it.

- Log shows `sendMessage failed (403)` "bot was blocked by the user" or "can't initiate conversation" → the
  recipient has to open the bot and press Start. The bot tells you when a reminder to someone could not be delivered.
- Log shows `sendMessage failed (400)` "chat not found" → the stored chat id is wrong; check it with the person
  (`/id` in the bot shows the right one).
- `429` → Telegram rate limit; the bot waits for `retry_after` and retries automatically.

## Google (Calendar / Gmail / Drive)

- `invalid_grant: Token has been expired or revoked` → the refresh token died. The bot detects this
  daily and sends a reconnect link; open it. If it **keeps happening about weekly**, your OAuth
  consent screen is still in *Testing* — publish it to *Production* (see the
  [README gotcha](./README.md#google-cloud-oauth-setup-gotcha)). This was the single most common
  silent failure in real use.
- Google shows "this app isn't verified" → expected for a personal deployment; Advanced → Go to app.
- Calendar/mail replies say "not connected" → that user hasn't completed the Google link; they say
  *"connect Google"* to get one. In `scripts/chat.py` Google features are always unavailable.
- Redirect URI mismatch → `GOOGLE_REDIRECT_URI` in `.env` must exactly match one registered in Google
  Cloud, including `https://` and the `/oauth/callback` path.

## Proactive updates aren't arriving

Check in this order — each one silently produces "nothing" rather than an error:

1. **Proactive mode is opt-in and off by default.** Message the bot *"turn on proactive mode"* and
   check *"proactive mode status"*.
2. **Google must be connected** for that user (see above) — without it the collectors have no data.
3. **Quiet hours** (default 22:30–07:00), a **"busy until..." status**, or the **daily cap** (default 6)
   are holding messages. Quiet-hours/busy messages are delivered later as a digest; cap-blocked ones
   are dropped by design.
4. The recipient must have pressed Start in the bot (above).
5. The model can decide something isn't worth interrupting you for — that is the point of the feature.

## Admin dashboard

- Always `403` → `ADMIN_ALLOWED_EMAIL` is empty (the dashboard is off by design), you are not on
  `ADMIN_HOST`, or the identity Cloudflare Access reports is not exactly `ADMIN_ALLOWED_EMAIL`.
  With `CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD` set, also check the AUD tag is the one of the Access
  application protecting that hostname, and that the server can reach
  `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`.

## Docker

- `permission denied` on `/data` with a **bind mount** → the container runs as UID 10001. Use the
  named volume from `docker-compose.yml`, or `chown -R 10001 ./data`.
- `docker compose up` complains about a missing `.env` → `cp .env.example .env` first.
- Container shows `unhealthy` → `docker compose logs bot`; usually a missing required variable.

## Other

- Hebrew prints as `?`/crashes in a Windows terminal → use a UTF-8 terminal (`chcp 65001`); the
  bundled scripts already force UTF-8 output.
- Gemini errors / `404` model → the model alias in `src/integrations/gemini.py` changed on Google's
  side; verify the *response body*, not just the HTTP status, after changing it.
- Restored a backup and Google features broke → `TOKEN_ENCRYPTION_KEY` differs from the one that
  encrypted the stored tokens; users must reconnect Google.

Still stuck? Open an issue (redact numbers, tokens and email addresses) or ask in Discussions.
