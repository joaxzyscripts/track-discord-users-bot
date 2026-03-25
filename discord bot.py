import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timedelta
import json
import os

TOKEN = 'TOKEN'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, 'tracked_users.json')
LEGACY_DATA_FILE = os.path.abspath('tracked_users.json')
HISTORY_RETENTION_DAYS = 30
AUTHORIZED_USER_ID = IDHERE
STATUS_LABELS = {
    "online": "Online",
    "offline": "Offline",
    "idle": "Ausente",
    "dnd": "Nao perturbe",
    "invisible": "Invisivel"
}

class MyClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.data = self.load_data()
        self.save_data()

    def default_data(self):
        return {
            "users": {},
            "quiet_until": None,
            "panel_channel_id": None,
            "panel_message_id": None
        }

    def normalize_data(self, raw_data):
        data = self.default_data()
        if not isinstance(raw_data, dict):
            return data

        users_block = raw_data.get("users")
        if not isinstance(users_block, dict):
            users_block = raw_data

        normalized_users = {}
        for uid, info in users_block.items():
            if not isinstance(info, dict):
                continue

            user_info = dict(info)
            destination_id = user_info.pop("destination_id", None)
            destination_type = user_info.pop("type", None)

            if "dm_id" not in user_info:
                user_info["dm_id"] = destination_id if destination_type == "dm" else None
            if "channel_id" not in user_info:
                user_info["channel_id"] = destination_id if destination_type == "channel" else None

            user_info.setdefault("last_status", "offline")
            user_info.setdefault("last_change", datetime.now().isoformat())
            user_info.setdefault("last_app", "Nada")
            user_info.setdefault("last_game_played", user_info.get("last_app", "Nada"))
            self.ensure_user_stats(user_info)
            normalized_users[str(uid)] = user_info

        data["users"] = normalized_users
        data["quiet_until"] = raw_data.get("quiet_until")
        data["panel_channel_id"] = raw_data.get("panel_channel_id")
        data["panel_message_id"] = raw_data.get("panel_message_id")
        return data

    def resolve_data_file(self):
        candidates = []
        for path in {DATA_FILE, LEGACY_DATA_FILE}:
            if path and os.path.exists(path):
                candidates.append(path)

        if not candidates:
            return DATA_FILE

        return max(candidates, key=os.path.getmtime)

    def load_data(self):
        source_file = self.resolve_data_file()
        if os.path.exists(source_file):
            with open(source_file, 'r', encoding='utf-8') as f:
                return self.normalize_data(json.load(f))
        return self.default_data()

    def save_data(self):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=4)

    def parse_timestamp(self, value):
        if not value:
            return None

        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    def status_label(self, status):
        if status is None:
            return "Desconhecido"

        status = str(status)
        return STATUS_LABELS.get(status, status.replace("_", " ").title())

    def format_duration(self, total_seconds):
        total_seconds = max(0, int(total_seconds))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if not parts:
            parts.append(f"{seconds}s")

        return " ".join(parts[:3])

    def find_member(self, user_id):
        for guild in self.guilds:
            member = guild.get_member(user_id)
            if member:
                return member
        return None

    def prune_user_history(self, info, now=None):
        now = now or datetime.now()
        cutoff = now - timedelta(days=HISTORY_RETENTION_DAYS)

        status_history = []
        for entry in info.get("status_history", []):
            if not isinstance(entry, dict):
                continue

            started_at = self.parse_timestamp(entry.get("started_at"))
            if not started_at:
                continue

            ended_at = self.parse_timestamp(entry.get("ended_at")) if entry.get("ended_at") else None
            if ended_at and ended_at < cutoff:
                continue

            status_history.append({
                "status": str(entry.get("status", "offline")),
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat() if ended_at else None
            })

        if not status_history:
            fallback_start = info.get("last_change") or now.isoformat()
            status_history = [{
                "status": info.get("last_status", "offline"),
                "started_at": fallback_start,
                "ended_at": None
            }]

        info["status_history"] = status_history[-500:]

        status_changes = []
        for entry in info.get("status_changes", []):
            if not isinstance(entry, dict):
                continue

            changed_at = self.parse_timestamp(entry.get("changed_at"))
            if not changed_at:
                continue

            status_changes.append({
                "from": entry.get("from"),
                "to": str(entry.get("to", "offline")),
                "changed_at": changed_at.isoformat()
            })

        if not status_changes:
            status_changes = [{
                "from": None,
                "to": info.get("last_status", "offline"),
                "changed_at": info.get("last_change") or now.isoformat()
            }]

        info["status_changes"] = status_changes[-50:]

        app_history = []
        for entry in info.get("app_history", []):
            if not isinstance(entry, dict):
                continue

            app = entry.get("app")
            started_at = self.parse_timestamp(entry.get("started_at"))
            if not app or not started_at:
                continue

            ended_at = self.parse_timestamp(entry.get("ended_at")) if entry.get("ended_at") else None
            if ended_at and ended_at < cutoff:
                continue

            app_history.append({
                "app": str(app),
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat() if ended_at else None
            })

        info["app_history"] = app_history[-500:]

    def ensure_user_stats(self, info):
        now_iso = datetime.now().isoformat()
        last_status = info.get("last_status", "offline")
        last_change = info.get("last_change") or now_iso
        last_app = info.get("last_app", "Nada")
        tracked_since = info.get("tracked_since") or last_change
        last_app_change = info.get("last_app_change") or tracked_since

        info["tracked_since"] = tracked_since
        info["last_app_change"] = last_app_change
        info.setdefault("last_seen_at", last_change if last_status == "offline" else tracked_since)

        status_history = info.get("status_history")
        if not isinstance(status_history, list) or not status_history:
            info["status_history"] = [{
                "status": last_status,
                "started_at": last_change,
                "ended_at": None
            }]
        else:
            current_segment = status_history[-1]
            if (
                not isinstance(current_segment, dict)
                or current_segment.get("status") != last_status
                or current_segment.get("ended_at") is not None
            ):
                status_history.append({
                    "status": last_status,
                    "started_at": last_change,
                    "ended_at": None
                })

        status_changes = info.get("status_changes")
        if not isinstance(status_changes, list):
            info["status_changes"] = []
        if not info["status_changes"]:
            info["status_changes"].append({
                "from": None,
                "to": last_status,
                "changed_at": last_change
            })

        app_history = info.get("app_history")
        if not isinstance(app_history, list):
            info["app_history"] = []

        has_open_session = any(
            isinstance(session, dict) and session.get("ended_at") is None
            for session in info["app_history"]
        )
        if last_app != "Nada" and not has_open_session:
            info["app_history"].append({
                "app": last_app,
                "started_at": last_app_change,
                "ended_at": None
            })
        elif last_app == "Nada":
            for session in reversed(info["app_history"]):
                if isinstance(session, dict) and session.get("ended_at") is None:
                    session["ended_at"] = last_app_change
                    break

        self.prune_user_history(info)

    def get_current_status_started_at(self, info):
        status_history = info.get("status_history", [])
        if status_history:
            current_segment = status_history[-1]
            if isinstance(current_segment, dict) and current_segment.get("ended_at") is None:
                started_at = self.parse_timestamp(current_segment.get("started_at"))
                if started_at:
                    return started_at

        return self.parse_timestamp(info.get("last_change"))

    def record_status_change(self, info, previous_status, current_status, now):
        if previous_status == current_status:
            return

        now_iso = now.isoformat()
        status_history = info.setdefault("status_history", [])
        if status_history:
            current_segment = status_history[-1]
            if isinstance(current_segment, dict) and current_segment.get("ended_at") is None:
                current_segment["ended_at"] = now_iso

        status_history.append({
            "status": current_status,
            "started_at": now_iso,
            "ended_at": None
        })
        info.setdefault("status_changes", []).append({
            "from": previous_status,
            "to": current_status,
            "changed_at": now_iso
        })
        if current_status == "offline":
            info["last_seen_at"] = now_iso

        self.prune_user_history(info, now)

    def record_app_change(self, info, previous_app, current_app, now):
        if previous_app == current_app:
            return

        now_iso = now.isoformat()
        app_history = info.setdefault("app_history", [])
        for session in reversed(app_history):
            if isinstance(session, dict) and session.get("ended_at") is None:
                session["ended_at"] = now_iso
                break

        if current_app != "Nada":
            app_history.append({
                "app": current_app,
                "started_at": now_iso,
                "ended_at": None
            })

        info["last_app_change"] = now_iso
        self.prune_user_history(info, now)

    def get_time_window_stats(self, info, window):
        now = datetime.now()
        tracked_since = self.parse_timestamp(info.get("tracked_since")) or now
        window_start = now - window
        stats_start = max(window_start, tracked_since)

        online_seconds = 0
        offline_seconds = 0
        for entry in info.get("status_history", []):
            if not isinstance(entry, dict):
                continue

            started_at = self.parse_timestamp(entry.get("started_at"))
            if not started_at:
                continue

            ended_at = self.parse_timestamp(entry.get("ended_at")) if entry.get("ended_at") else now
            overlap_start = max(started_at, stats_start)
            overlap_end = min(ended_at, now)
            if overlap_end <= overlap_start:
                continue

            duration_seconds = (overlap_end - overlap_start).total_seconds()
            if entry.get("status") == "offline":
                offline_seconds += duration_seconds
            else:
                online_seconds += duration_seconds

        return {
            "online": online_seconds,
            "offline": offline_seconds,
            "covered": online_seconds + offline_seconds,
            "window": window.total_seconds()
        }

    def get_top_app_for_window(self, info, window):
        now = datetime.now()
        tracked_since = self.parse_timestamp(info.get("tracked_since")) or now
        window_start = max(now - window, tracked_since)
        app_totals = {}

        for entry in info.get("app_history", []):
            if not isinstance(entry, dict):
                continue

            app = entry.get("app")
            started_at = self.parse_timestamp(entry.get("started_at"))
            if not app or not started_at:
                continue

            ended_at = self.parse_timestamp(entry.get("ended_at")) if entry.get("ended_at") else now
            overlap_start = max(started_at, window_start)
            overlap_end = min(ended_at, now)
            if overlap_end <= overlap_start:
                continue

            app_totals[app] = app_totals.get(app, 0) + (overlap_end - overlap_start).total_seconds()

        if not app_totals:
            return None

        return max(app_totals.items(), key=lambda item: item[1])

    def format_recent_status_changes(self, info, limit=5):
        lines = []

        for entry in reversed(info.get("status_changes", [])):
            if not isinstance(entry, dict):
                continue

            changed_at = self.parse_timestamp(entry.get("changed_at"))
            if not changed_at:
                continue

            from_status = entry.get("from")
            to_status = entry.get("to", "offline")
            timestamp = int(changed_at.timestamp())
            if from_status:
                label = f"{self.status_label(from_status)} -> {self.status_label(to_status)}"
            else:
                label = self.status_label(to_status)

            lines.append(f"{label} em <t:{timestamp}:f> (<t:{timestamp}:R>)")
            if len(lines) >= limit:
                break

        if not lines:
            return "Nenhuma mudanca registrada ainda."

        return "\n".join(lines)

    def format_last_seen(self, info):
        status = info.get("last_status", "offline")
        if status != "offline":
            started_at = self.get_current_status_started_at(info)
            if started_at:
                timestamp = int(started_at.timestamp())
                return f"Online desde <t:{timestamp}:t> (<t:{timestamp}:R>)"
            return "Online agora"

        last_seen = self.parse_timestamp(info.get("last_seen_at"))
        if last_seen:
            timestamp = int(last_seen.timestamp())
            return f"Saiu em <t:{timestamp}:f> (<t:{timestamp}:R>)"

        tracked_since = self.parse_timestamp(info.get("tracked_since"))
        if tracked_since:
            timestamp = int(tracked_since.timestamp())
            return f"Ainda nao foi visto online desde <t:{timestamp}:f>"

        return "Desconhecido"

    def format_last_seen_detailed(self, info):
        status = info.get("last_status", "offline")
        if status != "offline":
            started_at = self.get_current_status_started_at(info)
            if started_at:
                timestamp = int(started_at.timestamp())
                return f"Online agora, desde <t:{timestamp}:F> (<t:{timestamp}:R>)"
            return "Online agora."

        last_seen = self.parse_timestamp(info.get("last_seen_at"))
        if last_seen:
            timestamp = int(last_seen.timestamp())
            return f"Foi visto por ultimo em <t:{timestamp}:F> (<t:{timestamp}:R>)"

        tracked_since = self.parse_timestamp(info.get("tracked_since"))
        if tracked_since:
            timestamp = int(tracked_since.timestamp())
            return f"Ainda nao foi visto online desde que o monitoramento comecou em <t:{timestamp}:F>."

        return "Ainda nao foi visto online desde que o monitoramento comecou."

    def get_current_app(self, member):
        for activity in member.activities:
            if activity.type == discord.ActivityType.playing:
                return activity.name
        return "Nada"

    async def process_member_update(self, member):
        info = self.data["users"].get(str(member.id))
        if not info:
            return False

        self.ensure_user_stats(info)
        now = datetime.now()
        curr_status = str(member.status)
        last_status = info.get("last_status")
        curr_app = self.get_current_app(member)
        last_app = info.get("last_app", "Nada")
        last_game_played = info.get("last_game_played", last_app)

        if curr_status == last_status and curr_app == last_app:
            return False

        if curr_status != last_status:
            self.record_status_change(info, last_status, curr_status, now)
            emoji = "🟢" if curr_status != "offline" else "🔴"
            await self.broadcast(info, f"{emoji} **{member.name}** agora está **{curr_status.upper()}**.")

        if curr_app != last_app:
            self.record_app_change(info, last_app, curr_app, now)
            if curr_app != "Nada":
                await self.broadcast(info, f"🎮 **{member.name}** abriu: **{curr_app}**")

        self.data["users"][str(member.id)].update({
            "last_status": curr_status,
            "last_change": now.isoformat(),
            "last_app": curr_app,
            "last_game_played": curr_app if curr_app != "Nada" else last_game_played
        })
        return True

    async def setup_hook(self):
        await self.tree.sync()
        self.check_presence.start()

    @tasks.loop(seconds=1)
    async def check_presence(self):
        data_changed = False

        for uid_str, info in list(self.data["users"].items()):
            target_id = int(uid_str)
            member = None
            for guild in self.guilds:
                m = guild.get_member(target_id)
                if m:
                    member = m
                    break
            
            if member and await self.process_member_update(member):
                data_changed = True

        if data_changed:
            self.save_data()
            await self.update_live_panel()

    async def on_presence_update(self, before, after):
        if await self.process_member_update(after):
            self.save_data()
            await self.update_live_panel()

    async def broadcast(self, info, message):
        if self.data.get("quiet_until") and datetime.now() < datetime.fromisoformat(self.data["quiet_until"]):
            return

        # Enviar para DM se configurado
        if info.get("dm_id"):
            try:
                user = await self.fetch_user(info["dm_id"])
                await user.send(message)
            except: pass
        
        # Enviar para Canal se configurado
        if info.get("channel_id"):
            try:
                chan = self.get_channel(info["channel_id"])
                if chan: await chan.send(message)
            except: pass

    async def update_live_panel(self):
        if not self.data["panel_channel_id"] or not self.data["panel_message_id"]:
            return

        try:
            channel = self.get_channel(self.data["panel_channel_id"])
            message = await channel.fetch_message(self.data["panel_message_id"])
            
            embed = discord.Embed(title="📊 PAINEL DE MONITORAMENTO", color=discord.Color.blue(), timestamp=datetime.now())
            if not self.data["users"]:
                embed.description = "Nenhum usuario sendo monitorado."
            for uid, info in self.data["users"].items():
                user = await self.fetch_user(int(uid))
                status_emoji = "🟢" if info['last_status'] != "offline" else "🔴"
                jogo = info.get("last_app", "Nada")
                ultimo_jogo = info.get("last_game_played", jogo)
                visto = self.format_last_seen(info)
                embed.add_field(
                    name=f"{status_emoji} {user.name}",
                    value=f"Status: `{info['last_status']}`\nJogando: `{jogo}`\nUltimo jogo: `{ultimo_jogo}`\nUltima vez visto: {visto}",
                    inline=False
                )
            
            await message.edit(embed=embed)
        except: pass

