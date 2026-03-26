
import discord
from discord.ext import commands, tasks
import random
import os
import asyncio
import base64
import string
import time
from termcolor import colored
from discord import Embed
from discord.app_commands import CommandTree
from pystyle import Colorate, Colors, Center
from typing import Optional
import requests
import re

# -------------------------
# ASCII Title (kept for style)
# -------------------------
# Flask import and setup
from flask import Flask
import threading

app = Flask("uptime_check")

@app.route("/")
def home():
    return "OK", 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

# start Flask in a background thread
threading.Thread(target=run_flask).start()


# -------------------------
# STARTUP INPUTS
mode = os.getenv("MODE", "2")  # default to guild mode

bot_runner_user_id = int(os.getenv("BOT_RUNNER_ID", "0"))

bot_token = os.getenv("DISCORD_TOKEN")

server_id = os.getenv("SERVER_ID", "") if mode == "1" else ""
guild_id_for_sync = os.getenv("GUILD_ID_FOR_SYNC", "") if mode == "1" else ""

if not bot_token:
    raise ValueError("DISCORD_TOKEN is not set in environment variables")

if bot_runner_user_id == 0:
    raise ValueError("BOT_RUNNER_ID is not set correctly")

# -------------------------
# BOT SETUP
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
if not hasattr(bot, "tree"):
    tree = CommandTree(bot)
else:
    tree = bot.tree

# -------------------------
# GLOBAL STATE
# -------------------------
command_users = set()           # allowed users for protected commands (IDs)
disabled_commands = set()       # names of disabled commands (strings)
template_link: Optional[str] = None
generated_tokens = {}           # user_id -> {"token": str, "expires_at": float}
reminders = {}                  # reminder_id -> task info (for persistence you'd save to disk)
blocked_for_token = set()       # users who cannot be targeted for token retrieval (bot runner)
blocked_for_token.add(bot_runner_user_id)  # others cannot get runner's token

# default protected commands require either guild perms or being in command_users or being the runner
PROTECTED_COMMANDS = {
    "kick", "ban", "banall", "kickall", "rolecreate", "roledelete", "rolegive", "roleremove",
    "addchannel", "removechannel", "renamechannel", "disablecmd", "enablecmd", "restore",
    "purge", "lock", "unlock", "timeout", "slowmode"
}

# -------------------------
# UTILITIES
# -------------------------
def log(msg: str, color: str = "blue"):
    """Centralized colored logging."""
    prefix = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]"
    print(colored(f"{prefix} {msg}", color))

def is_command_disabled(name: str) -> bool:
    return name in disabled_commands

def has_command_access(interaction: discord.Interaction) -> bool:
    """General guard used for protected commands."""
    # Bot runner always has access
    if interaction.user.id == bot_runner_user_id:
        return True
    # explicit allowlist
    if interaction.user.id in command_users:
        return True
    # if in a guild, check for Manage Guild (higher privilege)
    if interaction.guild:
        member = interaction.guild.get_member(interaction.user.id)
        if member and member.guild_permissions.manage_guild:
            return True
    return False

def ensure_embed(title: str, description: str, color: int = 0x00ff00) -> Embed:
    return Embed(title=title, description=description, color=color)

def parse_message_link(link: str):
    """
    Accepts message link formats and returns (guild_id, channel_id, message_id)
    Example: https://discord.com/channels/<guild_id>/<channel_id>/<message_id>
    """
    match = re.search(r"/channels/(\d+)/(\d+)/(\d+)", link)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None

# -------------------------
# TOKEN GENERATOR (kept, improved)
# -------------------------
def generate_fake_token(user_id: int) -> str:
    # First part: base64 of user id without trailing '=' to look more token-like
    part1 = base64.b64encode(str(user_id).encode()).decode().rstrip("=")
    # second small random
    part2 = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    # third long random
    part3 = "".join(random.choices(string.ascii_letters + string.digits, k=38))
    return f"{part1}.{part2}.{part3}"

