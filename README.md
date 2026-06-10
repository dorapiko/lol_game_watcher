# Dorapiko Match Watcher

Dorapiko Match Watcher is a Discord bot that detects when a tracked League of Legends match ends and posts a concise post-game summary to a designated Discord channel.

This product is not endorsed by Riot Games.

## Product Overview

- Tracks registered players by Riot ID and PUUID.
- Detects match-end events by polling recent matches.
- Posts summary embeds to Discord, including result, champion, role, KDA, damage, CS, duration, and build progression.
- Supports runtime management commands to add, list, toggle, and remove tracked players.

## Riot APIs Used

- Riot Account-V1
  - Resolve Riot ID to PUUID.
- Match-V5
  - Recent match IDs for tracked players.
  - Match detail data for post-game summary.
  - Match timeline for item purchase order.

Only documented Riot endpoints are used.

## Policy and Compliance Notes

- No undocumented Riot endpoints are used.
- No third-party site scraping is used.
- No alternative rank systems (MMR or ELO) are provided.
- No player reporting, shaming, or public judgment features are provided.
- No official Riot logos are used.
- No claim of Riot partnership or Riot approval is made.

## Security Practices

- Secrets are loaded from environment variables.
- Do not commit .env or any real token/key files.
- Rotate Discord and Riot keys immediately if exposure is suspected.
- Use separate keys per environment and per project.

## Setup (Python)

1. Create and activate a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Create .env from .env.example and set values.

4. Run the bot.

```bash
python3 bot.py
```

## Environment Variables

- DISCORD_TOKEN: Discord bot token.
- DISCORD_CHANNEL_ID: Target text channel ID.
- RIOT_API_KEY: Riot API key.
- LOL_PLATFORM_REGION: Platform region, for example jp1.
- RIOT_REGION: Regional routing, for example asia.
- POLL_INTERVAL_SECONDS: Polling interval in seconds.
- TRACKED_PLAYERS_JSON: Initial tracked players array.

Example TRACKED_PLAYERS_JSON:

```json
[
  {"puuid": "xxxxxxxx"},
  {"name": "display-name", "puuid": "yyyyyyyy"}
]
```

## Public Release Checklist

- Confirm .env is not tracked by git.
- Confirm no live token or key is in committed files.
- Confirm Product URL is publicly reachable.
- Confirm project description matches implemented behavior.
- Confirm compliance items in this README are still true.

## Contact

- Project owner: Update this section before submitting for production review.
