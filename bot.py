import asyncio
import io
import json
import os
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Dict, Optional

import discord
from discord import app_commands
import requests
from dotenv import load_dotenv
from PIL import Image


load_dotenv(override=True)


@dataclass
class TrackedPlayer:
    puuid: str
    name: Optional[str] = None
    enabled: bool = True


@dataclass
class PlayerState:
    in_game: bool
    last_known_match_id: Optional[str]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_tracked_players(raw: str) -> list[TrackedPlayer]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TRACKED_PLAYERS_JSON must be valid JSON") from exc

    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("TRACKED_PLAYERS_JSON must be a non-empty array")

    result: list[TrackedPlayer] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid TRACKED_PLAYERS_JSON item at index {i}")
        name = item.get("name")
        puuid = item.get("puuid")
        if name is not None and not isinstance(name, str):
            raise RuntimeError(f"Invalid TRACKED_PLAYERS_JSON item at index {i}")
        if not isinstance(puuid, str):
            raise RuntimeError(f"Invalid TRACKED_PLAYERS_JSON item at index {i}")
        enabled = item.get("enabled", True)
        result.append(TrackedPlayer(puuid=puuid, name=name, enabled=bool(enabled)))
    return result


def filter_named_players(players: list[TrackedPlayer]) -> list[TrackedPlayer]:
    # Legacy env format used null names; keep only named entries to avoid re-importing old records.
    return [p for p in players if p.name is not None]


DISCORD_TOKEN = require_env("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(require_env("DISCORD_CHANNEL_ID"))
RIOT_API_KEY = require_env("RIOT_API_KEY")
LOL_PLATFORM_REGION = os.getenv("LOL_PLATFORM_REGION", "jp1")
RIOT_REGION = os.getenv("RIOT_REGION", "asia")
POLL_INTERVAL_SECONDS = max(15, int(os.getenv("POLL_INTERVAL_SECONDS", "60")))
TRACKED_PLAYERS = filter_named_players(parse_tracked_players(require_env("TRACKED_PLAYERS_JSON")))
TRACKED_PLAYERS_FILE = os.getenv("TRACKED_PLAYERS_FILE", "tracked_players.json")
TRACKING_STATE_FILE = os.getenv("TRACKING_STATE_FILE", "tracking_state.json")
PREVIEW_MODE = "--preview" in sys.argv
DEBUG_PREVIEW_PUUID = os.getenv("DEBUG_PREVIEW_PUUID")

TARGET_QUEUE_IDS = {400, 420, 430, 440, 450, 490}
RANKED_QUEUE_IDS = {420, 440}
NORMAL_QUEUE_IDS = {400, 430, 490}
ARAM_QUEUE_IDS = {450}


def queue_name(queue_id: int) -> str:
    if queue_id in RANKED_QUEUE_IDS:
        return "ランク"
    if queue_id in NORMAL_QUEUE_IDS:
        return "ノーマル"
    if queue_id in ARAM_QUEUE_IDS:
        return "ARAM"
    return "その他"


def role_name(position: str) -> str:
    mapping = {
        "TOP": "トップ",
        "JUNGLE": "ジャングル",
        "MIDDLE": "ミッド",
        "BOTTOM": "ADC",
        "UTILITY": "サポート",
    }
    return mapping.get(position, "不明")


def load_tracked_players_from_file(file_path: str) -> list[TrackedPlayer]:
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        result: list[TrackedPlayer] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            puuid = item.get("puuid")
            name = item.get("name")
            enabled = item.get("enabled", True)
            if isinstance(puuid, str):
                result.append(
                    TrackedPlayer(
                        puuid=puuid,
                        name=name if isinstance(name, str) else None,
                        enabled=bool(enabled),
                    )
                )
        return result
    except Exception:
        return []


def save_tracked_players_to_file(file_path: str, players: list[TrackedPlayer]) -> None:
    rows = [{"name": p.name, "puuid": p.puuid, "enabled": p.enabled} for p in players]
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def merge_tracked_players(base: list[TrackedPlayer], extra: list[TrackedPlayer]) -> list[TrackedPlayer]:
    order: list[str] = []
    merged_map: dict[str, TrackedPlayer] = {}
    for p in base:
        if p.puuid not in merged_map:
            order.append(p.puuid)
        merged_map[p.puuid] = p
    for p in extra:
        if p.puuid not in merged_map:
            order.append(p.puuid)
        merged_map[p.puuid] = p
    return [merged_map[puuid] for puuid in order]


def load_tracking_enabled(file_path: str) -> bool:
    if not os.path.exists(file_path):
        return True
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("enabled", True)) if isinstance(data, dict) else True
    except Exception:
        return True


def save_tracking_enabled(file_path: str, enabled: bool) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump({"enabled": bool(enabled)}, f, ensure_ascii=False, indent=2)


