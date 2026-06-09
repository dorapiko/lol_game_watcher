import axios from "axios";

export type ActiveGameState = {
  inGame: boolean;
  gameStartTime?: number;
};

export type MatchSummary = {
  matchId: string;
  queueId: number;
  championName: string;
  kills: number;
  deaths: number;
  assists: number;
  win: boolean;
  gameDurationSeconds: number;
};

type SummonerByPuuidResponse = {
  id: string;
};

type ActiveGameResponse = {
  gameStartTime: number;
};

type MatchResponse = {
  info: {
    queueId: number;
    gameDuration: number;
    participants: Array<{
      puuid: string;
      championName: string;
      kills: number;
      deaths: number;
      assists: number;
      win: boolean;
    }>;
  };
};

export class RiotApiClient {
  constructor(
    private readonly apiKey: string,
    private readonly platformRegion: string,
    private readonly regionalRoute: string
  ) {}

  private get headers() {
    return {
      "X-Riot-Token": this.apiKey
    };
  }

  async getActiveGameStateByPuuid(puuid: string): Promise<ActiveGameState> {
    const summonerUrl = `https://${this.platformRegion}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/${puuid}`;

    const summonerRes = await axios.get<SummonerByPuuidResponse>(summonerUrl, {
      headers: this.headers
    });

    const encryptedSummonerId = summonerRes.data.id;
    const spectatorUrl = `https://${this.platformRegion}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/${encryptedSummonerId}`;

    try {
      const gameRes = await axios.get<ActiveGameResponse>(spectatorUrl, {
        headers: this.headers
      });

      return {
        inGame: true,
        gameStartTime: gameRes.data.gameStartTime
      };
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 404) {
        return { inGame: false };
      }
      throw error;
    }
  }

  async getLatestMatchId(puuid: string): Promise<string | null> {
    const url = `https://${this.regionalRoute}.api.riotgames.com/lol/match/v5/matches/by-puuid/${puuid}/ids?start=0&count=1`;

    const res = await axios.get<string[]>(url, {
      headers: this.headers
    });

    if (!res.data.length) {
      return null;
    }

    return res.data[0];
  }

  async getMatchSummary(matchId: string, puuid: string): Promise<MatchSummary | null> {
    const url = `https://${this.regionalRoute}.api.riotgames.com/lol/match/v5/matches/${matchId}`;

    const res = await axios.get<MatchResponse>(url, {
      headers: this.headers
    });

    const participant = res.data.info.participants.find((p) => p.puuid === puuid);
    if (!participant) {
      return null;
    }

    return {
      matchId,
      queueId: res.data.info.queueId,
      championName: participant.championName,
      kills: participant.kills,
      deaths: participant.deaths,
      assists: participant.assists,
      win: participant.win,
      gameDurationSeconds: res.data.info.gameDuration
    };
  }
}