# -------------------------
# BACKGROUND: Reminder runner helper
# -------------------------
async def schedule_reminder(target_user_id: int, delay_seconds: int, message: str, reminder_id: str):
    await asyncio.sleep(delay_seconds)
    try:
        user = await bot.fetch_user(int(target_user_id))
        await user.send(f"⏰ Reminder: {message}")
        log(f"Sent reminder {reminder_id} to {target_user_id}", "green")
    except Exception as e:
        log(f"Failed to send reminder {reminder_id}: {e}", "red")
    finally:
        # cleanup from in-memory store
        reminders.pop(reminder_id, None)

# -------------------------
# ON_READY (auto-sync + optional template capture)
# -------------------------
@bot.event
async def on_ready():
    log("Bot ready. Syncing commands...", "green")
    try:
        if guild_id_for_sync:
            await tree.sync(guild=discord.Object(id=int(guild_id_for_sync)))
            log(f"Commands synced to guild {guild_id_for_sync}", "green")
        else:
            await tree.sync()
            log("Global commands synced", "green")
    except Exception as e:
        log(f"Command sync failed: {e}", "red")

    # If server_id provided and mode==1 do initial template capture & simple maintenance steps
    if mode == '1' and server_id:
        try:
            guild = bot.get_guild(int(server_id))
            if guild:
                # Save a server template code (requires Manage Guild permission)
                try:
                    tpl = await guild.create_template(name=f"backup_{guild.id}", description="Saved at startup")
                    global template_link
                    template_link = tpl.code
                    log(f"Saved template link for guild {guild.name}: {template_link}", "blue")
                except Exception as e:
                    log(f"Couldn't create template on guild {server_id}: {e}", "yellow")
        except Exception as e:
            log(f"Startup guild actions failed: {e}", "red")

# -------------------------
# HELPERS FOR PERMISSION CHECKS (decorators-like)
# -------------------------
async def guard_and_disabled_check(interaction: discord.Interaction, command_name: str) -> Optional[Embed]:
    """Return an embed with error if not allowed, otherwise None."""
    if is_command_disabled(command_name):
        return ensure_embed("Command Disabled", "This command has been disabled.", 0xff0000)
    if command_name in PROTECTED_COMMANDS and not has_command_access(interaction):
        return ensure_embed("Permission Denied", "You don't have permission to use this command.", 0xff0000)
    return None

# -------------------------
# COMMANDS
# -------------------------

@bot.tree.command(name="say", description="Make the bot say something")
async def say(interaction: discord.Interaction, message: str):
    try:
        await interaction.response.defer()  # Acknowledge the interaction
        # Delete the original user message (optional in slash commands, since they don't show as a regular message)
        # Send the plain text message
        await interaction.followup.send(message)
    except discord.errors.Forbidden:
        await interaction.followup.send("I don't have permission to send messages here.", ephemeral=True)


# 0) get_token (kept & improved)
@tree.command(name="get_token", description="Fetch token for a user")
async def get_token(interaction: discord.Interaction, user: discord.User):
    name = "get_token"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return

    uid = user.id
    # Prevent others from fetching the bot runner's token
    if uid == bot_runner_user_id and interaction.user.id != bot_runner_user_id:
        embed = ensure_embed("Permission Error", "You cannot fetch a token for this user.", 0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=False)
        log(f"{interaction.user} tried to fetch runner token", "yellow")
        return

    # reuse cached
    stored = generated_tokens.get(uid)
    if stored and time.time() < stored["expires_at"]:
        token = stored["token"]
        embed = ensure_embed("Token Fetched", f"{interaction.user.mention} here is the token of {user.mention}:\n`{token}`", 0x00ff00)
        await interaction.response.send_message(embed=embed, ephemeral=False)
        log(f"Returned cached token for {uid}", "yellow")
        return

    token = generate_fake_token(uid)
    generated_tokens[uid] = {"token": token, "expires_at": time.time() + 15 * 60}
    embed = ensure_embed("Token Generated", f"{interaction.user.mention} here is the token of {user.mention}:\n`{token}`", 0x00ff00)
    await interaction.response.send_message(embed=embed, ephemeral=False)
    log(f"Generated token for {uid}", "blue")