class RiotApiClient:
    def __init__(self, api_key: str, regional_route: str):
        self.api_key = api_key
        self.regional_route = regional_route

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Riot-Token": self.api_key}

    def get_latest_match_id(self, puuid: str) -> Optional[str]:
        url = (
            f"https://{self.regional_route}.api.riotgames.com/lol/match/v5/matches/"
            f"by-puuid/{puuid}/ids?start=0&count=1"
        )
        res = requests.get(url, headers=self.headers, timeout=20)
        res.raise_for_status()
        data = res.json()
        if not data:
            return None
        return data[0]

    def get_puuid_by_riot_id(self, game_name: str, tag_line: str) -> Optional[str]:
        game_name_enc = urllib.parse.quote(game_name, safe="")
        tag_line_enc = urllib.parse.quote(tag_line, safe="")
        url = (
            f"https://{self.regional_route}.api.riotgames.com/riot/account/v1/accounts/"
            f"by-riot-id/{game_name_enc}/{tag_line_enc}"
        )
        res = requests.get(url, headers=self.headers, timeout=20)
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json().get("puuid")

    def get_recent_match_ids(self, puuid: str, count: int = 20) -> list[str]:
        url = (
            f"https://{self.regional_route}.api.riotgames.com/lol/match/v5/matches/"
            f"by-puuid/{puuid}/ids?start=0&count={count}"
        )
        res = requests.get(url, headers=self.headers, timeout=20)
        res.raise_for_status()
        return res.json()

    def get_match_summary(self, match_id: str, puuid: str) -> Optional[dict]:
        url = f"https://{self.regional_route}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        res = requests.get(url, headers=self.headers, timeout=20)
        res.raise_for_status()

        data = res.json()
        info = data.get("info", {})
        participants = info.get("participants", [])

        participant = next((p for p in participants if p.get("puuid") == puuid), None)
        if not participant:
            return None

        team_id = int(participant.get("teamId", 0))
        role = str(participant.get("teamPosition") or participant.get("individualPosition") or "")
        enemy = next(
            (
                p
                for p in participants
                if int(p.get("teamId", 0)) != team_id
                and str(p.get("teamPosition") or p.get("individualPosition") or "") == role
            ),
            None,
        )

        team_kills = sum(int(p.get("kills", 0)) for p in participants if int(p.get("teamId", 0)) == team_id)
        kills = int(participant.get("kills", 0))
        assists = int(participant.get("assists", 0))
        kp = ((kills + assists) / team_kills * 100.0) if team_kills > 0 else 0.0

        cs = int(participant.get("totalMinionsKilled", 0)) + int(participant.get("neutralMinionsKilled", 0))
        item_ids = [int(participant.get(f"item{i}", 0)) for i in range(7)]

        return {
            "match_id": match_id,
            "player_name": participant.get("riotIdGameName") or participant.get("summonerName") or None,
            "player_tag_line": participant.get("riotIdTagline") or None,
            "queue_id": int(info.get("queueId", 0)),
            "participant_id": int(participant.get("participantId", 0)),
            "role": role,
            "champion_name": participant.get("championName", "Unknown"),
            "opponent_champion_name": enemy.get("championName") if isinstance(enemy, dict) else None,
            "kills": kills,
            "deaths": int(participant.get("deaths", 0)),
            "assists": assists,
            "kill_participation": kp,
            "win": bool(participant.get("win", False)),
            "game_duration_seconds": int(info.get("gameDuration", 0)),
            "damage_to_champions": int(participant.get("totalDamageDealtToChampions", 0)),
            "cs": cs,
            "item_ids": item_ids,
        }

    def get_latest_match_summary_for_queues(
        self,
        puuid: str,
        allowed_queue_ids: set[int],
        count: int = 30,
    ) -> Optional[dict]:
        match_ids = self.get_recent_match_ids(puuid, count=count)
        for match_id in match_ids:
            summary = self.get_match_summary(match_id, puuid)
            if summary and int(summary.get("queue_id", 0)) in allowed_queue_ids:
                return summary
        return None

    def get_item_purchase_order(self, match_id: str, participant_id: int) -> list[int]:
        if participant_id <= 0:
            return []
        url = f"https://{self.regional_route}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        res = requests.get(url, headers=self.headers, timeout=20)
        res.raise_for_status()

        timeline = res.json()
        frames = timeline.get("info", {}).get("frames", [])
        purchases: list[int] = []

        for frame in frames:
            events = frame.get("events", [])
            for event in events:
                if int(event.get("participantId", 0)) != participant_id:
                    continue

                event_type = event.get("type")
                if event_type == "ITEM_PURCHASED":
                    item_id = int(event.get("itemId", 0))
                    if item_id > 0:
                        purchases.append(item_id)
                elif event_type == "ITEM_UNDO":
                    before_id = int(event.get("beforeId", 0))
                    if before_id > 0:
                        for idx in range(len(purchases) - 1, -1, -1):
                            if purchases[idx] == before_id:
                                purchases.pop(idx)
                                break

        return purchases

    def get_result_streak(self, puuid: str, count: int = 20) -> Optional[dict]:
        match_ids = self.get_recent_match_ids(puuid, count=count)
        streak_type: Optional[str] = None
        streak_count = 0

        for match_id in match_ids:
            summary = self.get_match_summary(match_id, puuid)
            if not summary:
                continue

            current = "連勝" if summary["win"] else "連敗"
            if streak_type is None:
                streak_type = current
                streak_count = 1
                continue

            if current == streak_type:
                streak_count += 1
            else:
                break

        if not streak_type:
            return None
        return {"type": streak_type, "count": streak_count}


