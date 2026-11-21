# Fitness 12 Weeks Telegram Bot

Telegram tracker for 12 weeks of padel, running and strength training.

## Railway setup

1. Create a Railway project with this GitHub repo as a service.
2. Add a Railway PostgreSQL service.
3. In the bot service Variables, set:
   - `BOT_TOKEN`: Telegram BotFather token (secret)
   - `DATABASE_URL`: reference to PostgreSQL `DATABASE_URL` (Add Reference)
   - `TZ`: `Asia/Dubai` (optional; bot schedule uses Asia/Dubai explicitly)
4. Start command: `python bot.py` (or use Procfile worker).
5. Open your bot and send `/start`.

Do not commit tokens or database credentials to GitHub.
