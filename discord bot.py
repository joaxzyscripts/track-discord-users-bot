import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timedelta
import json
import logging
import os

TOKEN = ''
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, 'tracked_users.json')
LEGACY_DATA_FILE = os.path.abspath('tracked_users.json')
HISTORY_RETENTION_DAYS = 30
AUTHORIZED_USER_ID = 
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
        self.member_update_locks = {}
        self.panel_update_lock = asyncio.Lock()
        self.panel_dirty = False
        self.last_panel_signature = None
        self.data = self.load_data()
        self.save_data()

    def default_data(self):
        return {
            "users": {},
            "quiet_until": None,
            "panel_channel_id": None,
            "panel_message_id": None
        }

    def default_notifications(self):
        return {
            "notify_online_offline": True,
            "notify_status_changes": True,
            "notify_game_changes": True,
            "notify_device_changes": True,
            "min_session_minutes": 0
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
            user_info.setdefault("last_device", "Desconhecido")
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

    def mark_panel_dirty(self):
        self.panel_dirty = True

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

    def get_tracked_display_name(self, user_id, info=None):
        user = self.get_user(user_id)
        if user:
            return user.name

        member = self.find_member(user_id)
        if member:
            return member.name

        if isinstance(info, dict) and info.get("last_name"):
            return info["last_name"]

        return str(user_id)

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

        device_history = []
        for entry in info.get("device_history", []):
            if not isinstance(entry, dict):
                continue

            device = entry.get("device")
            started_at = self.parse_timestamp(entry.get("started_at"))
            if not device or not started_at:
                continue

            ended_at = self.parse_timestamp(entry.get("ended_at")) if entry.get("ended_at") else None
            if ended_at and ended_at < cutoff:
                continue

            device_history.append({
                "device": str(device),
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat() if ended_at else None
            })

        info["device_history"] = device_history[-500:]

        device_changes = []
        for entry in info.get("device_changes", []):
            if not isinstance(entry, dict):
                continue

            changed_at = self.parse_timestamp(entry.get("changed_at"))
            if not changed_at:
                continue

            device_changes.append({
                "from": entry.get("from"),
                "to": entry.get("to"),
                "changed_at": changed_at.isoformat()
            })

        info["device_changes"] = device_changes[-50:]

    def ensure_user_stats(self, info):
        now_iso = datetime.now().isoformat()
        last_status = info.get("last_status", "offline")
        last_change = info.get("last_change") or now_iso
        last_app = info.get("last_app", "Nada")
        last_device = info.get("last_device") or "Desconhecido"
        tracked_since = info.get("tracked_since") or last_change
        last_app_change = info.get("last_app_change") or tracked_since

        info["last_device"] = last_device
        info["tracked_since"] = tracked_since
        info["last_app_change"] = last_app_change
        info.setdefault("last_seen_at", last_change if last_status == "offline" else tracked_since)

        notifications = info.get("notifications")
        if not isinstance(notifications, dict):
            notifications = {}

        default_notifications = self.default_notifications()
        for key, value in default_notifications.items():
            notifications.setdefault(key, value)

        try:
            notifications["min_session_minutes"] = max(0, int(notifications.get("min_session_minutes", 0)))
        except (TypeError, ValueError):
            notifications["min_session_minutes"] = 0

        info["notifications"] = notifications

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

        device_history = info.get("device_history")
        if not isinstance(device_history, list):
            info["device_history"] = []
            device_history = info["device_history"]

        device_changes = info.get("device_changes")
        if not isinstance(device_changes, list):
            info["device_changes"] = []
            device_changes = info["device_changes"]

        has_open_device_session = any(
            isinstance(session, dict) and session.get("ended_at") is None
            for session in device_history
        )
        if last_status != "offline" and last_device != "Desconhecido" and not has_open_device_session:
            device_history.append({
                "device": last_device,
                "started_at": last_change,
                "ended_at": None
            })
        elif last_status == "offline":
            for session in reversed(device_history):
                if isinstance(session, dict) and session.get("ended_at") is None:
                    session["ended_at"] = last_change
                    break

        if last_status != "offline" and last_device != "Desconhecido" and not device_changes:
            device_changes.append({
                "from": None,
                "to": last_device,
                "changed_at": last_change
            })

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

    def get_member_lock(self, user_id):
        if user_id not in self.member_update_locks:
            self.member_update_locks[user_id] = asyncio.Lock()
        return self.member_update_locks[user_id]

    def close_device_session(self, info, now_iso):
        for session in reversed(info.setdefault("device_history", [])):
            if isinstance(session, dict) and session.get("ended_at") is None:
                session["ended_at"] = now_iso
                return

    def open_device_session(self, info, device, now_iso):
        if not device or device == "Desconhecido":
            return

        info.setdefault("device_history", []).append({
            "device": device,
            "started_at": now_iso,
            "ended_at": None
        })

    def record_device_change(self, info, previous_device, current_device, now):
        if previous_device == current_device or current_device == "Desconhecido":
            return

        info.setdefault("device_changes", []).append({
            "from": previous_device,
            "to": current_device,
            "changed_at": now.isoformat()
        })
        self.prune_user_history(info, now)

    def sync_device_session(self, info, previous_status, current_status, previous_device, current_device, now):
        now_iso = now.isoformat()
        if current_status == "offline":
            self.close_device_session(info, now_iso)
            self.prune_user_history(info, now)
            return

        if previous_status == "offline":
            self.close_device_session(info, now_iso)
            self.open_device_session(info, current_device, now_iso)
            self.prune_user_history(info, now)
            return

        if current_device != previous_device:
            self.close_device_session(info, now_iso)
            self.open_device_session(info, current_device, now_iso)
            self.record_device_change(info, previous_device, current_device, now)
            return

        has_open_device_session = any(
            isinstance(session, dict) and session.get("ended_at") is None
            for session in info.get("device_history", [])
        )
        if not has_open_device_session:
            self.open_device_session(info, current_device, now_iso)
            self.prune_user_history(info, now)

    def get_device_stats_for_window(self, info, window):
        now = datetime.now()
        tracked_since = self.parse_timestamp(info.get("tracked_since")) or now
        window_start = max(now - window, tracked_since)
        device_totals = {}

        for entry in info.get("device_history", []):
            if not isinstance(entry, dict):
                continue

            device = entry.get("device")
            started_at = self.parse_timestamp(entry.get("started_at"))
            if not device or not started_at:
                continue

            ended_at = self.parse_timestamp(entry.get("ended_at")) if entry.get("ended_at") else now
            overlap_start = max(started_at, window_start)
            overlap_end = min(ended_at, now)
            if overlap_end <= overlap_start:
                continue

            device_totals[device] = device_totals.get(device, 0) + (overlap_end - overlap_start).total_seconds()

        switch_count = 0
        for entry in info.get("device_changes", []):
            if not isinstance(entry, dict):
                continue

            changed_at = self.parse_timestamp(entry.get("changed_at"))
            if not changed_at or changed_at < window_start or changed_at > now:
                continue

            switch_count += 1

        return {
            "devices": device_totals,
            "switches": switch_count
        }

    def format_device_stats(self, info, label, window):
        stats = self.get_device_stats_for_window(info, window)
        if not stats["devices"]:
            line = f"{label}: nenhum dispositivo detectado"
        else:
            ranked_devices = sorted(stats["devices"].items(), key=lambda item: item[1], reverse=True)
            parts = [f"{device} `{self.format_duration(seconds)}`" for device, seconds in ranked_devices]
            line = f"{label}: " + " | ".join(parts)

        return f"{line} | trocas `{stats['switches']}`"

    def format_last_device_switch(self, info):
        for entry in reversed(info.get("device_changes", [])):
            if not isinstance(entry, dict):
                continue

            changed_at = self.parse_timestamp(entry.get("changed_at"))
            if not changed_at:
                continue

            timestamp = int(changed_at.timestamp())
            previous_device = entry.get("from") or "Desconhecido"
            current_device = entry.get("to") or "Desconhecido"
            return f"{previous_device} -> {current_device} em <t:{timestamp}:f> (<t:{timestamp}:R>)"

        return "Nenhuma troca de dispositivo registrada."

    def should_notify(self, info, event_type, duration_seconds=None):
        settings = dict(self.default_notifications())
        settings.update(info.get("notifications", {}))

        if event_type in {"online", "offline"}:
            if not settings.get("notify_online_offline", True):
                return False

            min_seconds = max(0, int(settings.get("min_session_minutes", 0))) * 60
            if duration_seconds is not None and duration_seconds < min_seconds:
                return False
            return True

        if event_type == "status":
            return settings.get("notify_status_changes", True)
        if event_type == "game":
            return settings.get("notify_game_changes", True)
        if event_type == "device":
            return settings.get("notify_device_changes", True)
        return True

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

    def is_active_client_status(self, status):
        if status is None:
            return False

        return str(status) not in {"offline", "invisible"}

    def get_member_device(self, member):
        if not member:
            return None

        devices = []
        if self.is_active_client_status(getattr(member, "desktop_status", None)):
            devices.append("PC")
        if self.is_active_client_status(getattr(member, "mobile_status", None)):
            devices.append("Phone")
        if self.is_active_client_status(getattr(member, "web_status", None)):
            devices.append("Browser")

        if not devices:
            return None

        return " + ".join(devices)

    def get_current_app(self, member):
        if not member or not getattr(member, "activities", None):
            return "Nada"

        for activity in member.activities:
            if activity.type == discord.ActivityType.playing:
                return activity.name
        return "Nada"

    async def process_member_update(self, member):
        return await self.handle_member_update(member)

        info = self.data["users"].get(str(member.id))
        if not info:
            return False

        self.ensure_user_stats(info)
        now = datetime.now()
        curr_status = str(member.status)
        last_status = info.get("last_status")
        curr_app = self.get_current_app(member)
        last_app = info.get("last_app", "Nada")
        detected_device = self.get_member_device(member)
        last_device = info.get("last_device", "Desconhecido")
        if detected_device:
            curr_device = detected_device
        elif curr_status == "offline" or last_status != "offline":
            curr_device = last_device
        else:
            curr_device = "Desconhecido"
        last_game_played = info.get("last_game_played", last_app)

        if curr_status == last_status and curr_app == last_app and curr_device == last_device:
            return False

        if curr_status != last_status:
            previous_started_at = self.get_current_status_started_at(info)
            previous_duration = None
            if previous_started_at:
                previous_duration = self.format_duration((now - previous_started_at).total_seconds())

            self.record_status_change(info, last_status, curr_status, now)
            if curr_status == "offline":
                duration_text = previous_duration or "algum tempo"
                await self.broadcast(
                    info,
                    f"[OFFLINE] **{member.name}** ficou online por **{duration_text}** e agora esta offline."
                )
            elif last_status == "offline":
                duration_text = previous_duration or "algum tempo"
                device_text = curr_device if curr_device != "Desconhecido" else "dispositivo desconhecido"
                await self.broadcast(
                    info,
                    f"[ONLINE] **{member.name}** ficou offline por **{duration_text}** e entrou pelo **{device_text}**."
                )
            else:
                device_suffix = ""
                if curr_device != "Desconhecido":
                    device_suffix = f" no **{curr_device}**"
                await self.broadcast(
                    info,
                    f"[STATUS] **{member.name}** agora esta **{self.status_label(curr_status)}**{device_suffix}."
                )

        if False and curr_status != last_status:
            self.record_status_change(info, last_status, curr_status, now)
            emoji = "🟢" if curr_status != "offline" else "🔴"
            await self.broadcast(info, f"{emoji} **{member.name}** agora está **{curr_status.upper()}**.")

        if curr_app != last_app:
            self.record_app_change(info, last_app, curr_app, now)
            if curr_app != "Nada":
                await self.broadcast(info, f"🎮 **{member.name}** abriu: **{curr_app}**")

        if curr_status != "offline" and curr_device != last_device and curr_status == last_status:
            await self.broadcast(info, f"[DEVICE] **{member.name}** agora esta no **{curr_device}**.")

        self.data["users"][str(member.id)].update({
            "last_status": curr_status,
            "last_change": now.isoformat(),
            "last_app": curr_app,
            "last_device": curr_device,
            "last_game_played": curr_app if curr_app != "Nada" else last_game_played
        })
        return True

    async def handle_member_update(self, member):
        async with self.get_member_lock(member.id):
            info = self.data["users"].get(str(member.id))
            if not info:
                return False

            self.ensure_user_stats(info)
            now = datetime.now()
            curr_status = str(member.status)
            last_status = info.get("last_status")
            curr_app = self.get_current_app(member)
            last_app = info.get("last_app", "Nada")
            detected_device = self.get_member_device(member)
            last_device = info.get("last_device", "Desconhecido")
            if detected_device:
                curr_device = detected_device
            elif curr_status == "offline" or last_status != "offline":
                curr_device = last_device
            else:
                curr_device = "Desconhecido"
            last_game_played = info.get("last_game_played", last_app)

            if curr_status == last_status and curr_app == last_app and curr_device == last_device:
                return False

            previous_duration_seconds = None
            previous_duration_text = None
            if curr_status != last_status:
                previous_started_at = self.get_current_status_started_at(info)
                if previous_started_at:
                    previous_duration_seconds = max(0, (now - previous_started_at).total_seconds())
                    previous_duration_text = self.format_duration(previous_duration_seconds)

            if curr_status != last_status:
                self.record_status_change(info, last_status, curr_status, now)

            self.sync_device_session(info, last_status, curr_status, last_device, curr_device, now)

            if curr_status != last_status:
                if curr_status == "offline":
                    duration_text = previous_duration_text or "algum tempo"
                    if self.should_notify(info, "offline", previous_duration_seconds):
                        await self.broadcast(
                            info,
                            f"[OFFLINE] **{member.name}** ficou online por **{duration_text}** e agora esta offline."
                        )
                elif last_status == "offline":
                    duration_text = previous_duration_text or "algum tempo"
                    device_text = curr_device if curr_device != "Desconhecido" else "dispositivo desconhecido"
                    if self.should_notify(info, "online", previous_duration_seconds):
                        await self.broadcast(
                            info,
                            f"[ONLINE] **{member.name}** ficou offline por **{duration_text}** e entrou pelo **{device_text}**."
                        )
                elif self.should_notify(info, "status"):
                    device_suffix = ""
                    if curr_device != "Desconhecido":
                        device_suffix = f" no **{curr_device}**"
                    await self.broadcast(
                        info,
                        f"[STATUS] **{member.name}** agora esta **{self.status_label(curr_status)}**{device_suffix}."
                    )

            if curr_app != last_app:
                self.record_app_change(info, last_app, curr_app, now)
                if curr_app != "Nada" and self.should_notify(info, "game"):
                    await self.broadcast(info, f"ðŸŽ® **{member.name}** abriu: **{curr_app}**")

            if curr_status != "offline" and curr_device != last_device and curr_status == last_status:
                if self.should_notify(info, "device"):
                    await self.broadcast(
                        info,
                        f"[DEVICE] **{member.name}** trocou de **{last_device}** para **{curr_device}**."
                    )

            self.data["users"][str(member.id)].update({
                "last_status": curr_status,
                "last_change": now.isoformat(),
                "last_app": curr_app,
                "last_device": curr_device,
                "last_game_played": curr_app if curr_app != "Nada" else last_game_played,
                "last_name": member.name
            })
            return True

    async def refresh_tracked_members(self):
        data_changed = False

        for uid_str in list(self.data["users"].keys()):
            target_id = int(uid_str)
            member = self.find_member(target_id)
            if not member:
                continue

            if await self.handle_member_update(member):
                data_changed = True

        if data_changed:
            self.save_data()
            self.mark_panel_dirty()

        return data_changed

    async def setup_hook(self):
        await self.tree.sync()
        self.check_presence.start()
        self.panel_updater.start()

    async def on_ready(self):
        await self.refresh_tracked_members()

    async def on_resumed(self):
        await self.refresh_tracked_members()

    @tasks.loop(seconds=15)
    async def check_presence(self):
        await self.refresh_tracked_members()

    @check_presence.before_loop
    async def before_check_presence(self):
        await self.wait_until_ready()

    async def on_presence_update(self, before, after):
        if await self.handle_member_update(after):
            self.save_data()
            self.mark_panel_dirty()

    async def broadcast(self, info, message):
        if self.data.get("quiet_until") and datetime.now() < datetime.fromisoformat(self.data["quiet_until"]):
            return

        # Enviar para DM se configurado
        if info.get("dm_id"):
            try:
                user = self.get_user(info["dm_id"])
                if user is None:
                    user = await self.fetch_user(info["dm_id"])
                await user.send(message)
            except Exception:
                logging.exception("Failed to send DM notification")
        
        # Enviar para Canal se configurado
        if info.get("channel_id"):
            try:
                chan = self.get_channel(info["channel_id"])
                if chan:
                    await chan.send(message)
            except Exception:
                logging.exception("Failed to send channel notification")

    @tasks.loop(seconds=10)
    async def panel_updater(self):
        if not self.panel_dirty:
            return

        self.panel_dirty = False
        await self.update_live_panel()

    @panel_updater.before_loop
    async def before_panel_updater(self):
        await self.wait_until_ready()

    async def update_live_panel(self):
        if not self.data["panel_channel_id"] or not self.data["panel_message_id"]:
            return

        async with self.panel_update_lock:
            try:
                channel = self.get_channel(self.data["panel_channel_id"])
                if channel is None:
                    return

                signature_rows = []
                for uid, info in self.data["users"].items():
                    signature_rows.append((
                        uid,
                        info.get("last_name"),
                        info.get("last_status"),
                        info.get("last_app"),
                        info.get("last_game_played"),
                        info.get("last_device"),
                        info.get("last_seen_at"),
                        info.get("last_change")
                    ))

                panel_signature = tuple(signature_rows)
                if panel_signature == self.last_panel_signature:
                    return

                message = await channel.fetch_message(self.data["panel_message_id"])
                embed = discord.Embed(title="?? PAINEL DE MONITORAMENTO", color=discord.Color.blue(), timestamp=datetime.now())
                if not self.data["users"]:
                    embed.description = "Nenhum usuario sendo monitorado."

                for uid, info in self.data["users"].items():
                    display_name = self.get_tracked_display_name(int(uid), info)
                    status_emoji = "??" if info['last_status'] != "offline" else "??"
                    jogo = info.get("last_app", "Nada")
                    ultimo_jogo = info.get("last_game_played", jogo)
                    visto = self.format_last_seen(info)
                    dispositivo_label = "Dispositivo" if info['last_status'] != "offline" else "Ultimo dispositivo"
                    dispositivo = info.get("last_device", "Desconhecido")
                    embed.add_field(
                        name=f"{status_emoji} {display_name}",
                        value=f"Status: `{info['last_status']}`\n{dispositivo_label}: `{dispositivo}`\nJogando: `{jogo}`\nUltimo jogo: `{ultimo_jogo}`\nUltima vez visto: {visto}",
                        inline=False
                    )

                await message.edit(embed=embed)
                self.last_panel_signature = panel_signature
            except Exception:
                self.panel_dirty = True
                logging.exception("Failed to update live panel")

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
    current_device = client.get_member_device(member)
    existing_info = client.data["users"].get(str(target.id), {})
    client.data["users"][str(target.id)] = {
        "last_status": current_status,
        "last_change": now_iso,
        "last_app": current_app,
        "last_device": current_device or existing_info.get("last_device", "Desconhecido"),
        "last_app_change": now_iso,
        "last_game_played": current_app if current_app != "Nada" else existing_info.get("last_game_played", "Nada"),
        "last_name": target.name,
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
        "device_history": existing_info.get("device_history", []),
        "device_changes": existing_info.get("device_changes", []),
        "notifications": existing_info.get("notifications", client.default_notifications()),
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
    device_lines = [
        client.format_device_stats(info, "24h", timedelta(hours=24)),
        client.format_device_stats(info, "7d", timedelta(days=7)),
        f"Ultima troca: {client.format_last_device_switch(info)}"
    ]
    notification_settings = dict(client.default_notifications())
    notification_settings.update(info.get("notifications", {}))

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
    filter_lines = [
        f"Online/offline: `{'ON' if notification_settings.get('notify_online_offline', True) else 'OFF'}`",
        f"Status: `{'ON' if notification_settings.get('notify_status_changes', True) else 'OFF'}`",
        f"Jogos: `{'ON' if notification_settings.get('notify_game_changes', True) else 'OFF'}`",
        f"Dispositivo: `{'ON' if notification_settings.get('notify_device_changes', True) else 'OFF'}`",
        f"Minimo online/offline: `{notification_settings.get('min_session_minutes', 0)} min`"
    ]

    embed = discord.Embed(
        title=f"Estatisticas de {target.name}",
        description=(
            f"Status atual: **{client.status_label(info.get('last_status', 'offline'))}**\n"
            f"Dispositivo: **{info.get('last_device', 'Desconhecido')}**\n"
            f"{client.format_last_seen_detailed(info)}"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Presenca", value="\n".join(presence_lines), inline=False)
    embed.add_field(name="Dispositivos", value="\n".join(device_lines), inline=False)
    embed.add_field(name="Mudancas recentes", value=client.format_recent_status_changes(info), inline=False)
    embed.add_field(name="Jogos", value="\n".join(game_lines), inline=False)
    embed.add_field(name="Filtros", value="\n".join(filter_lines), inline=False)

    await it.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="track_filters", description="Configura filtros de notificacao do usuario monitorado")
@app_commands.describe(
    target="Alvo monitorado para configurar",
    notify_online_offline="Receber alertas de entrada/saida",
    notify_status_changes="Receber alertas de idle/dnd/online",
    notify_game_changes="Receber alertas de jogos",
    notify_device_changes="Receber alertas de troca de dispositivo",
    min_session_minutes="Ignora online/offline menores que X minutos"
)
async def track_filters(
    it: discord.Interaction,
    target: discord.User,
    notify_online_offline: bool = None,
    notify_status_changes: bool = None,
    notify_game_changes: bool = None,
    notify_device_changes: bool = None,
    min_session_minutes: app_commands.Range[int, 0, 1440] = None
):
    if not await ensure_authorized_user(it):
        return

    info = client.data["users"].get(str(target.id))
    if not info:
        return await it.response.send_message(f"`{target.name}` nao esta sendo monitorado.", ephemeral=True)

    client.ensure_user_stats(info)
    settings = dict(client.default_notifications())
    settings.update(info.get("notifications", {}))

    if notify_online_offline is not None:
        settings["notify_online_offline"] = notify_online_offline
    if notify_status_changes is not None:
        settings["notify_status_changes"] = notify_status_changes
    if notify_game_changes is not None:
        settings["notify_game_changes"] = notify_game_changes
    if notify_device_changes is not None:
        settings["notify_device_changes"] = notify_device_changes
    if min_session_minutes is not None:
        settings["min_session_minutes"] = int(min_session_minutes)

    info["notifications"] = settings
    client.save_data()

    lines = [
        f"Online/offline: `{'ON' if settings['notify_online_offline'] else 'OFF'}`",
        f"Status: `{'ON' if settings['notify_status_changes'] else 'OFF'}`",
        f"Jogos: `{'ON' if settings['notify_game_changes'] else 'OFF'}`",
        f"Dispositivo: `{'ON' if settings['notify_device_changes'] else 'OFF'}`",
        f"Minimo online/offline: `{settings['min_session_minutes']} min`"
    ]
    await it.response.send_message(
        f"Filtros de **{target.name}** atualizados:\n" + "\n".join(lines),
        ephemeral=True
    )

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