client = MyClient()

async def ensure_authorized_user(it: discord.Interaction):
    if it.user.id == AUTHORIZED_USER_ID:
        return True

    await it.response.send_message("❌ Voce nao tem permissao para usar este comando.", ephemeral=True)
    return False

@client.tree.command(name="adicionar_painel", description="Coloca o painel fixo neste canal")
async def adicionar_painel(it: discord.Interaction):
    if not await ensure_authorized_user(it):
        return

    # Deletar painel antigo se existir
    if client.data.get("panel_channel_id") and client.data.get("panel_message_id"):
        try:
            old_chan = client.get_channel(client.data.get("panel_channel_id"))
            old_msg = await old_chan.fetch_message(client.data.get("panel_message_id"))
            await old_msg.delete()
        except: pass

    embed = discord.Embed(title="Iniciando Painel...", color=discord.Color.light_grey())
    await it.response.send_message("Painel configurado!", ephemeral=True)
    msg = await it.channel.send(embed=embed)
    
    client.data["panel_channel_id"] = it.channel_id
    client.data["panel_message_id"] = msg.id
    client.save_data()
    await client.update_live_panel()

@client.tree.command(name="track_add", description="Adiciona alvo (Pode escolher Canal, DM ou Ambos)")
@app_commands.describe(target="Alvo", dm_notificar="Usuário que receberá DM", canal_notificar="Canal que receberá o log")
async def track_add(it: discord.Interaction, target: discord.User, dm_notificar: discord.User = None, canal_notificar: discord.TextChannel = None):
    if not await ensure_authorized_user(it):
        return

    if not dm_notificar and not canal_notificar:
        return await it.response.send_message("❌ Escolha pelo menos um destino (DM ou Canal)!", ephemeral=True)

    now = datetime.now()
    now_iso = now.isoformat()
    member = client.find_member(target.id)
    current_status = str(member.status) if member else "offline"
    current_app = client.get_current_app(member) if member else "Nada"
    existing_info = client.data["users"].get(str(target.id), {})
    client.data["users"][str(target.id)] = {
        "last_status": current_status,
        "last_change": now_iso,
        "last_app": current_app,
        "last_app_change": now_iso,
        "last_game_played": current_app if current_app != "Nada" else existing_info.get("last_game_played", "Nada"),
        "tracked_since": existing_info.get("tracked_since", now_iso),
        "last_seen_at": existing_info.get("last_seen_at", now_iso if current_status != "offline" else None),
        "status_history": existing_info.get("status_history", [{
            "status": current_status,
            "started_at": now_iso,
            "ended_at": None
        }]),
        "status_changes": existing_info.get("status_changes", [{
            "from": None,
            "to": current_status,
            "changed_at": now_iso
        }]),
        "app_history": existing_info.get("app_history", [] if current_app == "Nada" else [{
            "app": current_app,
            "started_at": now_iso,
            "ended_at": None
        }]),
        "dm_id": dm_notificar.id if dm_notificar else None,
        "channel_id": canal_notificar.id if canal_notificar else None
    }
    client.ensure_user_stats(client.data["users"][str(target.id)])
    client.save_data()
    await it.response.send_message(f"✅ Monitorando **{target.name}**", ephemeral=True)
    await client.update_live_panel()

