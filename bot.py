import asyncio
import io
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Dict, Optional

import discord
import requests
from dotenv import load_dotenv
from PIL import Image


load_dotenv(override=True)


@dataclass
class TrackedPlayer:
    puuid: str
    name: Optional[str] = None


@dataclass
class PlayerState:
    in_game: bool
    last_known_match_id: Optional[str]


@dataclass
class OpggScore:
    score: Optional[float] = None
    badge: Optional[str] = None


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
        result.append(TrackedPlayer(puuid=puuid, name=name))
    return result


DISCORD_TOKEN = require_env("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(require_env("DISCORD_CHANNEL_ID"))
RIOT_API_KEY = require_env("RIOT_API_KEY")
LOL_PLATFORM_REGION = os.getenv("LOL_PLATFORM_REGION", "jp1")
RIOT_REGION = os.getenv("RIOT_REGION", "asia")
POLL_INTERVAL_SECONDS = max(15, int(os.getenv("POLL_INTERVAL_SECONDS", "60")))
TRACKED_PLAYERS = parse_tracked_players(require_env("TRACKED_PLAYERS_JSON"))
PREVIEW_MODE = "--preview" in sys.argv
DEBUG_PREVIEW_PUUID = os.getenv("DEBUG_PREVIEW_PUUID")

TARGET_QUEUE_IDS = {400, 420, 430, 440, 490}
RANKED_QUEUE_IDS = {420, 440}
NORMAL_QUEUE_IDS = {400, 430, 490}


def queue_name(queue_id: int) -> str:
    if queue_id in RANKED_QUEUE_IDS:
        return "ランク"
    if queue_id in NORMAL_QUEUE_IDS:
        return "ノーマル"
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


class OpggClient:
    def __init__(self, platform_region: str):
        self.platform_region = platform_region
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def _region(self) -> str:
        return re.sub(r"\d", "", self.platform_region).lower()

    def fetch_score(
        self,
        player_name: Optional[str],
        player_tag_line: Optional[str],
        match_id: str,
    ) -> Optional[OpggScore]:
        if not player_name or not player_tag_line:
            return None

        encoded_name = urllib.parse.quote(player_name, safe="")
        url = f"https://op.gg/lol/summoners/{self._region()}/{encoded_name}-{player_tag_line}/matches/{match_id}"

        try:
            res = self.session.get(url, timeout=12)
            if not res.ok:
                return None
            text = res.text

            score_badge = re.search(r"\b([0-9]{1,2}(?:\.[0-9])?)\s+(MVP|ACE)\b", text)
            if score_badge:
                return OpggScore(score=float(score_badge.group(1)), badge=score_badge.group(2))

            badge_only = re.search(r"\b(MVP|ACE|Unlucky)\b", text)
            if badge_only:
                return OpggScore(score=None, badge=badge_only.group(1))
        except Exception:
            return None

        return None


riot = RiotApiClient(RIOT_API_KEY, RIOT_REGION)
opgg = OpggClient(LOL_PLATFORM_REGION)
ddragon = DataDragonClient()
state: Dict[str, PlayerState] = {}

intents = discord.Intents.none()
intents.guilds = True
client = discord.Client(intents=intents)


def format_duration(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    return f"{m}m {s}s"


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

    opgg_score: Optional[OpggScore] = summary.get("opgg_score")
    if opgg_score:
        if opgg_score.score is not None and opgg_score.badge:
            lines.append(f"OP.GG OP Score: {opgg_score.score:.1f} ({opgg_score.badge})")
        elif opgg_score.score is not None:
            lines.append(f"OP.GG OP Score: {opgg_score.score:.1f}")
        elif opgg_score.badge:
            lines.append(f"OP.GG評価: {opgg_score.badge}")

    return "\n".join(lines)


def build_deeplol_url(summary: dict) -> Optional[str]:
    player_name = summary.get("player_name")
    tag_line = summary.get("player_tag_line")
    if not player_name or not tag_line:
        return None
    encoded_name = urllib.parse.quote(player_name, safe="")
    encoded_tag = urllib.parse.quote(tag_line, safe="")
    return f"https://www.deeplol.gg/summoner/{opgg._region()}/{encoded_name}-{encoded_tag}"


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
    embed.add_field(name="🎮 ゲームモード", value=queue_name(int(summary.get("queue_id", 0))), inline=True)
    embed.add_field(name="🧭 ロール", value=role_name(str(summary.get("role", ""))), inline=True)
    embed.add_field(
        name="⚔️ K/D/A",
        value=f"{summary['kills']}/{summary['deaths']}/{summary['assists']}",
        inline=True,
    )
    embed.add_field(name="🤝 キル関与率", value=f"{float(summary.get('kill_participation', 0.0)):.1f}%", inline=True)
    embed.add_field(name="🌾 CS", value=str(summary["cs"]), inline=True)
    embed.add_field(name="💥 ダメージ", value=f"{summary['damage_to_champions']:,}", inline=True)

    streak = summary.get("streak")
    if streak and isinstance(streak.get("count"), int):
        if streak.get("type") == "連勝":
            streak_value = str(streak["count"])
        else:
            streak_value = f"0 (現在 連敗 {streak['count']})"
        embed.add_field(name="🔥 連勝数", value=streak_value, inline=True)

    opgg_score: Optional[OpggScore] = summary.get("opgg_score")
    if opgg_score:
        if opgg_score.score is not None and opgg_score.badge:
            embed.add_field(name="🏅 OP.GG OP Score", value=f"{opgg_score.score:.1f} ({opgg_score.badge})", inline=True)
        elif opgg_score.score is not None:
            embed.add_field(name="🏅 OP.GG OP Score", value=f"{opgg_score.score:.1f}", inline=True)
        elif opgg_score.badge:
            embed.add_field(name="⭐ OP.GG評価", value=opgg_score.badge, inline=True)

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

    opgg_match_url = f"https://op.gg/lol/summoners/{opgg._region()}/{summoner_path}/matches/{match_id}"
    opgg_profile_url = f"https://op.gg/lol/summoners/{opgg._region()}/{summoner_path}"
    deeplol_url = f"https://www.deeplol.gg/summoner/{opgg._region()}/{summoner_path}"

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="OP.GG試合詳細", url=opgg_match_url))
    view.add_item(discord.ui.Button(label="OP.GGプロフィール", url=opgg_profile_url))
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
    for p in TRACKED_PLAYERS:
        current = state.get(p.puuid, PlayerState(in_game=False, last_known_match_id=None))

        try:
            latest_match_id = await asyncio.to_thread(riot.get_latest_match_id, p.puuid)

            if latest_match_id and latest_match_id != current.last_known_match_id:
                summary = await asyncio.to_thread(riot.get_match_summary, latest_match_id, p.puuid)
                if summary:
                    build_item_ids = await asyncio.to_thread(
                        riot.get_item_purchase_order,
                        latest_match_id,
                        int(summary.get("participant_id", 0)),
                    )
                    summary["build_item_ids"] = build_item_ids

                    streak_result, opgg_result = await asyncio.gather(
                        asyncio.to_thread(riot.get_result_streak, p.puuid),
                        asyncio.to_thread(
                            opgg.fetch_score,
                            summary.get("player_name"),
                            summary.get("player_tag_line"),
                            latest_match_id,
                        ),
                        return_exceptions=True,
                    )
                    if isinstance(streak_result, dict):
                        summary["streak"] = streak_result
                    if isinstance(opgg_result, OpggScore):
                        summary["opgg_score"] = opgg_result

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

    streak_result, opgg_result = await asyncio.gather(
        asyncio.to_thread(riot.get_result_streak, target_player.puuid),
        asyncio.to_thread(
            opgg.fetch_score,
            summary.get("player_name"),
            summary.get("player_tag_line"),
            summary.get("match_id", ""),
        ),
        return_exceptions=True,
    )
    if isinstance(streak_result, dict):
        summary["streak"] = streak_result
    if isinstance(opgg_result, OpggScore):
        summary["opgg_score"] = opgg_result

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


async def watch_loop(channel: discord.TextChannel) -> None:
    while True:
        await poll_and_notify(channel)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user}")

    target = client.get_channel(DISCORD_CHANNEL_ID)
    if target is None:
        fetched = await client.fetch_channel(DISCORD_CHANNEL_ID)
        target = fetched

    if not isinstance(target, discord.TextChannel):
        raise RuntimeError("DISCORD_CHANNEL_ID must be a text channel ID")

    await asyncio.to_thread(ddragon.preload)

    if PREVIEW_MODE:
        await send_preview(target)
        await client.close()
        return

    await init_baseline()
    await target.send("LoL監視Bot(Python)を起動しました。試合終了を監視します。")
    asyncio.create_task(watch_loop(target))


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
