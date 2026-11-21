"""12-week personal fitness tracker for Telegram, designed for Railway + PostgreSQL."""
import logging
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Dubai")
GOALS = {"padel": ("🎾 Падел", 4), "run": ("🏃 Бег", 2), "strength": ("💪 Силовые", 3)}
START_MONDAY = datetime(2026, 11, 23, tzinfo=TZ).date()
WEEKS = 12

def db():
    url = os.environ["DATABASE_URL"]
    return psycopg.connect(url, autocommit=True)

def initialize():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS participants (
            chat_id BIGINT PRIMARY KEY,
            enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS progress (
            chat_id BIGINT NOT NULL,
            week_num INTEGER NOT NULL,
            activity TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, week_num, activity))""")

def week_num(today=None):
    today = today or datetime.now(TZ).date()
    return (today - START_MONDAY).days // 7 + 1

def read_counts(chat_id, week):
    result = {k: 0 for k in GOALS}
    with db() as conn:
        rows = conn.execute("SELECT activity, count FROM progress WHERE chat_id=%s AND week_num=%s", (chat_id, week)).fetchall()
    for activity, n in rows:
        if activity in result:
            result[activity] = n
    return result

def render(chat_id, week):
    counts = read_counts(chat_id, week)
    monday = START_MONDAY + timedelta(weeks=week - 1)
    sunday = monday + timedelta(days=6)
    lines = [f"📅 Неделя {week}/{WEEKS} · {monday:%d.%m}–{sunday:%d.%m}", ""]
    keyboard = []
    for key, (label, goal) in GOALS.items():
        count = counts[key]
        lines.append(f"{label}: {count}/{goal} " + "✅" * count + "▫️" * (goal - count))
        keyboard.append([
            InlineKeyboardButton(f"➖ {label}", callback_data=f"edit:{week}:{key}:-1"),
            InlineKeyboardButton("➕ Отметить", callback_data=f"edit:{week}:{key}:1")
        ])
    total = sum(counts.values())
    lines.extend(["", f"Всего тренировок: {total}/9", "Цель по силовым: 2–3 раза в неделю.", "Нажимай ➕ после завершённой тренировки."])
    keyboard.append([InlineKeyboardButton("📊 Статистика", callback_data="stats")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as conn:
        conn.execute("INSERT INTO participants(chat_id) VALUES(%s) ON CONFLICT DO NOTHING", (chat_id,))
    w = week_num()
    if w < 1:
        await update.message.reply_text("Программа стартует 23 ноября 2026. Напиши /today в понедельник, чтобы открыть первую неделю.")
    elif w > WEEKS:
        await update.message.reply_text("12 недель завершились! Напиши /stats для итогов.")
    else:
        txt, kb = render(chat_id, w)
        await update.message.reply_text(txt, reply_markup=kb)

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = week_num()
    upper = min(max(w, 1), WEEKS)
    with db() as conn:
        rows = conn.execute("""SELECT week_num, activity, count FROM progress
            WHERE chat_id=%s AND week_num BETWEEN 1 AND %s ORDER BY week_num""", (chat_id, upper)).fetchall()
    sums = {key: 0 for key in GOALS}
    week_totals = {}
    for week, activity, count in rows:
        if activity in sums:
            sums[activity] += count
            week_totals[week] = week_totals.get(week, 0) + count
    text = ["📊 Статистика программы", f"Прошло недель: {upper}/{WEEKS}"]
    for key, (label, goal) in GOALS.items():
        text.append(f"{label}: {sums[key]} / {goal * upper}")
    text.append(f"Всего тренировок: {sum(sums.values())}")
    text.append("\nПо неделям: " + ", ".join(f"{i}: {week_totals.get(i, 0)}" for i in range(1, upper+1)))
    return "\n".join(text)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await stats(update, context))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_chat = query.message.chat_id
    data = query.data
    if data == "stats":
        await query.answer()
        await query.message.reply_text(await stats(update, context))
        return
    try:
        _, w_string, activity, change_str = data.split(":")
        w, change = int(w_string), int(change_str)
    except (ValueError, AttributeError):
        await query.answer("Неизвестная кнопка")
        return
    if activity not in GOALS or change not in (-1, 1) or w != week_num() or not (1 <= w <= WEEKS):
        await query.answer("Эта неделя уже закрыта. Используй /today.", show_alert=True)
        return
    goal = GOALS[activity][1]
    with db() as conn:
        conn.execute("INSERT INTO participants(chat_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_chat,))
        conn.execute("""INSERT INTO progress(chat_id, week_num, activity, count)
          VALUES (%s,%s,%s,%s)
          ON CONFLICT (chat_id, week_num, activity)
          DO UPDATE SET count=LEAST(%s, GREATEST(0, progress.count + %s))""",
          (user_chat, w, activity, max(change, 0), goal, change))
    txt, kb = render(user_chat, w)
    await query.answer("Сохранено ✅")
    try:
        await query.edit_message_text(txt, reply_markup=kb)
    except Exception as exc:
        log.warning("Could not edit message: %s", exc)

async def monday_notify(context: ContextTypes.DEFAULT_TYPE):
    w = week_num()
    if not 1 <= w <= WEEKS:
        return
    with db() as conn:
        chat_ids = [r[0] for r in conn.execute("SELECT chat_id FROM participants").fetchall()]
    for chat_id in chat_ids:
        try:
            txt, kb = render(chat_id, w)
            await context.bot.send_message(chat_id, "🚀 Новая неделя! Твой план обновлён.")
            await context.bot.send_message(chat_id, txt, reply_markup=kb)
        except Exception as exc:
            log.warning("Notification to %s failed: %s", chat_id, exc)

async def sunday_notify(context: ContextTypes.DEFAULT_TYPE):
    w = week_num()
    if not 1 <= w <= WEEKS:
        return
    with db() as conn:
        chat_ids = [r[0] for r in conn.execute("SELECT chat_id FROM participants").fetchall()]
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id, "🏁 Итоги недели:\n" + await stats_for_week(chat_id, w))
        except Exception as exc:
            log.warning("Weekly report to %s failed: %s", chat_id, exc)

def weekly_report(chat_id, w):
    counts = read_counts(chat_id, w)
    lines = [f"Неделя {w}/{WEEKS}:"]
    for key, (label, goal) in GOALS.items():
        lines.append(f"{label}: {counts[key]}/{goal}")
    return "\n".join(lines)

async def stats_for_week(chat_id, w):
    return weekly_report(chat_id, w)

async def test_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = max(1, min(week_num(), WEEKS))
    txt, kb = render(chat_id, w)
    await context.bot.send_message(
        chat_id,
        "🧪 ТЕСТ: Новая неделя! Твой план обновлён."
    )
    await context.bot.send_message(
        chat_id, txt, reply_markup=kb
    )
def main():
        if not os.environ.get("BOT_TOKEN") or not os.environ.get("DATABASE_URL"):
            raise RuntimeError("Set BOT_TOKEN and DATABASE_URL in Railway Variables")
        initialize()
        app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("testnotify", test_notify))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.job_queue.run_daily(monday_notify, time=time(7, 0, tzinfo=TZ), days=(1,), name="monday")
    app.job_queue.run_daily(sunday_notify, time=time(20, 0, tzinfo=TZ), days=(0,), name="sunday")
    log.info("Bot is running")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