class DataDragonClient:
    def __init__(self):
        self.version: Optional[str] = None
        self.items: dict[str, dict] = {}
        self.champions_ja: dict[str, str] = {}

    def _ensure_items(self) -> None:
        if self.version and self.items:
            return

        versions = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=20)
        versions.raise_for_status()
        self.version = versions.json()[0]

        items = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{self.version}/data/en_US/item.json",
            timeout=20,
        )
        items.raise_for_status()
        self.items = items.json().get("data", {})

    def _ensure_champions_ja(self) -> None:
        if self.version and self.champions_ja:
            return
        if not self.version:
            self._ensure_items()

        champs = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{self.version}/data/ja_JP/champion.json",
            timeout=20,
        )
        champs.raise_for_status()
        data = champs.json().get("data", {})
        self.champions_ja = {
            v.get("id", k): v.get("name", v.get("id", k))
            for k, v in data.items()
            if isinstance(v, dict)
        }

    def item_line(self, item_id: int) -> Optional[str]:
        if item_id <= 0:
            return None
        self._ensure_items()
        item = self.items.get(str(item_id))
        if not item:
            return None
        icon_file = item.get("image", {}).get("full")
        icon_url = (
            f"https://ddragon.leagueoflegends.com/cdn/{self.version}/img/item/{icon_file}"
            if icon_file and self.version
            else None
        )
        if icon_url:
            return f"- {icon_url}"
        return None

    def item_icon_url(self, item_id: int) -> Optional[str]:
        if item_id <= 0:
            return None
        self._ensure_items()
        item = self.items.get(str(item_id))
        if not item:
            return None
        icon_file = item.get("image", {}).get("full")
        if icon_file and self.version:
            return f"https://ddragon.leagueoflegends.com/cdn/{self.version}/img/item/{icon_file}"
        return None

    def is_vision_item(self, item_id: int) -> bool:
        if item_id <= 0:
            return True
        self._ensure_items()
        item = self.items.get(str(item_id), {})
        tags = item.get("tags", []) if isinstance(item, dict) else []
        known_vision_items = {
            2055, 3340, 3363, 3364, 4643,
        }
        if item_id in known_vision_items:
            return True
        if isinstance(tags, list) and any(tag in {"Vision", "Trinket"} for tag in tags):
            return True
        return False

    def is_core_item(self, item_id: int) -> bool:
        if item_id <= 0:
            return False
        if self.is_vision_item(item_id):
            return False

        self._ensure_items()
        item = self.items.get(str(item_id))
        if not isinstance(item, dict):
            return False

        tags = item.get("tags", [])
        if isinstance(tags, list) and any(tag in {"Consumable", "Trinket", "Jungle"} for tag in tags):
            return False

        if item.get("into"):
            return False

        total_gold = int(item.get("gold", {}).get("total", 0))
        return total_gold >= 2200

    def champion_name_ja(self, champion_id: str) -> str:
        if not champion_id:
            return "不明"
        self._ensure_champions_ja()
        return self.champions_ja.get(champion_id, champion_id)

    def preload(self) -> None:
        self._ensure_items()
        self._ensure_champions_ja()

    def champion_icon_url(self, champion_id: str) -> Optional[str]:
        if not champion_id:
            return None
        if not self.version:
            self._ensure_items()
        if not self.version:
            return None
        return f"https://ddragon.leagueoflegends.com/cdn/{self.version}/img/champion/{champion_id}.png"


riot = RiotApiClient(RIOT_API_KEY, RIOT_REGION)
ddragon = DataDragonClient()
TRACKED_PLAYERS = merge_tracked_players(TRACKED_PLAYERS, load_tracked_players_from_file(TRACKED_PLAYERS_FILE))
TRACKING_ENABLED = load_tracking_enabled(TRACKING_STATE_FILE)
state: Dict[str, PlayerState] = {}
riot_auth_alert_active = False

intents = discord.Intents.none()
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
has_synced_commands = False


def extract_http_status(exc: Exception) -> Optional[int]:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return int(exc.response.status_code)
    return None


def is_riot_auth_error(exc: Exception) -> bool:
    status = extract_http_status(exc)
    return status in {401, 403}


async def notify_riot_auth_error_once(channel: discord.TextChannel, exc: Exception) -> None:
    global riot_auth_alert_active
    if riot_auth_alert_active:
        return

    riot_auth_alert_active = True
    status = extract_http_status(exc)
    status_text = str(status) if status is not None else "unknown"

    embed = discord.Embed(
        title="🚨 Riot APIキーの認証エラー",
        description="Riot APIへのアクセスに失敗しました。開発キーが失効した可能性があります。",
        color=discord.Color.red(),
    )
    embed.add_field(name="HTTPステータス", value=status_text, inline=True)
    embed.add_field(name="対処", value="Riot開発者ポータルでキーを再発行して .env を更新し、Botを再起動してください。", inline=False)
    await channel.send(embed=embed)


