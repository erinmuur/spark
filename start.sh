#!/bin/sh
# Start Slack bot in background
python bot.py &

# Start Flask app in foreground (gunicorn for production)
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 app:app
