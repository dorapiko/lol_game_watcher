# LoL Game Watcher Discord Bot

進行中のLoLゲームが終わったタイミングを検知し、指定Discordチャンネルに試合結果を投稿するBotです。

## できること
- 監視対象プレイヤーの進行中ゲーム状態を定期チェック
- 「ゲーム中 -> 非ゲーム中」遷移を検出
- 最新試合の結果（勝敗、チャンピオン、KDA、試合時間）をDiscordへ投稿

## セットアップ
1. 依存関係をインストール

   npm install

2. 環境変数を作成

   .env.example をコピーして .env を作成し、値を埋めます。

3. Botを実行

   npm run dev

## 環境変数
- DISCORD_TOKEN: Discord Botトークン
- DISCORD_CHANNEL_ID: 投稿先テキストチャンネルID
- RIOT_API_KEY: Riot Developer APIキー
- LOL_PLATFORM_REGION: 例 `jp1`
- RIOT_REGION: 例 `asia`
- POLL_INTERVAL_SECONDS: 監視間隔（秒）
- TRACKED_PLAYERS_JSON: 監視対象配列(JSON)

例:
[
  {"name":"friend1","puuid":"xxxxxxxx"}
]

## 注意
- Riot APIのレート制限に注意してください。
- 初回起動時はベースラインを記録し、過去試合は投稿しません。
- この実装は最小構成です。必要に応じて再試行、永続化、詳細なキュー名変換を追加してください。
