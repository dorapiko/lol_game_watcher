import { Client, GatewayIntentBits, TextChannel } from "discord.js";
import { config } from "./config";
import { RiotApiClient } from "./riot";

type PlayerState = {
  inGame: boolean;
  lastKnownMatchId: string | null;
  activeGameStartTime?: number;
};

const riot = new RiotApiClient(
  config.riotApiKey,
  config.lolPlatformRegion,
  config.riotRegion
);

const state = new Map<string, PlayerState>();

const client = new Client({
  intents: [GatewayIntentBits.Guilds]
});

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

function buildResultMessage(playerName: string, summary: {
  matchId: string;
  queueId: number;
  championName: string;
  kills: number;
  deaths: number;
  assists: number;
  win: boolean;
  gameDurationSeconds: number;
}): string {
  const result = summary.win ? "勝利" : "敗北";
  return [
    `【LoL試合結果】${playerName}`,
    `結果: ${result}`,
    `チャンピオン: ${summary.championName}`,
    `KDA: ${summary.kills}/${summary.deaths}/${summary.assists}`,
    `キューID: ${summary.queueId}`,
    `試合時間: ${formatDuration(summary.gameDurationSeconds)}`,
    `Match ID: ${summary.matchId}`
  ].join("\n");
}

async function initBaseline() {
  for (const p of config.trackedPlayers) {
    const latestMatchId = await riot.getLatestMatchId(p.puuid);
    state.set(p.puuid, {
      inGame: false,
      lastKnownMatchId: latestMatchId
    });
  }
}

async function pollAndNotify(channel: TextChannel) {
  for (const p of config.trackedPlayers) {
    const current = state.get(p.puuid) || {
      inGame: false,
      lastKnownMatchId: null
    };

    try {
      const active = await riot.getActiveGameStateByPuuid(p.puuid);

      if (!current.inGame && active.inGame) {
        state.set(p.puuid, {
          ...current,
          inGame: true,
          activeGameStartTime: active.gameStartTime
        });
        continue;
      }

      if (current.inGame && !active.inGame) {
        const latestMatchId = await riot.getLatestMatchId(p.puuid);

        if (latestMatchId && latestMatchId !== current.lastKnownMatchId) {
          const summary = await riot.getMatchSummary(latestMatchId, p.puuid);
          if (summary) {
            await channel.send(buildResultMessage(p.name, summary));
          }
        }

        state.set(p.puuid, {
          inGame: false,
          lastKnownMatchId: latestMatchId,
          activeGameStartTime: undefined
        });
        continue;
      }

      state.set(p.puuid, {
        ...current,
        inGame: active.inGame,
        activeGameStartTime: active.gameStartTime
      });
    } catch (error) {
      console.error(`Poll failed for ${p.name}:`, error);
    }
  }
}

client.once("ready", async () => {
  console.log(`Logged in as ${client.user?.tag}`);

  const target = await client.channels.fetch(config.discordChannelId);
  if (!target || !(target instanceof TextChannel)) {
    throw new Error("DISCORD_CHANNEL_ID must be a text channel ID");
  }

  await initBaseline();
  await target.send("LoL監視Botを起動しました。試合終了を監視します。");

  const intervalMs = Math.max(15, config.pollIntervalSeconds) * 1000;
  setInterval(() => {
    void pollAndNotify(target);
  }, intervalMs);
});

client.login(config.discordToken).catch((error) => {
  console.error("Discord login failed:", error);
  process.exit(1);
});