# 1) /userinfo
@tree.command(name="userinfo", description="Show detailed info about a user")
async def userinfo(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    name = "userinfo"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return

    user = user or (interaction.user if isinstance(interaction.user, discord.Member) else None)
    if not user:
        await interaction.response.send_message(embed=ensure_embed("Error", "User not found.", 0xff0000))
        return

    roles = ", ".join(r.mention for r in user.roles[1:]) or "No roles"
    created = user.created_at.strftime("%Y-%m-%d %H:%M:%S")
    joined = user.joined_at.strftime("%Y-%m-%d %H:%M:%S") if getattr(user, "joined_at", None) else "Unknown"

    embed = Embed(title=f"User Info — {user}", color=0x00ff00)
    embed.set_thumbnail(url=user.display_avatar.url if user.display_avatar else None)
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Bot?", value=str(user.bot), inline=True)
    embed.add_field(name="Created", value=created, inline=True)
    embed.add_field(name="Joined", value=joined, inline=True)
    embed.add_field(name="Roles", value=roles, inline=False)
    await interaction.response.send_message(embed=embed)
    log(f"userinfo requested for {user}", "blue")


# 2) /serverinfo
@tree.command(name="serverinfo", description="Show information about the server")
async def serverinfo(interaction: discord.Interaction):
    name = "serverinfo"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message(embed=ensure_embed("Error", "This command must be used in a server.", 0xff0000))
        return

    humans = len([m for m in guild.members if not m.bot])
    bots = len([m for m in guild.members if m.bot])
    channels = len(guild.channels)
    roles_count = len(guild.roles)
    embed = Embed(title=f"Server Info — {guild.name}", color=0x00ff00)
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    embed.add_field(name="ID", value=str(guild.id), inline=True)
    embed.add_field(name="Owner", value=str(guild.owner), inline=True)
    embed.add_field(name="Members (Humans/Bots)", value=f"{humans}/{bots}", inline=True)
    embed.add_field(name="Channels", value=str(channels), inline=True)
    embed.add_field(name="Roles", value=str(roles_count), inline=True)
    embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    await interaction.response.send_message(embed=embed)
    log(f"serverinfo requested for {guild.name}", "blue")


# 3) /roleinfo
@tree.command(name="roleinfo", description="Show information about a role")
async def roleinfo(interaction: discord.Interaction, role: discord.Role):
    name = "roleinfo"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    members = [m for m in interaction.guild.members if role in m.roles]
    perms = [p for p, v in role.permissions if v]
    embed = Embed(title=f"Role Info — {role.name}", color=role.color.value if role.color else 0x00ff00)
    embed.add_field(name="ID", value=str(role.id), inline=True)
    embed.add_field(name="Members", value=str(len(members)), inline=True)
    embed.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
    embed.add_field(name="Permissions", value=", ".join(perms) if perms else "None", inline=False)
    await interaction.response.send_message(embed=embed)
    log(f"roleinfo for {role.name}", "blue")


# 4) /permissions (effective permissions in current channel)
@tree.command(name="permissions", description="Show the effective permissions of a user in this channel")
async def permissions_cmd(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    name = "permissions"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    member = member or interaction.user
    perms = interaction.channel.permissions_for(member)
    granted = [p[0] for p in perms if getattr(perms, p[0])]
    embed = Embed(title=f"Permissions for {member}", description=", ".join(granted) or "None", color=0x00ff00)
    await interaction.response.send_message(embed=embed)
    log(f"permissions checked for {member}", "blue")


# 5) /channelinfo
@tree.command(name="channelinfo", description="Show information about a channel")
async def channelinfo(interaction: discord.Interaction, channel: Optional[discord.abc.GuildChannel] = None):
    name = "channelinfo"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    channel = channel or interaction.channel
    embed = Embed(title=f"Channel Info — {channel.name}", color=0x00ff00)
    embed.add_field(name="ID", value=str(channel.id), inline=True)
    embed.add_field(name="Type", value=str(channel.type), inline=True)
    if isinstance(channel, discord.TextChannel):
        embed.add_field(name="Topic", value=channel.topic or "None", inline=False)
        embed.add_field(name="Slowmode", value=str(channel.slowmode_delay), inline=True)
    await interaction.response.send_message(embed=embed)
    log(f"channelinfo for {channel.name}", "blue")


# 6) /avatar
@tree.command(name="avatar", description="Show user's avatar and banner if any")
async def avatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    name = "avatar"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    user = user or interaction.user
    embed = Embed(title=f"Avatar — {user}", color=0x00ff00)
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    log(f"avatar displayed for {user}", "blue")


# 11) /purge
@tree.command(name="purge", description="Bulk delete messages (up to 100)")
async def purge(interaction: discord.Interaction, amount: int):
    name = "purge"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    if amount < 1 or amount > 100:
        await interaction.response.send_message(embed=ensure_embed("Error", "Amount must be between 1 and 100", 0xff0000))
        return
    if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message(embed=ensure_embed("Permission Denied", "Bot lacks Manage Messages permission.", 0xff0000))
        return
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.response.send_message(embed=ensure_embed("Purge", f"Deleted {len(deleted)} messages.", 0x00ff00), ephemeral=True)
        log(f"Purged {len(deleted)} messages in {interaction.channel}", "blue")
    except Exception as e:
        log(f"Error purging messages: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to purge messages.", 0xff0000))


# 13) /lock (deny send_messages to @everyone)
@tree.command(name="lock", description="Lock the current channel (remove send_messages for @everyone)")
async def lock(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    name = "lock"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    channel = channel or interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(embed=ensure_embed("Error", "This command only works on text channels.", 0xff0000))
        return
    try:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message(embed=ensure_embed("Locked", f"Channel {channel.mention} locked.", 0x00ff00))
        log(f"Locked channel {channel.name}", "blue")
    except Exception as e:
        log(f"Failed to lock channel: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to lock channel.", 0xff0000))


# 14) /unlock
@tree.command(name="unlock", description="Unlock the current channel (restore send_messages for @everyone)")
async def unlock(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    name = "unlock"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    channel = channel or interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(embed=ensure_embed("Error", "This command only works on text channels.", 0xff0000))
        return
    try:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message(embed=ensure_embed("Unlocked", f"Channel {channel.mention} unlocked.", 0x00ff00))
        log(f"Unlocked channel {channel.name}", "blue")
    except Exception as e:
        log(f"Failed to unlock channel: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to unlock channel.", 0xff0000))


# 15) /timeout (mute a member for seconds)
@tree.command(name="timeout", description="Timeout a user (seconds)")
async def timeout(interaction: discord.Interaction, member: discord.Member, seconds: int, reason: Optional[str] = None):
    name = "timeout"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    # permission check
    if not interaction.guild.me.guild_permissions.moderate_members:
        await interaction.response.send_message(embed=ensure_embed("Permission Denied", "Bot needs Moderate Members permission.", 0xff0000))
        return
    if seconds < 1 or seconds > 2419200:  # Discord limit: 28 days
        await interaction.response.send_message(embed=ensure_embed("Error", "Seconds must be between 1 and 2419200 (28 days).", 0xff0000))
        return
    try:
        await member.timeout_for(seconds, reason=reason) if hasattr(member, "timeout_for") else member.edit(timed_out_until=discord.utils.utcnow() + discord.timedelta(seconds=seconds))
        await interaction.response.send_message(embed=ensure_embed("Timed Out", f"Timed out {member.mention} for {seconds} seconds.", 0x00ff00))
        log(f"Timed out {member} for {seconds}s", "blue")
    except Exception as e:
        log(f"Timeout failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to apply timeout.", 0xff0000))