def format_duration(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    return f"{m}m {s}s"


def deeplol_region_from_platform(platform_region: str) -> str:
    # jp1 -> jp, kr -> kr のように末尾の数字だけ落として小文字化する。
    return "".join(ch for ch in platform_region.lower() if not ch.isdigit())


def build_result_message(player_name: str, summary: dict) -> str:
    result = "勝利" if summary["win"] else "敗北"
    champion_name_ja = ddragon.champion_name_ja(summary.get("champion_name", ""))
    lines = [
        f"【LoL試合終了】{player_name}: {result}",
        f"試合時間: {format_duration(summary['game_duration_seconds'])}",
        f"K/D/A: {summary['kills']}/{summary['deaths']}/{summary['assists']}",
        f"ダメージ: {summary['damage_to_champions']:,}",
        f"CS: {summary['cs']}",
        f"使用キャラ: {champion_name_ja}",
    ]

    item_lines = [ddragon.item_line(i) for i in summary.get("item_ids", [])]
    item_lines = [line for line in item_lines if line]
    if item_lines:
        lines.append("購入アイテム:")
        lines.extend(item_lines)

    streak = summary.get("streak")
    if streak and isinstance(streak.get("count"), int):
        lines.append(f"現在の流れ: {streak['type']} {streak['count']}")

    return "\n".join(lines)


def build_deeplol_url(summary: dict) -> Optional[str]:
    player_name = summary.get("player_name")
    tag_line = summary.get("player_tag_line")
    if not player_name or not tag_line:
        return None
    encoded_name = urllib.parse.quote(player_name, safe="")
    encoded_tag = urllib.parse.quote(tag_line, safe="")
    return f"https://www.deeplol.gg/summoner/{deeplol_region_from_platform(LOL_PLATFORM_REGION)}/{encoded_name}-{encoded_tag}"


def build_result_embed(player_name: str, summary: dict) -> discord.Embed:
    is_win = bool(summary.get("win", False))
    result = "勝利" if is_win else "敗北"
    result_emoji = "🟢" if is_win else "🔴"
    color = discord.Color.green() if is_win else discord.Color.red()
    champion_id = summary.get("champion_name", "")
    champion_name_ja = ddragon.champion_name_ja(champion_id)
    deeplol_url = build_deeplol_url(summary)

    embed = discord.Embed(
        title=f"{result_emoji} {player_name} - {result}",
        url=deeplol_url,
        color=color,
    )
    embed.add_field(name="⏱ 試合時間", value=format_duration(summary["game_duration_seconds"]), inline=True)
    embed.add_field(name="ゲームモード", value=queue_name(int(summary.get("queue_id", 0))), inline=True)
    embed.add_field(name="ロール", value=role_name(str(summary.get("role", ""))), inline=True)
    embed.add_field(
        name="⚔️ K/D/A",
        value=f"{summary['kills']}/{summary['deaths']}/{summary['assists']}",
        inline=True,
    )
    embed.add_field(name="キル関与率", value=f"{float(summary.get('kill_participation', 0.0)):.1f}%", inline=True)
    embed.add_field(name="CS", value=str(summary["cs"]), inline=True)
    embed.add_field(name="ダメージ", value=f"{summary['damage_to_champions']:,}", inline=True)

    streak = summary.get("streak")
    if streak and isinstance(streak.get("count"), int):
        if streak.get("type") == "連勝":
            streak_value = str(streak["count"])
        else:
            streak_value = f"0 (現在 連敗 {streak['count']})"
        embed.add_field(name="🔥 連勝数", value=streak_value, inline=True)

    champ_icon = ddragon.champion_icon_url(champion_id)
    if champ_icon:
        embed.set_thumbnail(url=champ_icon)

    embed.set_footer(text=f"Match ID: {summary.get('match_id', 'unknown')}")
    return embed


def build_result_view(summary: dict) -> Optional[discord.ui.View]:
    player_name = summary.get("player_name")
    tag_line = summary.get("player_tag_line")
    match_id = summary.get("match_id")
    if not player_name or not tag_line or not match_id:
        return None

    encoded_name = urllib.parse.quote(player_name, safe="")
    encoded_tag = urllib.parse.quote(tag_line, safe="")
    summoner_path = f"{encoded_name}-{encoded_tag}"

    deeplol_url = (
        f"https://www.deeplol.gg/summoner/{deeplol_region_from_platform(LOL_PLATFORM_REGION)}/{summoner_path}"
    )

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="DeepLOL", url=deeplol_url))
    return view


