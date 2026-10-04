# Setting up Telegram

Telegram is far simpler than a business messaging API: no account review, no message templates,
no 24-hour window, and no public URL is needed unless you want one. Official docs:
<https://core.telegram.org/bots/api>

## 1. Create the bot (2 minutes)

1. In Telegram, open a chat with **@BotFather** and send `/newbot`.
2. Pick a display name and a username ending in `bot`.
3. BotFather replies with a **token** like `123456789:ABC...`. Put it in `.env`:

   ```
   TELEGRAM_BOT_TOKEN=123456789:ABC...
   ```

   Treat the token like a password. If it leaks, send `/revoke` to BotFather for a new one.
4. Optional: `/setdescription`, `/setuserpic`, and `/setcommands` with
   `start - say hello`, `help - what can you do`, `id - show my chat id`.

## 2. Choose how the bot receives messages

| `TELEGRAM_MODE` | When to use it | Needs |
| --- | --- | --- |
| `polling` (default) | Home server, laptop, Raspberry Pi, a VM behind NAT - the bot asks Telegram for new messages. | Nothing: no public URL, no open port |
| `webhook` | You already have a public HTTPS URL and prefer push delivery. | `PUBLIC_BASE_URL` and `TELEGRAM_WEBHOOK_SECRET` (any random string of letters, digits, `_`, `-`) |

In webhook mode the bot registers `https://<PUBLIC_BASE_URL>/telegram/webhook` with Telegram on
startup, and rejects any request that does not carry your secret in the
`X-Telegram-Bot-Api-Secret-Token` header. Polling and webhooks are mutually exclusive on
Telegram's side; the bot deletes a stale webhook when it starts in polling mode.

## 3. Check and start

```bash
python scripts/doctor.py --online
```

then run the bot (Docker or Python - see the main README).

## 4. Create yourself as admin

You need your **chat id** (a number, for example `123456789`). Message **@userinfobot**, or message
your own bot and send `/id` - a chat that is not a user yet is told its id (once an hour per chat).

```bash
python scripts/create_admin.py 123456789 "Your Name"
```

Open your bot in Telegram, press **Start**, and say hello.

## Adding a person

1. They open your bot in Telegram and press **Start** (a bot can only message people who started it).
   The bot answers with their chat id.
2. You add that id: say to the bot (as admin) *"add user: Dad, 123456789"*, use the admin dashboard,
   or run `python scripts/create_admin.py <chat id> "<name>"` for another admin.

Messages from chats that are not users are ignored by design: no model call, no media download.
Only private chats are served; groups and channels are ignored.

### If someone gets no answer

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Nothing at all | Not added as a user, or the bot is not running | Add them; check the bot log (`docker compose logs bot`, or `logs/uvicorn.log` on Windows) |
| Replies work but reminders to them fail | They never pressed Start, or blocked the bot | Ask them to open the bot and press Start; the bot tells you when a delivery fails |
| `getUpdates` conflict in the log | A webhook is registered, or a second copy of the bot is polling | Run one instance only (the bot holds a single-instance lock) |
| Voice message or file "download failed" | Bots can only download files up to 20 MB | Send a smaller file |

## Limits worth knowing

- Telegram caps a message at 4096 characters; longer replies are split at paragraph boundaries.
- Bots may send roughly 30 messages per second overall and 1 per second to the same chat; the bot
  retries on `429` using Telegram's `retry_after`.
- `*bold*` in the bot's messages is sent as Telegram HTML bold.