# /slowmode
@tree.command(name="slowmode", description="Set channel slowmode in seconds")
async def slowmode(interaction: discord.Interaction, seconds: int, channel: Optional[discord.TextChannel] = None):
    name = "slowmode"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    channel = channel or interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(embed=ensure_embed("Error", "This command only works on text channels.", 0xff0000))
        return
    if seconds < 0 or seconds > 21600:
        await interaction.response.send_message(embed=ensure_embed("Error", "Slowmode must be between 0 and 21600 seconds.", 0xff0000))
        return
    try:
        await channel.edit(slowmode_delay=seconds)
        await interaction.response.send_message(embed=ensure_embed("Slowmode Set", f"Slowmode of {channel.mention} set to {seconds}s.", 0x00ff00))
        log(f"Set slowmode {seconds}s on {channel.name}", "blue")
    except Exception as e:
        log(f"Slowmode set failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to set slowmode.", 0xff0000))


# 17) /quote (message link)
@tree.command(name="quote", description="Quote a message via link")
async def quote(interaction: discord.Interaction, message_link: str):
    name = "quote"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    parsed = parse_message_link(message_link)
    if not parsed:
        await interaction.response.send_message(embed=ensure_embed("Error", "Invalid message link.", 0xff0000))
        return
    guild_id, channel_id, message_id = parsed
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        msg = await channel.fetch_message(message_id)
        embed = Embed(title=f"Quote from {msg.author}", description=msg.content or "[embed/attachment]", color=0x00ff00)
        embed.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url if msg.author.display_avatar else None)
        embed.add_field(name="Link", value=message_link, inline=False)
        await interaction.response.send_message(embed=embed)
        log(f"Quoted message {message_id}", "blue")
    except Exception as e:
        log(f"Quote failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to fetch the message (missing permissions or invalid link).", 0xff0000))


