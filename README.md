# LoL Game Watcher Discord Bot

進行中のLoLゲームが終わったタイミングを検知し、指定Discordチャンネルに試合結果を投稿するBotです。

## できること
- 監視対象プレイヤーの進行中ゲーム状態を定期チェック
- 「ゲーム中 -> 非ゲーム中」遷移を検出
- 最新試合の結果（勝敗、チャンピオン、KDA、試合時間）をDiscordへ投稿

## セットアップ（Python）
1. 仮想環境を作成（任意ですが推奨）

   python3 -m venv .venv
   source .venv/bin/activate

2. 依存関係をインストール

   pip install -r requirements.txt

3. 環境変数を作成

   .env.example をコピーして .env を作成し、値を埋めます。

4. Botを実行

   python3 bot.py

## セットアップ（TypeScript）
既存のTypeScript版を使う場合は以下です。

1. npm install
2. .env.example をコピーして .env を作成
3. npm run dev

## 環境変数
- DISCORD_TOKEN: Discord Botトークン
- DISCORD_CHANNEL_ID: 投稿先テキストチャンネルID
- RIOT_API_KEY: Riot Developer APIキー
- LOL_PLATFORM_REGION: 例 `jp1`
- RIOT_REGION: 例 `asia`
- POLL_INTERVAL_SECONDS: 監視間隔（秒）
- TRACKED_PLAYERS_JSON: 監視対象配列(JSON)。`name` は任意で、未指定ならLoLの名前を自動取得

例:
[
   {"puuid":"xxxxxxxx"},
   {"name":"任意の表示名","puuid":"yyyyyyyy"}
]

## 注意
- Riot APIのレート制限に注意してください。
- 初回起動時はベースラインを記録し、過去試合は投稿しません。
- この実装は最小構成です。必要に応じて再試行、永続化、詳細なキュー名変換を追加してください。
