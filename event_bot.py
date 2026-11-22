import os
import re
import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from aiohttp import web          # installed automatically with discord.py
from discord import app_commands
from discord.ext import commands, tasks

# ================= CONFIG =================
TOKEN = os.getenv("DISCORD_TOKEN", "PUT_TOKEN_HERE")           # set as env var on Render
GUILD_ID = int(os.getenv("GUILD_ID", "123456789012345678"))    # your server ID

GUILD = discord.Object(id=GUILD_ID)
UK_TZ = ZoneInfo("Europe/London")                      # needs: pip install tzdata (Windows)
UTC = timezone.utc

GRACE = timedelta(minutes=15)    # how long after start the message keeps updating
CHECK_SECONDS = 15               # how often the bot checks schedules

BASE_DIR = Path(__file__).resolve().parent
# On Render with a persistent disk, set DATA_DIR=/data so events survive restarts
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "events_data.json"   # all events are saved here
BANNER_DIR = DATA_DIR / "banners"           # banner images are saved here
BANNER_DIR.mkdir(exist_ok=True)

TIME_HELP = (
    "❌ Invalid time. Use `YYYY-MM-DD HH:MM` (24h, UK time), e.g. `2026-11-25 19:40`.\n"
    "For `send_time` and `close_time` you can also write just `HH:MM` (e.g. `19:10`) "
    "and it will use the start date."
)

# ================= BOT SETUP =================
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ================= STORAGE (saved to JSON) =================
def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("next_id", 1)
            d.setdefault("schedules", {})
            return d
        except (json.JSONDecodeError, OSError):
            print("⚠️ events_data.json was unreadable, backing it up and starting fresh.")
            try:
                os.replace(DATA_FILE, DATA_FILE.with_suffix(".bak"))
            except OSError:
                pass
    return {"next_id": 1, "schedules": {}}


def save_data():
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


data = load_data()
schedules: dict = data["schedules"]   # {"1": {...}, "2": {...}}
vc_cache: dict = {}                   # in-memory: last seen voice members per schedule


# ================= TIME UTILS (everything stored in UTC) =================
def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def uk_str(dt: datetime) -> str:
    return dt.astimezone(UK_TZ).strftime("%a %d %b, %H:%M %Z")


TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}$")


def parse_uk(text: str, ref: datetime | None = None) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' or 'HH:MM' as UK time, return aware UTC datetime."""
    text = text.strip()
    if TIME_ONLY.match(text):
        h, m = map(int, text.split(":"))
        if h > 23 or m > 59:
            raise ValueError("bad time")
        base = (ref or now_utc()).astimezone(UK_TZ)
        return base.replace(hour=h, minute=m, second=0, microsecond=0).astimezone(UTC)
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UK_TZ).astimezone(UTC)


def add_interval(dt: datetime, hours: int) -> datetime:
    """Daily (24h multiples) keeps the same UK clock time across GMT/BST changes.
    Other intervals (1h, 6h...) are exact elapsed hours."""
    if hours % 24 == 0:
        local = dt.astimezone(UK_TZ)
        return (local + timedelta(hours=hours)).astimezone(UTC)
    return dt + timedelta(hours=hours)


# ================= SCHEDULE HELPERS =================
def s_start(s) -> datetime:
    return from_iso(s["next_start"])


def post_at(s, start):
    return start - timedelta(seconds=s["send_offset"])


def close_at(s, start):
    return start - timedelta(seconds=s["close_offset"])


def end_at(s, start):
    end = start + GRACE
    if s["repeat_hours"]:
        next_post = add_interval(start, s["repeat_hours"]) - timedelta(seconds=s["send_offset"])
        end = min(end, next_post)
    return end


def roll_forward(s, now, force_step=False):
    """Move next_start forward to the first occurrence that hasn't fully ended."""
    start = s_start(s)
    if force_step:
        start = add_interval(start, s["repeat_hours"])
    while end_at(s, start) <= now:
        start = add_interval(start, s["repeat_hours"])
    s["next_start"] = to_iso(start)


def remove_schedule(sid):
    s = schedules.pop(str(sid), None)
    vc_cache.pop(int(sid), None)
    if s and s.get("banner"):
        try:
            (BANNER_DIR / s["banner"]).unlink(missing_ok=True)
        except OSError:
            pass
    save_data()


def advance(s, now):
    """Occurrence finished: repeat -> prepare the next one, once -> delete schedule."""
    if not s["repeat_hours"]:
        remove_schedule(s["id"])
        return
    roll_forward(s, now, force_step=True)
    s.update(state="waiting", message_id=None, participants=[], subs=[])
    save_data()


