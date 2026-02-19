import os
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks

# ====== EINSTELLUNGEN ======
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WARN_ROLE_NAME = "Warnliste"
DEFAULT_DAYS = 3
DB_FILE = "warnlist.db"
CHECK_EVERY_MINUTES = 5

DM_ON_ADD = True                 # DM an User beim Hinzufügen (optional)
DM_ON_MANUAL_REMOVE = True       # DM an User nur bei /warndel
DM_USER_ON_EXPIRE = False        # <- wie gewünscht: KEINE DM bei Ablauf
# ===========================


def utcnow():
    return datetime.now(timezone.utc)


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        # warnings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                expires_at TEXT    NOT NULL,
                reason     TEXT    NOT NULL,
                added_by   INTEGER,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(warnings)").fetchall()]
        if "added_by" not in cols:
            conn.execute("ALTER TABLE warnings ADD COLUMN added_by INTEGER")

        # settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER
            )
        """)
        conn.commit()


def upsert_warning(guild_id: int, user_id: int, expires_at: datetime, reason: str, added_by: int | None):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO warnings (guild_id, user_id, expires_at, reason, added_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET
                expires_at=excluded.expires_at,
                reason=excluded.reason,
                added_by=excluded.added_by
        """, (guild_id, user_id, expires_at.isoformat(), reason, added_by))
        conn.commit()