# 19) /remind
@tree.command(name="remind", description="Set a DM reminder: /remind <time_seconds> <message>")
async def remind(interaction: discord.Interaction, seconds: int, *, message: str):
    name = "remind"
    guard = await guard_and_disabled_check(interaction, name)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    if seconds < 1:
        await interaction.response.send_message(embed=ensure_embed("Error", "Seconds must be a positive integer.", 0xff0000))
        return
    reminder_id = f"r_{int(time.time())}_{random.randint(1000,9999)}"
    # schedule background task
    task = asyncio.create_task(schedule_reminder(interaction.user.id, seconds, message, reminder_id))
    reminders[reminder_id] = {"task": task, "user": interaction.user.id, "when": time.time() + seconds, "message": message}
    await interaction.response.send_message(embed=ensure_embed("Reminder Set", f"I will DM you in {seconds} seconds.", 0x00ff00), ephemeral=True)
    log(f"Set reminder {reminder_id} for {interaction.user}", "blue")


# -------------------------
# FULL ROLE MANAGEMENT (create, delete, give, remove) - improved and safe
# -------------------------
@tree.command(name="create_role", description="Create a role (name,color_hex,mentionable True/False)")
async def create_role(interaction: discord.Interaction, name: str, color_hex: Optional[str] = None, mentionable: Optional[bool] = False):
    cmd = "rolecreate"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        color_value = int(color_hex.replace("#", ""), 16) if color_hex else 0
        role = await interaction.guild.create_role(name=name, colour=discord.Colour(color_value), mentionable=mentionable)
        await interaction.response.send_message(embed=ensure_embed("Role Created", f"Created role {role.name}", 0x00ff00))
        log(f"Created role {role.name}", "blue")
    except Exception as e:
        log(f"Create role failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to create role (check permissions).", 0xff0000))


@tree.command(name="delete_role", description="Delete a role")
async def delete_role(interaction: discord.Interaction, role: discord.Role):
    cmd = "roledelete"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        await role.delete()
        await interaction.response.send_message(embed=ensure_embed("Role Deleted", f"Deleted role {role.name}", 0x00ff00))
        log(f"Deleted role {role.name}", "blue")
    except Exception as e:
        log(f"Delete role failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to delete role (check permissions).", 0xff0000))