def find_by_message(message_id: int):
    for s in schedules.values():
        if s.get("message_id") == message_id:
            return s
    return None


# ================= EMBED =================
def format_roster(s, vc_members=None) -> str:
    if not s["participants"]:
        return "—"
    lines = []
    for i, uid in enumerate(s["participants"]):
        status = ""
        if vc_members is not None:
            status = " ✅" if uid in vc_members else " ❌"
        lines.append(f"{i + 1}. <@{uid}>{status}")
    return "\n".join(lines)


def format_subs(s) -> str:
    if not s["subs"]:
        return "──────"
    return "\n".join(f"{i + 1}. <@{uid}>" for i, uid in enumerate(s["subs"]))


def build_embed(s, vc_members=None) -> discord.Embed:
    is_open = s["state"] == "open"
    start = s_start(s)

    if is_open:
        title = f"🚀 {s['name']} - OPEN 🟢"
        status = "🟢 **Registration is open!**"
    else:
        title = f"🚀 {s['name']} - CLOSED ✅"
        status = "🔴 **Registration is closed!**"

    desc = (
        f"{status}\n\n"
        f"🔊 **Voice Channel:** <#{s['vc_id']}>\n\n"
        f"**Participants:** {len(s['participants'])}/{s['limit']}\n\n"
        f"**Main Roster:**\n{format_roster(s, vc_members)}\n\n\n"
        f"**Subs List:**\n{format_subs(s)}\n\n"
    )

    if is_open:
        desc += (
            f"🕒 **Starts:** {uk_str(start)} (<t:{int(start.timestamp())}:R>)\n"
            f"🔒 **Registration closes:** <t:{int(close_at(s, start).timestamp())}:R>"
        )
    else:
        desc += "**Registration has ended have fun 🎉**"

    embed = discord.Embed(
        title=title,
        description=desc,
        color=0x2ECC71 if is_open else 0xE74C3C,
        timestamp=start,
    )

    guild = bot.get_guild(s["guild_id"])
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    elif bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    if s.get("banner") and (BANNER_DIR / s["banner"]).exists():
        embed.set_image(url=f"attachment://{s['banner']}")

    return embed


# ================= BUTTONS (persistent – work after restart) =================
class EventView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="✅", custom_id="event_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = find_by_message(interaction.message.id)
        if not s or s["state"] != "open":
            return await interaction.response.send_message("Registration is closed.", ephemeral=True)

        uid = interaction.user.id
        if uid in s["participants"] or uid in s["subs"]:
            return await interaction.response.send_message("You are already registered.", ephemeral=True)

        if len(s["participants"]) < s["limit"]:
            s["participants"].append(uid)
        else:
            s["subs"].append(uid)

        save_data()
        await interaction.response.edit_message(embed=build_embed(s))

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger, emoji="❌", custom_id="event_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = find_by_message(interaction.message.id)
        if not s or s["state"] != "open":
            return await interaction.response.send_message("Registration is closed.", ephemeral=True)

        uid = interaction.user.id
        if uid in s["participants"]:
            s["participants"].remove(uid)
            if s["subs"]:
                s["participants"].append(s["subs"].pop(0))   # promote first sub
        elif uid in s["subs"]:
            s["subs"].remove(uid)
        else:
            return await interaction.response.send_message("You are not registered.", ephemeral=True)

        save_data()
        await interaction.response.edit_message(embed=build_embed(s))


