# Discord Event Bot

Recurring / one-time event registration bot (UK time).

## Slash commands
- `/event_create` – create an event (start, send, close time, repeat every N hours, VC, role, channel, banner)
- `/event_list` – list events and their IDs
- `/event_delete` – delete an event by ID

## Deploy on Render

1. **Discord Developer Portal**: Bot tab -> enable **Server Members Intent**. Invite the bot with scopes
   `bot` + `applications.commands` (permissions: View Channels, Send Messages, Embed Links,
   Mention Everyone, Read Message History).
2. **GitHub**: create a new repository and upload everything in this folder
   (`event_bot.py`, `requirements.txt`, `render.yaml`, `.gitignore`, `README.md`).
   Do NOT upload your token.
3. **Render** -> New -> **Web Service** -> connect the repo. Settings:
   - Runtime: Python
   - Build command: `pip install -r requirements.txt`
   - Start command: `python event_bot.py`
   - Instance type: Free (or Starter if you want saved events, see below)
4. **Environment** tab, add:
   - `DISCORD_TOKEN` = your bot token
   - `GUILD_ID` = your server ID
   - `PYTHON_VERSION` = `3.12.7`
5. Deploy. In the logs you should see `Logged in as ...` and `Web server listening on port ...`.

## Keeping it awake (free plan)
Render free web services go to sleep after ~15 minutes without web traffic, which would turn the bot off.
Create a free monitor at https://uptimerobot.com (type HTTP(s)) that pings your Render URL
(e.g. `https://discord-event-bot.onrender.com/health`) every 5 minutes.

## IMPORTANT: saved events on Render
Render's free plan has an **ephemeral disk**: `events_data.json` is wiped on every restart/redeploy,
so all scheduled events would be lost. To keep them:
- Use a paid instance with a **Persistent Disk** mounted at `/data` and set env var `DATA_DIR=/data`
  (see the commented block in `render.yaml`), or
- Run the bot on your own PC (events are saved next to `event_bot.py`).

## Run locally
```
pip install -r requirements.txt
set DISCORD_TOKEN=your_token      (PowerShell: $env:DISCORD_TOKEN="your_token")
set GUILD_ID=your_server_id
python event_bot.py
```