@tree.command(name="give_role", description="Give a role to a user")
async def give_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    cmd = "rolegive"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        await member.add_roles(role)
        await interaction.response.send_message(embed=ensure_embed("Role Given", f"Gave {role.name} to {member.display_name}", 0x00ff00))
        log(f"Gave role {role.name} to {member}", "blue")
    except Exception as e:
        log(f"Give role failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to add role (check permissions).", 0xff0000))


@tree.command(name="remove_role", description="Remove a role from a user")
async def remove_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    cmd = "roleremove"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        await member.remove_roles(role)
        await interaction.response.send_message(embed=ensure_embed("Role Removed", f"Removed {role.name} from {member.display_name}", 0x00ff00))
        log(f"Removed role {role.name} from {member}", "blue")
    except Exception as e:
        log(f"Remove role failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to remove role (check permissions).", 0xff0000))

# -------------------------
# FULL CHANNEL MANAGEMENT (create, delete, rename already included but refined)
# -------------------------
@tree.command(name="create_channel", description="Create a text channel")
async def create_channel(interaction: discord.Interaction, name: str, category: Optional[discord.CategoryChannel] = None):
    cmd = "addchannel"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        channel = await interaction.guild.create_text_channel(name=name, category=category)
        await interaction.response.send_message(embed=ensure_embed("Channel Created", f"Created {channel.mention}", 0x00ff00))
        log(f"Created channel {channel.name}", "blue")
    except Exception as e:
        log(f"Create channel failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to create channel (check permissions).", 0xff0000))


@tree.command(name="delete_channel", description="Delete a channel")
async def delete_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    cmd = "removechannel"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        await channel.delete()
        await interaction.response.send_message(embed=ensure_embed("Channel Deleted", f"Deleted {channel.name}", 0x00ff00))
        log(f"Deleted channel {channel.name}", "blue")
    except Exception as e:
        log(f"Delete channel failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to delete channel (check permissions).", 0xff0000))


@tree.command(name="rename_channel", description="Rename the current channel")
async def rename_channel(interaction: discord.Interaction, new_name: str, channel: Optional[discord.abc.GuildChannel] = None):
    cmd = "renamechannel"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    channel = channel or interaction.channel
    try:
        await channel.edit(name=new_name)
        await interaction.response.send_message(embed=ensure_embed("Renamed", f"Renamed to {new_name}", 0x00ff00))
        log(f"Renamed channel to {new_name}", "blue")
    except Exception as e:
        log(f"Rename failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to rename channel (check permissions).", 0xff0000))


# -------------------------
# MODERATION COMMANDS: kick, ban, unban, kickall/banall left but restricted
# -------------------------
@tree.command(name="kick", description="Kick a member")
async def kick_cmd(interaction: discord.Interaction, member: discord.Member, *, reason: Optional[str] = None):
    cmd = "kick"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(embed=ensure_embed("Kicked", f"Kicked {member}", 0x00ff00))
        log(f"Kicked {member}", "blue")
    except Exception as e:
        log(f"Kick failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to kick (missing permissions?).", 0xff0000))


@tree.command(name="ban", description="Ban a member")
async def ban_cmd(interaction: discord.Interaction, member: discord.Member, *, reason: Optional[str] = None):
    cmd = "ban"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(embed=ensure_embed("Banned", f"Banned {member}", 0x00ff00))
        log(f"Banned {member}", "blue")
    except Exception as e:
        log(f"Ban failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to ban (missing permissions?).", 0xff0000))


@tree.command(name="unban", description="Unban a user by ID")
async def unban_cmd(interaction: discord.Interaction, user_id: int):
    cmd = "unban"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    try:
        user = await bot.fetch_user(user_id)
        await interaction.guild.unban(user)
        await interaction.response.send_message(embed=ensure_embed("Unbanned", f"Unbanned {user}", 0x00ff00))
        log(f"Unbanned {user}", "blue")
    except Exception as e:
        log(f"Unban failed: {e}", "red")
        await interaction.response.send_message(embed=ensure_embed("Error", "Failed to unban (ID invalid or missing perms).", 0xff0000))


