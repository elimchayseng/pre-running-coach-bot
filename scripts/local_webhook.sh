#!/usr/bin/env bash
# Local webhook testing: starts Flask dev server + ngrok tunnel
# Prerequisites: pip install -r requirements.txt, ngrok installed and authed
set -euo pipefail

PORT="${PORT:-8080}"

echo "Starting Flask dev server on port $PORT..."
FLASK_APP=app.py flask run --port "$PORT" &
FLASK_PID=$!

cleanup() {
    echo "Shutting down..."
    kill "$FLASK_PID" 2>/dev/null || true
    kill "$NGROK_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting ngrok tunnel..."
ngrok http "$PORT" --log=stdout > /dev/null &
NGROK_PID=$!

sleep 3

NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])")
echo "Ngrok URL: $NGROK_URL"

echo "Setting Telegram webhook to $NGROK_URL/webhook ..."
python3 -c "
import os, asyncio
from telegram import Bot
bot = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'])
asyncio.run(bot.set_webhook(url='$NGROK_URL/webhook'))
print('Webhook set!')
"

echo "Ready! Send a message to your bot on Telegram."
echo "Press Ctrl+C to stop."
wait