# ================= COMMANDS =================
@bot.tree.command(
    name="event_create",
    description="Create a one-time or repeating event (all times are UK time)",
    guild=GUILD,
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    name="Event name (e.g. Informal Event)",
    start_time="When the event starts: YYYY-MM-DD HH:MM (UK, 24h) or just HH:MM",
    send_time="When to post the message: YYYY-MM-DD HH:MM or HH:MM (UK)",
    close_time="When registration closes: YYYY-MM-DD HH:MM or HH:MM (UK)",
    limit="Main roster size (extra people go to Subs)",
    channel="Channel to post the event in",
    voice_channel="Voice channel to check attendance in",
    role_to_ping="Role to ping when the event is posted",
    repeat_every_hours="0 = once, 1 = every hour, 6 = every 6 hours, 24 = daily ...",
    banner="Optional banner image/GIF shown at the bottom",
)
async def event_create(
    interaction: discord.Interaction,
    name: str,
    start_time: str,
    send_time: str,
    close_time: str,
    limit: app_commands.Range[int, 1, 100],
    channel: discord.TextChannel,
    voice_channel: discord.VoiceChannel,
    role_to_ping: discord.Role,
    repeat_every_hours: app_commands.Range[int, 0, 720],
    banner: discord.Attachment | None = None,
):
    await interaction.response.defer(ephemeral=True)
    now = now_utc()
    rep = repeat_every_hours

    # ---- parse times ----
    try:
        start = parse_uk(start_time)
        # "HH:MM" for a one-time event that already passed today -> tomorrow
        if TIME_ONLY.match(start_time.strip()) and start <= now and not rep:
            start = add_interval(start, 24)

        send_dt = parse_uk(send_time, ref=start)
        close_dt = parse_uk(close_time, ref=start)
        # "HH:MM" values that land after the start belong to the previous day
        if TIME_ONLY.match(send_time.strip()) and send_dt > start:
            send_dt = add_interval(send_dt, -24)
        if TIME_ONLY.match(close_time.strip()) and close_dt > start:
            close_dt = add_interval(close_dt, -24)
    except ValueError:
        return await interaction.followup.send(TIME_HELP, ephemeral=True)

    # ---- validate ----
    if not (send_dt <= close_dt <= start):
        return await interaction.followup.send(
            "❌ Order must be: **send_time ≤ close_time ≤ start_time**.", ephemeral=True
        )
    if not rep and start <= now:
        return await interaction.followup.send("❌ The start time is in the past.", ephemeral=True)

    send_off = int((start - send_dt).total_seconds())
    close_off = int((start - close_dt).total_seconds())
    if rep and send_off >= rep * 3600:
        return await interaction.followup.send(
            f"❌ The message is posted {send_off // 60} min before the start, which is longer than "
            f"the repeat interval ({rep}h). Use a later send_time.",
            ephemeral=True,
        )

    # ---- banner (saved locally so it never expires) ----
    sid = data["next_id"]
    banner_name = None
    if banner:
        if not (banner.content_type or "").startswith("image/"):
            return await interaction.followup.send("❌ The banner must be an image or GIF.", ephemeral=True)
        ext = Path(banner.filename).suffix.lower()
        if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            ext = ".png"
        banner_name = f"banner_{sid}{ext}"
        (BANNER_DIR / banner_name).write_bytes(await banner.read())

    s = {
        "id": sid,
        "guild_id": interaction.guild_id,
        "name": name,
        "channel_id": channel.id,
        "vc_id": voice_channel.id,
        "role_id": role_to_ping.id,
        "limit": limit,
        "repeat_hours": rep,
        "send_offset": send_off,
        "close_offset": close_off,
        "next_start": to_iso(start),
        "banner": banner_name,
        "state": "waiting",       # waiting -> open -> closed -> (next occurrence / deleted)
        "message_id": None,
        "participants": [],
        "subs": [],
    }
    if rep:
        roll_forward(s, now)

    data["next_id"] = sid + 1
    schedules[str(sid)] = s
    save_data()

    st = s_start(s)
    repeat_text = "once" if not rep else f"every {rep} hour(s)"
    note = ""
    if post_at(s, st) <= now:
        note = "\n⚠️ The send time has already passed, so it will be posted within seconds."
    await interaction.followup.send(
        f"✅ **Event #{sid} scheduled** — {name}\n"
        f"• Repeats: **{repeat_text}**\n"
        f"• Next start: **{uk_str(st)}**\n"
        f"• Message posts: **{uk_str(post_at(s, st))}** in {channel.mention}\n"
        f"• Registration closes: **{uk_str(close_at(s, st))}**\n"
        f"• Voice channel: {voice_channel.mention}"
        f"{note}",
        ephemeral=True,
    )


@bot.tree.command(name="event_list", description="Show all scheduled events", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
async def event_list(interaction: discord.Interaction):
    if not schedules:
        return await interaction.response.send_message("No events scheduled.", ephemeral=True)

    lines = []
    for s in sorted(schedules.values(), key=lambda x: x["id"]):
        rep = "once" if not s["repeat_hours"] else f"every {s['repeat_hours']}h"
        lines.append(
            f"**#{s['id']} — {s['name']}** ({rep})\n"
            f"Next start: {uk_str(s_start(s))} • State: `{s['state']}`\n"
            f"<#{s['channel_id']}> • 🔊 <#{s['vc_id']}> • Limit: {s['limit']}"
        )
    embed = discord.Embed(title="📅 Scheduled events", description="\n\n".join(lines), color=0x3498DB)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="event_delete", description="Delete a scheduled event by its ID", guild=GUILD)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(event_id="The # number from /event_list")