def build_matchup_file(summary: dict) -> Optional[discord.File]:
    my_champ = str(summary.get("champion_name") or "")
    enemy_champ = str(summary.get("opponent_champion_name") or "")
    my_url = ddragon.champion_icon_url(my_champ)
    enemy_url = ddragon.champion_icon_url(enemy_champ)
    if not my_url or not enemy_url:
        return None

    try:
        my_res = requests.get(my_url, timeout=10)
        my_res.raise_for_status()
        enemy_res = requests.get(enemy_url, timeout=10)
        enemy_res.raise_for_status()

        icon_size = 76
        my_img = Image.open(io.BytesIO(my_res.content)).convert("RGBA").resize((icon_size, icon_size), Image.Resampling.LANCZOS)
        enemy_img = Image.open(io.BytesIO(enemy_res.content)).convert("RGBA").resize((icon_size, icon_size), Image.Resampling.LANCZOS)

        canvas = Image.new("RGBA", (icon_size * 2 + 24, icon_size), (20, 24, 34, 255))
        canvas.paste(my_img, (0, 0), my_img)
        canvas.paste(enemy_img, (icon_size + 24, 0), enemy_img)

        # center separator for a versus-like look without text.
        for y in range(8, icon_size - 8):
            canvas.putpixel((icon_size + 11, y), (235, 235, 240, 220))
            canvas.putpixel((icon_size + 12, y), (235, 235, 240, 220))

        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(fp=buffer, filename="matchup.png")
    except Exception:
        return None


def build_item_strip_file(summary: dict) -> Optional[discord.File]:
    purchase_ids = [
        int(item_id)
        for item_id in summary.get("build_item_ids", [])
        if isinstance(item_id, int) and int(item_id) > 0
    ]
    core_ids: list[int] = []
    seen: set[int] = set()
    for item_id in purchase_ids:
        if not ddragon.is_core_item(item_id):
            continue
        if item_id in seen:
            continue
        seen.add(item_id)
        core_ids.append(item_id)
    core_ids = core_ids[:6]

    item_urls = [ddragon.item_icon_url(item_id) for item_id in core_ids]
    item_urls = [url for url in item_urls if url]
    if not item_urls:
        return None

    icons: list[Image.Image] = []
    icon_size = 44
    spacing = 4

    for url in item_urls:
        try:
            res = requests.get(url, timeout=10)
            res.raise_for_status()
            img = Image.open(io.BytesIO(res.content)).convert("RGBA")
            if img.size != (icon_size, icon_size):
                img = img.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            icons.append(img)
        except Exception:
            continue

    if not icons:
        return None

    width = len(icons) * icon_size + max(0, len(icons) - 1) * spacing
    height = icon_size
    strip = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    x = 0
    for icon in icons:
        strip.paste(icon, (x, 0), icon)
        x += icon_size + spacing

    buffer = io.BytesIO()
    strip.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(fp=buffer, filename="items.png")


async def init_baseline() -> None:
    for p in TRACKED_PLAYERS:
        latest_match_id = await asyncio.to_thread(riot.get_latest_match_id, p.puuid)
        state[p.puuid] = PlayerState(in_game=False, last_known_match_id=latest_match_id)


async def poll_and_notify(channel: discord.TextChannel) -> None:
    global riot_auth_alert_active
    if not TRACKING_ENABLED:
        return

    for p in TRACKED_PLAYERS:
        if not p.enabled:
            continue

        current = state.get(p.puuid, PlayerState(in_game=False, last_known_match_id=None))

        try:
            latest_match_id = await asyncio.to_thread(riot.get_latest_match_id, p.puuid)
            if riot_auth_alert_active:
                riot_auth_alert_active = False

            if latest_match_id and latest_match_id != current.last_known_match_id:
                summary = await asyncio.to_thread(riot.get_match_summary, latest_match_id, p.puuid)
                if summary:
                    build_item_ids = await asyncio.to_thread(
                        riot.get_item_purchase_order,
                        latest_match_id,
                        int(summary.get("participant_id", 0)),
                    )
                    summary["build_item_ids"] = build_item_ids

                    streak_result = await asyncio.to_thread(riot.get_result_streak, p.puuid)
                    if isinstance(streak_result, dict):
                        summary["streak"] = streak_result

                    player_name = summary.get("player_name") or p.name or p.puuid
                    embed = build_result_embed(player_name, summary)
                    view = build_result_view(summary)
                    item_file = await asyncio.to_thread(build_item_strip_file, summary)
                    matchup_file = await asyncio.to_thread(build_matchup_file, summary)

                    attachments: list[discord.File] = []
                    if matchup_file:
                        attachments.append(matchup_file)
                        embed.set_thumbnail(url="attachment://matchup.png")
                    if item_file:
                        attachments.append(item_file)
                        embed.set_image(url="attachment://items.png")

                    if view and attachments:
                        await channel.send(embed=embed, view=view, files=attachments)
                    elif view:
                        await channel.send(embed=embed, view=view)
                    elif attachments:
                        await channel.send(embed=embed, files=attachments)
                    else:
                        await channel.send(embed=embed)

                state[p.puuid] = PlayerState(in_game=False, last_known_match_id=latest_match_id)
                continue

            state[p.puuid] = PlayerState(in_game=False, last_known_match_id=current.last_known_match_id)
        except Exception as exc:  # pylint: disable=broad-except
            player_label = p.name or p.puuid
            print(f"Poll failed for {player_label}: {exc}")
            if is_riot_auth_error(exc):
                await notify_riot_auth_error_once(channel, exc)


