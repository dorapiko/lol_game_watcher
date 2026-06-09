import dotenv from "dotenv";

dotenv.config();

export type TrackedPlayer = {
  name: string;
  puuid: string;
};

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value || value.trim() === "") {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function parseTrackedPlayers(raw: string): TrackedPlayer[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("TRACKED_PLAYERS_JSON must be valid JSON");
  }

  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error("TRACKED_PLAYERS_JSON must be a non-empty array");
  }

  return parsed.map((entry, index) => {
    if (
      typeof entry !== "object" ||
      entry === null ||
      typeof (entry as { name?: unknown }).name !== "string" ||
      typeof (entry as { puuid?: unknown }).puuid !== "string"
    ) {
      throw new Error(`Invalid TRACKED_PLAYERS_JSON item at index ${index}`);
    }

    return {
      name: (entry as { name: string }).name,
      puuid: (entry as { puuid: string }).puuid
    };
  });
}

export const config = {
  discordToken: requireEnv("DISCORD_TOKEN"),
  discordChannelId: requireEnv("DISCORD_CHANNEL_ID"),
  riotApiKey: requireEnv("RIOT_API_KEY"),
  lolPlatformRegion: process.env.LOL_PLATFORM_REGION || "jp1",
  riotRegion: process.env.RIOT_REGION || "asia",
  pollIntervalSeconds: Number(process.env.POLL_INTERVAL_SECONDS || "60"),
  trackedPlayers: parseTrackedPlayers(requireEnv("TRACKED_PLAYERS_JSON"))
};