async def event_delete(interaction: discord.Interaction, event_id: int):
    s = schedules.get(str(event_id))
    if not s:
        return await interaction.response.send_message("❌ No event with that ID.", ephemeral=True)

    if s.get("message_id"):
        ch = bot.get_channel(s["channel_id"])
        if ch:
            try:
                await ch.get_partial_message(s["message_id"]).delete()
            except discord.HTTPException:
                pass

    remove_schedule(event_id)
    await interaction.response.send_message(f"🗑️ Event #{event_id} deleted.", ephemeral=True)


# ================= EVENT ENGINE =================
def get_vc_members(s) -> set:
    vc = bot.get_channel(s["vc_id"])
    return {m.id for m in vc.members} if vc else set()


async def post_event(s) -> bool:
    channel = bot.get_channel(s["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(s["channel_id"])
        except discord.HTTPException as e:
            print(f"Event #{s['id']}: cannot find channel: {e}")
            return False

    role = channel.guild.get_role(s["role_id"])
    s["participants"], s["subs"] = [], []
    s["state"] = "open"

    file = None
    if s.get("banner"):
        path = BANNER_DIR / s["banner"]
        if path.exists():
            file = discord.File(path, filename=s["banner"])

    try:
        msg = await channel.send(
            content=role.mention if role else None,
            embed=build_embed(s),
            view=EventView(),
            file=file,
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.HTTPException as e:
        print(f"Event #{s['id']}: failed to post: {e}")
        s["state"] = "waiting"
        return False

    s["message_id"] = msg.id
    save_data()
    return True


async def edit_event_message(s, vc_members=None) -> bool:
    """Returns False only if the message was deleted."""
    if not s.get("message_id"):
        return False
    channel = bot.get_channel(s["channel_id"])
    if channel is None:
        return True
    try:
        await channel.get_partial_message(s["message_id"]).edit(
            embed=build_embed(s, vc_members),
            view=discord.utils.MISSING if s["state"] == "open" else None,   # None removes buttons
        )
    except discord.NotFound:
        return False
    except discord.HTTPException as e:
        print(f"Event #{s['id']}: failed to edit message: {e}")
    return True


async def process_schedule(s, now):
    sid = s["id"]
    start = s_start(s)
    p_at, c_at, e_at = post_at(s, start), close_at(s, start), end_at(s, start)

    # 1) waiting -> post
    if s["state"] == "waiting":
        if now >= e_at:                 # occurrence was missed completely (bot was off)
            advance(s, now)
            return
        if now < p_at:
            return
        if not await post_event(s):
            return

    finishing = s["state"] in ("open", "closed") and now >= e_at

    # 2) open -> closed
    if s["state"] == "open" and (now >= c_at or finishing):
        s["state"] = "closed"
        vc_cache.pop(sid, None)
        save_data()

    # 3) closed -> keep ✅/❌ voice status updated
    if s["state"] == "closed":
        members = get_vc_members(s)
        if finishing or vc_cache.get(sid) != members:
            vc_cache[sid] = members
            if not await edit_event_message(s, members):
                finishing = True        # message was deleted by someone

    # 4) finished -> next occurrence (or delete if one-time)
    if finishing:
        vc_cache.pop(sid, None)
        advance(s, now)


@tasks.loop(seconds=CHECK_SECONDS)
async def event_watcher():
    now = now_utc()
    for s in list(schedules.values()):
        try:
            await process_schedule(s, now)
        except Exception:
            print(f"Error while processing event #{s.get('id')}:")
            traceback.print_exc()


@event_watcher.before_loop
async def before_watcher():
    await bot.wait_until_ready()


# ================= WEB SERVER (for Render) =================
async def start_web_server():
    """Render Web Services must listen on $PORT. UptimeRobot can ping this to keep the bot awake."""
    port = int(os.getenv("PORT", "0"))
    if not port:
        return   # running locally, no web server needed

    async def ok(request):
        return web.Response(text="Event bot is running ✅")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"🌐 Web server listening on port {port}")


# ================= LIFECYCLE =================
@bot.event
async def setup_hook():
    await start_web_server()
    bot.add_view(EventView())                 # makes old buttons work after a restart

    bot.tree.clear_commands(guild=None)       # remove stale global commands
    await bot.tree.sync()
    await bot.tree.sync(guild=GUILD)          # register commands for your server

    event_watcher.start()


@bot.event
async def on_ready():
    print(f"🤖 Logged in as {bot.user}")
    print(f"📂 Loaded {len(schedules)} saved event(s) from {DATA_FILE.name}")


if TOKEN == "PUT_TOKEN_HERE":
    raise SystemExit("Set your bot token first (DISCORD_TOKEN env var or TOKEN in the file).")

bot.run(TOKEN)