async def send_preview(channel: discord.TextChannel) -> None:
    target_player = None
    if DEBUG_PREVIEW_PUUID:
        target_player = next((p for p in TRACKED_PLAYERS if p.puuid == DEBUG_PREVIEW_PUUID), None)
    if target_player is None:
        target_player = TRACKED_PLAYERS[0]

    summary = await asyncio.to_thread(
        riot.get_latest_match_summary_for_queues,
        target_player.puuid,
        TARGET_QUEUE_IDS,
        30,
    )
    if not summary:
        await channel.send("プレビュー対象の最新Normal/Ranked試合が見つかりませんでした。")
        return

    build_item_ids = await asyncio.to_thread(
        riot.get_item_purchase_order,
        str(summary.get("match_id", "")),
        int(summary.get("participant_id", 0)),
    )
    summary["build_item_ids"] = build_item_ids

    streak_result = await asyncio.to_thread(riot.get_result_streak, target_player.puuid)
    if isinstance(streak_result, dict):
        summary["streak"] = streak_result

    player_name = summary.get("player_name") or target_player.name or target_player.puuid
    embed = build_result_embed(player_name, summary)
    view = build_result_view(summary)
    item_file = await asyncio.to_thread(build_item_strip_file, summary)
    matchup_file = await asyncio.to_thread(build_matchup_file, summary)

    attachments: list[discord.File] = []
    if matchup_file:
        attachments.append(matchup_file)
        embed.set_thumbnail(url="attachment://matchup.png")
    if item_file:
        attachments.append(item_file)
        embed.set_image(url="attachment://items.png")

    content = "デバッグプレビュー: 最新Normal/Ranked試合"
    if view and attachments:
        await channel.send(content=content, embed=embed, view=view, files=attachments)
    elif view:
        await channel.send(content=content, embed=embed, view=view)
    elif attachments:
        await channel.send(content=content, embed=embed, files=attachments)
    else:
        await channel.send(content=content, embed=embed)


@tree.command(name="track_add", description="Riot ID(名前#タグ)を監視対象に追加")
@app_commands.describe(riot_id="例: 藤Uの自由#dlpk")
async def add_tracked_player(interaction: discord.Interaction, riot_id: str) -> None:
    riot_id = riot_id.strip()
    if "#" not in riot_id:
        await interaction.response.send_message("形式が違います。`名前#タグ` で入力してください。", ephemeral=True)
        return

    game_name, tag_line = riot_id.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        await interaction.response.send_message("形式が違います。`名前#タグ` で入力してください。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        puuid = await asyncio.to_thread(riot.get_puuid_by_riot_id, game_name, tag_line)
        if not puuid:
            await interaction.followup.send("Riot IDが見つかりませんでした。全角`＃`ではなく半角`#`で入力してください。", ephemeral=True)
            return

        exists = next((p for p in TRACKED_PLAYERS if p.puuid == puuid), None)
        if exists:
            await interaction.followup.send("そのプレイヤーはすでに監視対象です。", ephemeral=True)
            return

        new_player = TrackedPlayer(puuid=puuid, name=game_name, enabled=True)
        TRACKED_PLAYERS.append(new_player)

        latest_match_id = await asyncio.to_thread(riot.get_latest_match_id, puuid)
        state[puuid] = PlayerState(in_game=False, last_known_match_id=latest_match_id)
        await asyncio.to_thread(save_tracked_players_to_file, TRACKED_PLAYERS_FILE, TRACKED_PLAYERS)

        embed = discord.Embed(
            title="🛰️ 新しい監視対象をロックオン",
            description=f"**{game_name}#{tag_line}** を追跡リストに追加しました。",
            color=discord.Color.green(),
        )
        embed.add_field(name="現在の監視人数", value=f"{len(TRACKED_PLAYERS)}人", inline=True)
        embed.set_footer(text="静かに、でも確実に見守ります。")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:  # pylint: disable=broad-except
        await interaction.followup.send(f"追加に失敗しました: {exc}", ephemeral=True)


@tree.command(name="help", description="コマンド一覧と使い方を表示")
async def show_help(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="📚 どらぴこbot ヘルプ",
        description="よく使うコマンドをまとめました。",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="/track_add riot_id",
        value="監視対象を追加します。\n例: /track_add riot_id: 藤Uの自由#dlpk",
        inline=False,
    )
    embed.add_field(
        name="/track_list",
        value="現在の監視対象一覧とON/OFF状態を表示します。",
        inline=False,
    )
    embed.add_field(
        name="/track_toggle",
        value="プルダウンでプレイヤーごとの監視ON/OFFを切り替えます。",
        inline=False,
    )
    embed.add_field(
        name="/track_remove",
        value="プルダウンで監視対象を削除します。",
        inline=False,
    )
    embed.add_field(
        name="/tracking_toggle",
        value="トラッキング機能全体のON/OFFを切り替えます。",
        inline=False,
    )
    embed.add_field(
        name="/help",
        value="このヘルプを表示します。",
        inline=False,
    )
    embed.set_footer(text="困ったらまず /help をどうぞ。")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="track_list", description="現在の監視対象プレイヤー一覧を表示")