def remove_entry(guild_id: int, user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        conn.commit()


def get_guild_entries(guild_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute("""
            SELECT user_id, expires_at, reason, added_by
            FROM warnings
            WHERE guild_id=?
        """, (guild_id,)).fetchall()


def get_expired_entries():
    now = utcnow().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute("""
            SELECT guild_id, user_id, expires_at, reason, added_by
            FROM warnings
            WHERE expires_at <= ?
        """, (now,)).fetchall()


def set_log_channel(guild_id: int, channel_id: int | None):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO settings (guild_id, log_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id
        """, (guild_id, channel_id))
        conn.commit()


def get_log_channel_id(guild_id: int) -> int | None:
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute("SELECT log_channel_id FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
    return row[0] if row else None


async def safe_dm(user: discord.abc.User, text: str) -> bool:
    try:
        await user.send(text)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


class WarnBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Slash Commands syncen
        await self.tree.sync()


client = WarnBot()


async def get_warn_role(guild: discord.Guild) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=WARN_ROLE_NAME)


async def post_log(guild: discord.Guild, text: str | None = None, embed: discord.Embed | None = None):
    channel_id = get_log_channel_id(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(content=text, embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        return



@client.event
async def on_ready():
    init_db()
    if not expiry_checker.is_running():
        expiry_checker.start()
    print(f"Eingeloggt als {client.user}")


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

@client.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    now = utcnow()

    embed = discord.Embed(
        title="Mitglied beigetreten",
        color=discord.Color.green(),
        timestamp=now
    )
    embed.add_field(name="User", value=f"{member.mention}\n`{member.name}`", inline=False)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Zeit", value=f"**{fmt_local(now)}**", inline=True)

    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    await post_log(guild, embed=embed)


@client.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    now = utcnow()

    embed = discord.Embed(
        title="Mitglied verlassen",
        color=discord.Color.red(),
        timestamp=now
    )
    embed.add_field(name="Name", value=f"**{member.name}#{member.discriminator}**", inline=False)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Zeit", value=f"**{fmt_local(now)}**", inline=True)

    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    await post_log(guild, embed=embed)


# -------- Slash Commands --------

@client.tree.command(name="setlog", description="Setzt den Log-Kanal für Warnlisten-Events (Ablauf etc.).")
@admin_only()
@app_commands.describe(channel="Kanal für Logs (leer lassen zum Deaktivieren)")
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Nur auf einem Server möglich.", ephemeral=True)

    if channel is None:
        set_log_channel(guild.id, None)
        return await interaction.response.send_message("✅ Log-Kanal deaktiviert.", ephemeral=True)

    set_log_channel(guild.id, channel.id)
    await interaction.response.send_message(f"✅ Log-Kanal gesetzt: {channel.mention}", ephemeral=True)


@client.tree.command(name="warnadd", description="Setzt einen User auf die Warnliste (Rolle + Grund + Frist 1–7 Tage).")
@admin_only()
@app_commands.describe(
    user="Der betroffene User",
    reason="Grund für die Warnliste",
    days="Frist in Tagen (1–7), Standard 3"
)
async def warnadd(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: str,
    days: app_commands.Range[int, 1, 7] = DEFAULT_DAYS
):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Nur auf einem Server möglich.", ephemeral=True)

    role = await get_warn_role(guild)
    if role is None:
        return await interaction.response.send_message(
            f"Rolle **{WARN_ROLE_NAME}** nicht gefunden. Bitte anlegen oder Namen im Bot anpassen.",
            ephemeral=True
        )

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return await interaction.response.send_message("Mir fehlt **Rollen verwalten**.", ephemeral=True)

    if me.top_role <= role:
        return await interaction.response.send_message(
            "Ich kann die Rolle nicht verwalten. Bitte setz meine Bot-Rolle **über** die Warnliste-Rolle.",
            ephemeral=True
        )

    reason = (reason or "").strip() or "Kein Grund angegeben"
    expires_at = utcnow() + timedelta(days=int(days))

    try:
        if role not in user.roles:
            await user.add_roles(role, reason="Warnliste: /warnadd")
        upsert_warning(guild.id, user.id, expires_at, reason, added_by=interaction.user.id)

        if DM_ON_ADD:
            await safe_dm(
                user,
                f"⚠️ Du bist auf der **Warnliste** in **{guild.name}**.\n"
                f"**Grund:** {reason}\n"
                f"**Frist:** bis {fmt_local(expires_at)}"
            )

        # Log (optional)
        await post_log(
            guild,
            f"🟧 **Warnliste gesetzt**: {user.mention} — bis **{fmt_local(expires_at)}**\nGrund: *{reason}*"
        )

        await interaction.response.send_message(
            f"✅ {user.mention} ist jetzt auf der Warnliste.\n"
            f"**Frist:** {days} Tage (bis **{fmt_local(expires_at)}**)\n"
            f"**Grund:** {reason}",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ Keine Rechte, Rolle zu setzen (Rollenrang/Berechtigungen prüfen).",
            ephemeral=True
        )


@client.tree.command(name="warndel", description="Entfernt einen User von der Warnliste (Rolle + DB).")
@admin_only()
@app_commands.describe(user="Der betroffene User")
async def warndel(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Nur auf einem Server möglich.", ephemeral=True)

    role = await get_warn_role(guild)
    if role is None:
        return await interaction.response.send_message(f"Rolle **{WARN_ROLE_NAME}** nicht gefunden.", ephemeral=True)

    try:
        if role in user.roles:
            await user.remove_roles(role, reason="Warnliste: /warndel")
        remove_entry(guild.id, user.id)

        # ✅ Nur hier DM an User (wie gewünscht)
        if DM_ON_MANUAL_REMOVE:
            await safe_dm(user, f"✅ Du bist **nicht mehr** auf der Warnliste in **{guild.name}**.")

        # Log (optional)
        await post_log(guild, f"🟩 **Warnliste manuell entfernt**: {user.mention}")

        await interaction.response.send_message(f"✅ {user.mention} entfernt.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ Keine Rechte, Rolle zu entfernen (Rollenrang/Berechtigungen prüfen).",
            ephemeral=True
        )


@client.tree.command(name="warnstatus", description="Zeigt die aktuelle Warnliste (nur für Admin sichtbar).")
@admin_only()
async def warnstatus(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Nur auf einem Server möglich.", ephemeral=True)

    entries = get_guild_entries(guild.id)
    if not entries:
        return await interaction.response.send_message("Aktuell ist niemand auf der Warnliste.", ephemeral=True)

    lines = []
    for user_id, expires_at, reason, _added_by in entries:
        member = guild.get_member(user_id)
        name = member.mention if member else f"`{user_id}`"
        exp_dt = datetime.fromisoformat(expires_at)
        lines.append(f"- {name} bis **{fmt_local(exp_dt)}** — *{reason}*")

    text = "📌 **Warnliste:**\n" + "\n".join(lines[:25])
    if len(lines) > 25:
        text += f"\n… und **{len(lines)-25}** weitere."

    await interaction.response.send_message(text, ephemeral=True)


# -------- Expiry Checker --------

@tasks.loop(minutes=CHECK_EVERY_MINUTES)
async def expiry_checker():
    expired = get_expired_entries()
    if not expired:
        return

    for guild_id, user_id, expires_at, reason, _added_by in expired:
        guild = client.get_guild(guild_id)
        if guild is None:
            remove_entry(guild_id, user_id)
            continue

        role = await get_warn_role(guild)
        if role is None:
            remove_entry(guild_id, user_id)
            continue

        member = guild.get_member(user_id)

        # Log VOR Entfernen (optional)
        exp_dt = datetime.fromisoformat(expires_at)
        await post_log(
            guild,
            f"⏳ **Warnliste abgelaufen**: {(member.mention if member else f'User-ID {user_id}')}\n"
            f"War bis **{fmt_local(exp_dt)}** — Grund: *{reason}*"
        )

        # Rolle entfernen
        if member and role in member.roles:
            try:
                await member.remove_roles(role, reason="Warnliste abgelaufen (Auto-Remove)")
            except discord.Forbidden:
                pass

            # ❌ KEINE DM an User bei Ablauf (wie gewünscht)
            if DM_USER_ON_EXPIRE:
                await safe_dm(
                    member,
                    f"⏳ Deine Warnlisten-Frist in **{guild.name}** ist abgelaufen."
                )

        remove_entry(guild_id, user_id)


if __name__ == "__main__":
    if TOKEN == "HIER_DEIN_TOKEN" or not TOKEN:
        raise SystemExit("Bitte TOKEN setzen (DISCORD_BOT_TOKEN oder im Code eintragen).")
    client.run(TOKEN)