@client.tree.command(name="track_stats", description="Mostra estatisticas do usuario monitorado")
@app_commands.describe(target="Alvo monitorado para consultar")
async def track_stats(it: discord.Interaction, target: discord.User):
    info = client.data["users"].get(str(target.id))
    if not info:
        return await it.response.send_message(f"`{target.name}` nao esta sendo monitorado.", ephemeral=True)

    client.ensure_user_stats(info)

    stats_24h = client.get_time_window_stats(info, timedelta(hours=24))
    stats_7d = client.get_time_window_stats(info, timedelta(days=7))
    top_app_24h = client.get_top_app_for_window(info, timedelta(hours=24))
    top_app_7d = client.get_top_app_for_window(info, timedelta(days=7))

    presence_lines = []
    for label, stats in (("24h", stats_24h), ("7d", stats_7d)):
        line = (
            f"{label}: online `{client.format_duration(stats['online'])}` | "
            f"offline `{client.format_duration(stats['offline'])}`"
        )
        if stats["covered"] + 1 < stats["window"]:
            line += f" | monitorado `{client.format_duration(stats['covered'])}`"
        presence_lines.append(line)

    game_lines = []
    if top_app_24h:
        game_lines.append(f"24h: `{top_app_24h[0]}` ({client.format_duration(top_app_24h[1])})")
    else:
        game_lines.append("24h: nenhum jogo detectado")

    if top_app_7d:
        game_lines.append(f"7d: `{top_app_7d[0]}` ({client.format_duration(top_app_7d[1])})")
    else:
        game_lines.append("7d: nenhum jogo detectado")

    game_lines.append(f"Ultimo jogo detectado: `{info.get('last_game_played', 'Nada')}`")

    embed = discord.Embed(
        title=f"Estatisticas de {target.name}",
        description=(
            f"Status atual: **{client.status_label(info.get('last_status', 'offline'))}**\n"
            f"{client.format_last_seen_detailed(info)}"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Presenca", value="\n".join(presence_lines), inline=False)
    embed.add_field(name="Mudancas recentes", value=client.format_recent_status_changes(info), inline=False)
    embed.add_field(name="Jogos", value="\n".join(game_lines), inline=False)

    await it.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="track_remove", description="Remove alvo do monitoramento")
@app_commands.describe(target="Alvo que deixara de ser monitorado")
async def track_remove(it: discord.Interaction, target: discord.User):
    if not await ensure_authorized_user(it):
        return

    removed = client.data["users"].pop(str(target.id), None)
    if not removed:
        return await it.response.send_message(f"`{target.name}` nao esta sendo monitorado.", ephemeral=True)

    client.save_data()
    await it.response.send_message(f"Removido **{target.name}** do monitoramento.", ephemeral=True)
    await client.update_live_panel()

@client.tree.command(name="purge", description="Apaga todas as mensagens deste canal")
@app_commands.default_permissions(manage_messages=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(it: discord.Interaction):
    if not isinstance(it.channel, discord.TextChannel):
        return await it.response.send_message("❌ Este comando so funciona em canais de texto.", ephemeral=True)

    await it.response.defer(ephemeral=True, thinking=True)

    channel = it.channel

    try:
        deleted = await channel.purge(
            limit=None,
            bulk=True,
            reason=f"Purge solicitado por {it.user} ({it.user.id})"
        )

        if client.data.get("panel_channel_id") == channel.id:
            client.data["panel_channel_id"] = None
            client.data["panel_message_id"] = None
            client.save_data()

        await it.followup.send(
            f"✅ {len(deleted)} mensagens foram apagadas de {channel.mention}.",
            ephemeral=True
        )
    except discord.Forbidden:
        await it.followup.send("❌ O bot nao tem permissao para apagar mensagens neste canal.", ephemeral=True)
    except discord.HTTPException as exc:
        await it.followup.send(f"❌ Falha ao apagar mensagens: {exc}", ephemeral=True)

@purge.error
async def purge_error(it: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ Voce precisa da permissao de gerenciar mensagens para usar este comando."
        if it.response.is_done():
            await it.followup.send(message, ephemeral=True)
        else:
            await it.response.send_message(message, ephemeral=True)

client.run(TOKEN)