async def list_tracked_players(interaction: discord.Interaction) -> None:
    if not TRACKED_PLAYERS:
        embed = discord.Embed(
            title="🕵️ ストーキング中... 0人",
            description="今日は静かな夜。まだ誰も追跡していません。",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    lines: list[str] = []
    for idx, player in enumerate(TRACKED_PLAYERS, start=1):
        display_name = player.name if player.name else "(name未設定)"
        status_icon = "🟢" if player.enabled else "⚫"
        lines.append(f"{idx}. {status_icon} {display_name}")

    embed = discord.Embed(
        title=f"🕵️ ストーキング中... {len(TRACKED_PLAYERS)}人",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="🟢: 監視中 / ⚫: 休止中")
    await interaction.response.send_message(embed=embed, ephemeral=True)


class TrackToggleSelect(discord.ui.Select):
    def __init__(self, players: list[TrackedPlayer]):
        options: list[discord.SelectOption] = []
        for idx, player in enumerate(players[:25], start=1):
            display_name = player.name if player.name else "(name未設定)"
            status = "ON" if player.enabled else "OFF"
            options.append(
                discord.SelectOption(
                    label=f"{idx}. [{status}] {display_name}"[:100],
                    value=player.puuid,
                    description=f"現在: {'監視中' if player.enabled else '休止中'}",
                )
            )

        super().__init__(
            placeholder="切り替えたいプレイヤーを選択",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_puuid = self.values[0]
        target_player = next((p for p in TRACKED_PLAYERS if p.puuid == selected_puuid), None)
        if not target_player:
            await interaction.response.send_message("対象プレイヤーが見つかりません。", ephemeral=True)
            return

        target_player.enabled = not target_player.enabled
        if target_player.enabled:
            latest_match_id = await asyncio.to_thread(riot.get_latest_match_id, target_player.puuid)
            state[target_player.puuid] = PlayerState(in_game=False, last_known_match_id=latest_match_id)
            status_text = "ON"
        else:
            status_text = "OFF"

        await asyncio.to_thread(save_tracked_players_to_file, TRACKED_PLAYERS_FILE, TRACKED_PLAYERS)
        display_name = target_player.name if target_player.name else "(name未設定)"
        await interaction.response.send_message(
            f"{display_name} のトラッキングを {status_text} にしました。",
            ephemeral=True,
        )


class TrackToggleView(discord.ui.View):
    def __init__(self, requester_id: int, players: list[TrackedPlayer]):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.add_item(TrackToggleSelect(players))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("この操作はコマンド実行者のみ可能です。", ephemeral=True)
            return False
        return True


class TrackRemoveSelect(discord.ui.Select):
    def __init__(self, players: list[TrackedPlayer]):
        options: list[discord.SelectOption] = []
        for idx, player in enumerate(players[:25], start=1):
            display_name = player.name if player.name else "(name未設定)"
            status = "ON" if player.enabled else "OFF"
            options.append(
                discord.SelectOption(
                    label=f"{idx}. [{status}] {display_name}"[:100],
                    value=player.puuid,
                    description=f"現在: {'監視中' if player.enabled else '休止中'}",
                )
            )

        super().__init__(
            placeholder="削除したいプレイヤーを選択",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_puuid = self.values[0]
        target_idx = next((i for i, p in enumerate(TRACKED_PLAYERS) if p.puuid == selected_puuid), None)
        if target_idx is None:
            await interaction.response.send_message("対象プレイヤーが見つかりません。", ephemeral=True)
            return

        target_player = TRACKED_PLAYERS.pop(target_idx)
        state.pop(target_player.puuid, None)
        await asyncio.to_thread(save_tracked_players_to_file, TRACKED_PLAYERS_FILE, TRACKED_PLAYERS)

        display_name = target_player.name if target_player.name else "(name未設定)"
        embed = discord.Embed(
            title="🧹 監視リストをお掃除しました",
            description=f"**{display_name}** を監視リストから外しました。\nしばらく自由の身です。",
            color=discord.Color.orange(),
        )
        embed.add_field(name="残りの監視人数", value=f"{len(TRACKED_PLAYERS)}人", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TrackRemoveView(discord.ui.View):
    def __init__(self, requester_id: int, players: list[TrackedPlayer]):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.add_item(TrackRemoveSelect(players))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("この操作はコマンド実行者のみ可能です。", ephemeral=True)
            return False
        return True


@tree.command(name="track_toggle", description="プルダウンで監視ON/OFFを切り替え")
async def toggle_tracked_player(interaction: discord.Interaction) -> None:
    if not TRACKED_PLAYERS:
        embed = discord.Embed(
            title="🎛️ トラッキング切り替え",
            description="現在の監視対象は0人です。",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if len(TRACKED_PLAYERS) > 25:
        embed = discord.Embed(
            title="🎛️ トラッキング切り替え",
            description=f"監視対象が {len(TRACKED_PLAYERS)} 人いるため、先頭25人のみ表示します。",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    view = TrackToggleView(interaction.user.id, TRACKED_PLAYERS)
    lines = []
    for idx, player in enumerate(TRACKED_PLAYERS, start=1):
        display_name = player.name if player.name else "(name未設定)"
        status_icon = "🟢" if player.enabled else "⚫"
        lines.append(f"{idx}. {status_icon} {display_name}")

    embed = discord.Embed(
        title=f"🎛️ トラッキング切り替え ({len(TRACKED_PLAYERS)}人)",
        description="切り替えるプレイヤーを下のメニューから選択してください。",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="現在の状態", value="\n".join(lines), inline=False)
    embed.set_footer(text="🟢: 監視中 / ⚫: 休止中")
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@tree.command(name="track_remove", description="プルダウンで監視リストから完全削除")
async def remove_tracked_player(interaction: discord.Interaction) -> None:
    if not TRACKED_PLAYERS:
        embed = discord.Embed(
            title="🧹 監視リスト整理",
            description="現在の監視対象は0人です。消す相手がいません。",
            color=discord.Color.dark_grey(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if len(TRACKED_PLAYERS) > 25:
        embed = discord.Embed(
            title="🧹 監視リスト整理",
            description=f"監視対象が {len(TRACKED_PLAYERS)} 人いるため、先頭25人のみ表示します。",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    view = TrackRemoveView(interaction.user.id, TRACKED_PLAYERS)
    lines = []
    for idx, player in enumerate(TRACKED_PLAYERS, start=1):
        display_name = player.name if player.name else "(name未設定)"
        status_icon = "🟢" if player.enabled else "⚫"
        lines.append(f"{idx}. {status_icon} {display_name}")

    embed = discord.Embed(
        title=f"🧹 監視リスト整理 ({len(TRACKED_PLAYERS)}人)",
        description="リストから外すプレイヤーを下のメニューで選んでください。",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="現在のリスト", value="\n".join(lines), inline=False)
    embed.set_footer(text="🟢: 監視中 / ⚫: 休止中")
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@tree.command(name="tracking_toggle", description="トラッキング機能全体のON/OFF切り替え")
async def toggle_tracking_feature(interaction: discord.Interaction) -> None:
    global TRACKING_ENABLED
    TRACKING_ENABLED = not TRACKING_ENABLED
    await asyncio.to_thread(save_tracking_enabled, TRACKING_STATE_FILE, TRACKING_ENABLED)

    status_text = "ON" if TRACKING_ENABLED else "OFF"
    status_icon = "🟢" if TRACKING_ENABLED else "⚫"
    color = discord.Color.green() if TRACKING_ENABLED else discord.Color.red()
    embed = discord.Embed(
        title="🧭 全体トラッキング設定",
        description=f"トラッキング機能を **{status_text}** にしました。",
        color=color,
    )
    embed.add_field(name="現在の状態", value=f"{status_icon} {status_text}", inline=True)
    embed.add_field(name="登録人数", value=f"{len(TRACKED_PLAYERS)}人", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def watch_loop(channel: discord.TextChannel) -> None:
    while True:
        await poll_and_notify(channel)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def cleanup_global_command_duplicates() -> None:
    try:
        global_commands = await tree.fetch_commands()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Could not fetch global commands for cleanup: {exc}")
        return

    target_names = {"id", "track_add"}
    duplicate_candidates = [cmd for cmd in global_commands if cmd.name in target_names]
    if not duplicate_candidates:
        return

    for cmd in duplicate_candidates:
        try:
            await cmd.delete()
            print(f"Deleted global /{cmd.name} command (id={cmd.id})")
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Failed to delete global /{cmd.name} command (id={cmd.id}): {exc}")


@client.event
async def on_ready() -> None:
    global has_synced_commands
    print(f"Logged in as {client.user}")

    target = client.get_channel(DISCORD_CHANNEL_ID)
    if target is None:
        fetched = await client.fetch_channel(DISCORD_CHANNEL_ID)
        target = fetched

    if not isinstance(target, discord.TextChannel):
        raise RuntimeError("DISCORD_CHANNEL_ID must be a text channel ID")

    if not has_synced_commands:
        # Global sync can take time to appear; guild sync makes commands available quickly.
        try:
            await cleanup_global_command_duplicates()
            tree.copy_global_to(guild=target.guild)
            await tree.sync(guild=target.guild)
            print(f"Slash commands synced for guild: {target.guild.id}")
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Guild sync failed, fallback to global sync: {exc}")
            await tree.sync()
            print("Slash commands synced globally")
        has_synced_commands = True

    await asyncio.to_thread(ddragon.preload)

    if PREVIEW_MODE:
        await send_preview(target)
        await client.close()
        return

    try:
        await init_baseline()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Baseline init failed: {exc}")
        if is_riot_auth_error(exc):
            await notify_riot_auth_error_once(target, exc)
    await target.send("どらぴこbot起動中!")
    asyncio.create_task(watch_loop(target))


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