# -------------------------
# COMMAND DISABLE / ENABLE
# -------------------------
@tree.command(name="disablecmd", description="Disable a command by name")
async def disablecmd_cmd(interaction: discord.Interaction, command_name: str):
    cmd = "disablecmd"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    disabled_commands.add(command_name)
    await interaction.response.send_message(embed=ensure_embed("Disabled", f"Disabled command: {command_name}", 0x00ff00))
    log(f"Disabled command {command_name}", "blue")


@tree.command(name="enablecmd", description="Enable a command by name")
async def enablecmd_cmd(interaction: discord.Interaction, command_name: str):
    cmd = "enablecmd"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    if command_name in disabled_commands:
        disabled_commands.remove(command_name)
        await interaction.response.send_message(embed=ensure_embed("Enabled", f"Enabled command: {command_name}", 0x00ff00))
        log(f"Enabled command {command_name}", "blue")
    else:
        await interaction.response.send_message(embed=ensure_embed("Not Disabled", f"Command {command_name} was not disabled.", 0xff0000))


# -------------------------
# PERMISSION ALLOWLIST MANAGEMENT
# -------------------------
@tree.command(name="addcmdperms", description="Allow a user to use protected commands")
async def addcmdperms_cmd(interaction: discord.Interaction, member: discord.Member):
    cmd = "addcmdperms"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    command_users.add(member.id)
    await interaction.response.send_message(embed=ensure_embed("Added", f"Added {member} to allowed users.", 0x00ff00))
    log(f"Added {member} to command_users", "blue")


@tree.command(name="removecmdperms", description="Remove a user's permission to use protected commands")
async def removecmdperms_cmd(interaction: discord.Interaction, member: discord.Member):
    cmd = "removecmdperms"
    guard = await guard_and_disabled_check(interaction, cmd)
    if guard:
        await interaction.response.send_message(embed=guard)
        return
    if member.id in command_users:
        command_users.remove(member.id)
        await interaction.response.send_message(embed=ensure_embed("Removed", f"Removed {member} from allowed users.", 0x00ff00))
        log(f"Removed {member} from command_users", "blue")
    else:
        await interaction.response.send_message(embed=ensure_embed("Not Found", f"{member} not in allowed users.", 0xff0000))


# -------------------------
# HELP / ACTIVE_DEV kept minimal
# -------------------------
@tree.command(name="active_dev", description="Command to meet the Active Developer badge requirements")
async def active_dev(interaction: discord.Interaction):
    await interaction.response.send_message("This command meets the requirements for the Active Developer badge!")

@tree.command(name="help", description="Show a short help message")
async def help_cmd(interaction: discord.Interaction):
    # produce a compact help list
    msg = (
        "/get_token (user)\n"
        "/userinfo [user]\n"
        "/serverinfo\n"
        "/roleinfo <role>\n"
        "/permissions [user]\n"
        "/channelinfo [channel]\n"
        "/avatar [user]\n"
        "/purge <amount>\n"
        "/lock [channel]\n"
        "/unlock [channel]\n"
        "/timeout <member> <seconds>\n"
        "/slowmode <seconds> [channel]\n"
        "/quote <message_link>\n"
        "/remind <seconds> <message>\n"
        "Admin: /create_role /delete_role /give_role /remove_role /create_channel /delete_channel"
    )
    await interaction.response.send_message(embed=ensure_embed("Help", msg, 0x00ff00), ephemeral=True)

# -------------------------
# RUN BOT
# -------------------------
if __name__ == "__main__":
    try:
        log("Starting bot...", "green")
        bot.run(bot_token)
    except discord.errors.LoginFailure:
        log("Invalid bot token. Please check your token and try again.", "red")
    except Exception as e:
        log(f"An error occurred while running the bot: {e}", "red")

