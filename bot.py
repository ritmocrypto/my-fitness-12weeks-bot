"""12-week personal fitness tracker for Telegram, designed for Railway + PostgreSQL."""
import logging
import asyncio
import base64
import json
import re
from io import BytesIO
from decimal import Decimal, InvalidOperation
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import psycopg
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Dubai")
GOALS = {"padel": ("🎾 Падел", 4), "run": ("🏃 Бег", 2), "strength": ("💪 Силовые", 3)}
START_MONDAY = datetime(2026, 11, 23, tzinfo=TZ).date()
WEEKS = 12
TEST_MODE = os.getenv("TEST_MODE", "false").lower() in ("1", "true", "yes")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
AI = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=45.0, max_retries=1) if os.getenv("OPENAI_API_KEY") else None

def active_week():
    w = week_num()
    return 0 if w < 1 and TEST_MODE else w

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
        conn.execute("""CREATE TABLE IF NOT EXISTS nutrition_settings (
            chat_id BIGINT PRIMARY KEY, calorie_goal INTEGER NOT NULL DEFAULT 2500)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS meals (
            id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
            local_date DATE NOT NULL, description TEXT NOT NULL,
            calories INTEGER NOT NULL, protein NUMERIC(8,1) NOT NULL,
            fat NUMERIC(8,1) NOT NULL, carbs NUMERIC(8,1) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE INDEX IF NOT EXISTS meals_by_chat_day
            ON meals(chat_id,local_date)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS meal_drafts (
            id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
            description TEXT NOT NULL, calories INTEGER NOT NULL,
            protein NUMERIC(8,1) NOT NULL, fat NUMERIC(8,1) NOT NULL,
            carbs NUMERIC(8,1) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS meal_templates (
            id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
            description TEXT NOT NULL, calories INTEGER NOT NULL,
            protein NUMERIC(8,1) NOT NULL, fat NUMERIC(8,1) NOT NULL,
            carbs NUMERIC(8,1) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(chat_id, description))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS body_weights (
            id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
            local_date DATE NOT NULL, weight_kg NUMERIC(5,2) NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(chat_id,local_date))""")

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
    heading = "🧪 ТЕСТОВАЯ НЕДЕЛЯ (не входит в программу)" if week == 0 else f"📅 Неделя {week}/{WEEKS}"
    lines = [f"{heading} · {monday:%d.%m}–{sunday:%d.%m}", ""]
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
    keyboard.append([InlineKeyboardButton("🍽 Питание сегодня", callback_data="nutrition")])
    keyboard.append([InlineKeyboardButton("⭐ Шаблоны блюд", callback_data="templates")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as conn:
        conn.execute("INSERT INTO participants(chat_id) VALUES(%s) ON CONFLICT DO NOTHING", (chat_id,))
    w = active_week()
    if w < 0:
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
    if data == "nutrition" or data == "templates" or data.startswith(("meal:", "template:", "save:")):
        await nutrition_button(update, context)
        return
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
    if activity not in GOALS or change not in (-1, 1) or w != active_week() or not (0 <= w <= WEEKS):
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
    # Отчёт по питанию в пределах календарной недели.
    monday = START_MONDAY + timedelta(weeks=w-1)
    sunday = monday + timedelta(days=6)
    with db() as conn:
        nutrition = conn.execute("""SELECT COUNT(DISTINCT local_date),
            COALESCE(SUM(calories),0) FROM meals
            WHERE chat_id=%s AND local_date BETWEEN %s AND %s""",
            (chat_id, monday, sunday)).fetchone()
    lines.append(f"Питание: {nutrition[0]} дней с записями, {nutrition[1]} ккал суммарно")
    return "\n".join(lines)

async def stats_for_week(chat_id, w):
    return weekly_report(chat_id, w)

async def test_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = active_week()
    if w > WEEKS:
        await update.message.reply_text("Программа уже завершена.")
        return
    if w < 0:
        await update.message.reply_text("Для предварительного теста добавь TEST_MODE=true в Railway Variables.")
        return
    txt, kb = render(chat_id, w)
    await context.bot.send_message(
        chat_id,
        "🧪 ТЕСТ: Новая неделя! Твой план обновлён."
    )
    await context.bot.send_message(
        chat_id, txt, reply_markup=kb
    )
    
async def test_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if active_week() != 0:
        await update.message.reply_text("Сброс доступен только до старта программы в тестовом режиме.")
        return
    with db() as conn:
        conn.execute("DELETE FROM progress WHERE chat_id=%s AND week_num=0", (update.effective_chat.id,))
    await update.message.reply_text("🧪 Тестовые отметки очищены. Напиши /today.")


# ── Питание: вся история хранится в PostgreSQL; фотографий в БД нет. ──
FOOD_GUIDE = (
    "🍽 Отправь описание еды обычным сообщением или фотографию с подписью.\n"
    "Пример: 2 яйца, 2 тоста, 100 г авокадо, кофе без сахара.\n"
    "ИИ предложит калории и БЖУ — нажми «Записать», если оценка подходит.\n\n"
    "Без ИИ можно внести точно: /manual Название | 500 | 30 | 20 | 45\n"
    "(ккал | белки, г | жиры, г | углеводы, г).\n"
    "Команды: /nutrition, /templates, /weight 88.5, /calgoal 2500, /undofood."
)


def today_local():
    return datetime.now(TZ).date()


def numbers(value, lower, upper, name):
    try:
        x = float(str(value).replace(",", "."))
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"Некорректное значение: {name}")
    if not (lower <= x <= upper) or x != x:
        raise ValueError(f"Недопустимое значение: {name}")
    return round(x, 1)


def normalize_meal(item):
    description = str(item.get("description") or "Блюдо").strip()[:250]
    if not description:
        raise ValueError("Нет названия блюда")
    return (description, int(round(numbers(item.get("calories"), 0, 10000, "калории"))),
            numbers(item.get("protein"), 0, 600, "белки"),
            numbers(item.get("fat"), 0, 600, "жиры"),
            numbers(item.get("carbs"), 0, 1000, "углеводы"))


def meal_line(item):
    desc, kcal, p, f, c = item
    return f"{desc}\n🔥 {kcal} ккал · Б {p:g} г · Ж {f:g} г · У {c:g} г"


def create_draft(chat_id, meal):
    with db() as conn:
        conn.execute("DELETE FROM meal_drafts WHERE chat_id=%s AND created_at < NOW() - INTERVAL '2 days'", (chat_id,))
        return conn.execute("""INSERT INTO meal_drafts
            (chat_id,description,calories,protein,fat,carbs)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""", (chat_id,*meal)).fetchone()[0]


async def show_draft(message, chat_id, meal, origin="ИИ"):
    did = create_draft(chat_id, meal)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Записать в дневник", callback_data=f"meal:add:{did}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"meal:cancel:{did}")]
    ])
    await message.reply_text(f"{origin} — оценка:\n{meal_line(meal)}\n\n⚠️ Проверь размер порции, масло и соусы. Для коррекции: /manual ...", reply_markup=kb)


async def ai_estimate(text, image_bytes=None):
    if AI is None:
        raise RuntimeError("В Railway не настроен OPENAI_API_KEY. Пока доступен /manual.")
    system = ("Ты помощник по питанию. Оцени примерно калории и БЖУ всей порции. "
              "Учитывай указанные граммы; если порция неизвестна, делай разумное допущение и УКАЖИ его в description. "
              "Не выдавай оценку за точные измерения. Если изображение не содержит еды или данных недостаточно, "
              "верни JSON {\"error\":\"Не удалось определить еду\"}. "
              "Верни только JSON объект: description (на русском), calories (ккал), protein, fat, carbs (граммы). "
              "Без пояснений и markdown.")
    content = [{"type": "input_text", "text": text or "Оцени всю порцию еды на фотографии; укажи допущения по объёму и ингредиентам."}]
    if image_bytes:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type":"input_image", "image_url":f"data:image/jpeg;base64,{encoded}", "detail":"low"})
    response = await AI.responses.create(
        model=OPENAI_MODEL, instructions=system,
        input=[{"role":"user","content":content}],
        text={"format":{"type":"json_object"}},
        store=False, max_output_tokens=400)
    obj = json.loads(response.output_text)
    if obj.get("error"):
        raise ValueError(str(obj["error"])[:150])
    return normalize_meal(obj)


async def food_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(FOOD_GUIDE)


async def manual_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.partition(" ")[2]
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 5:
        await update.message.reply_text("Формат: /manual Омлет | 350 | 22 | 25 | 8\n(ккал | белки | жиры | углеводы)")
        return
    try:
        meal = normalize_meal(dict(zip(("description","calories","protein","fat","carbs"), parts)))
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    await show_draft(update.message, update.effective_chat.id, meal, origin="Ручной ввод")


async def food_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type != "private":
        return
    # Не обрабатываем произвольный текст, если AI выключен: не тратим запросы и не создаём шум.
    is_photo = bool(message.photo)
    if not is_photo and not message.text:
        return
    if AI is None:
        await message.reply_text("Для анализа еды нужен OPENAI_API_KEY в Railway. Сейчас можно: /manual Омлет | 350 | 22 | 25 | 8")
        return
    image_bytes = None
    if is_photo:
        # Берём фото среднего размера, чтобы уменьшить стоимость и размер API-вызова.
        photo = next((p for p in reversed(message.photo) if p.file_size and p.file_size <= 5_000_000), message.photo[0])
        file = await photo.get_file()
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_bytes = buf.getvalue()
    await message.reply_text("🔎 Оцениваю порцию…")
    try:
        meal = await ai_estimate(message.caption if is_photo else message.text, image_bytes)
    except Exception as exc:
        log.warning("Food estimation failed: %s", type(exc).__name__)
        await message.reply_text("Не удалось оценить блюдо. Попробуй уточнить порцию или используй /manual. Проверь OPENAI_API_KEY и баланс API, если ошибка повторяется.")
        return
    await show_draft(message, update.effective_chat.id, meal)


def nutrition_dashboard(chat_id):
    d = today_local()
    with db() as conn:
        setting = conn.execute("SELECT calorie_goal FROM nutrition_settings WHERE chat_id=%s", (chat_id,)).fetchone()
        goal = setting[0] if setting else 2500
        totals = conn.execute("""SELECT COALESCE(SUM(calories),0),
            COALESCE(SUM(protein),0), COALESCE(SUM(fat),0), COALESCE(SUM(carbs),0), COUNT(*)
            FROM meals WHERE chat_id=%s AND local_date=%s""", (chat_id,d)).fetchone()
        items = conn.execute("""SELECT description,calories FROM meals
            WHERE chat_id=%s AND local_date=%s ORDER BY id DESC LIMIT 8""", (chat_id,d)).fetchall()
    kcal,p,f,c,count=totals
    lines=[f"🍽 Питание за {d:%d.%m.%Y} (Дубай)",
           f"🔥 {kcal}/{goal} ккал · {'Осталось' if kcal <= goal else 'Превышение'} {abs(goal-kcal)} ккал",
           f"Б {p:g} г · Ж {f:g} г · У {c:g} г", f"Приёмов пищи: {count}"]
    if items:
        lines.append("\nПоследние записи:")
        lines.extend(f"• {name} — {k} ккал" for name,k in items)
    lines.append("\n/food — инструкция · /templates — шаблоны · /undofood — удалить последнюю запись")
    return "\n".join(lines)


async def nutrition_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(nutrition_dashboard(update.effective_chat.id))


async def goal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args=context.args
    if not args:
        await update.message.reply_text("Укажи цель: /calgoal 2500 (допустимо 1200–5000 ккал)")
        return
    try:
        val=int(args[0])
        if not 1200 <= val <= 5000: raise ValueError()
    except ValueError:
        await update.message.reply_text("Укажи число от 1200 до 5000, например /calgoal 2500")
        return
    with db() as conn:
        conn.execute("""INSERT INTO nutrition_settings(chat_id,calorie_goal) VALUES(%s,%s)
            ON CONFLICT(chat_id) DO UPDATE SET calorie_goal=EXCLUDED.calorie_goal""", (update.effective_chat.id,val))
    await update.message.reply_text(f"🎯 Цель установлена: {val} ккал/день. Это ориентир, не медицинская рекомендация.")


async def weight_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    if context.args:
        try:
            w=numbers(context.args[0],30,300,"вес")
        except ValueError:
            await update.message.reply_text("Пример: /weight 88.5 (в кг)")
            return
        with db() as conn:
            conn.execute("""INSERT INTO body_weights(chat_id,local_date,weight_kg) VALUES(%s,%s,%s)
                ON CONFLICT(chat_id,local_date) DO UPDATE SET weight_kg=EXCLUDED.weight_kg,
                recorded_at=NOW()""", (cid,today_local(),w))
        await update.message.reply_text(f"⚖️ Вес {w:g} кг сохранён на сегодня.")
    else:
        with db() as conn:
            rows=conn.execute("""SELECT local_date,weight_kg FROM body_weights WHERE chat_id=%s
                ORDER BY local_date DESC LIMIT 7""",(cid,)).fetchall()
        if not rows:
            await update.message.reply_text("Записей веса нет. Введи /weight 88.5")
        else:
            await update.message.reply_text("⚖️ Последние измерения:\n"+"\n".join(f"{d:%d.%m}: {w:g} кг" for d,w in rows))


async def undofood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    with db() as conn:
        row=conn.execute("""DELETE FROM meals WHERE id=(SELECT id FROM meals
            WHERE chat_id=%s AND local_date=%s ORDER BY id DESC LIMIT 1)
            AND chat_id=%s RETURNING description,calories""",(cid,today_local(),cid)).fetchone()
    await update.message.reply_text(f"🗑 Удалено: {row[0]} ({row[1]} ккал)" if row else "Сегодня записей нет.")


def templates_markup(chat_id):
    with db() as conn:
        rows=conn.execute("""SELECT id,description,calories FROM meal_templates
            WHERE chat_id=%s ORDER BY id DESC LIMIT 20""", (chat_id,)).fetchall()
    if not rows:
        return "Шаблонов ещё нет. После записи еды нажми «⭐ Сохранить шаблон».", None
    keyboard=[[InlineKeyboardButton(f"➕ {desc[:35]} · {k} ккал",callback_data=f"template:add:{id}")]
              for id,desc,k in rows]
    return "⭐ Выбери сохранённое блюдо для добавления в сегодняшний дневник:",InlineKeyboardMarkup(keyboard)


async def templates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt,kb=templates_markup(update.effective_chat.id)
    await update.message.reply_text(txt,reply_markup=kb)


async def nutrition_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    cid=q.message.chat_id
    data=q.data
    if data=="nutrition":
        await q.answer()
        await q.message.reply_text(nutrition_dashboard(cid))
        return
    if data=="templates":
        await q.answer()
        txt,kb=templates_markup(cid)
        await q.message.reply_text(txt,reply_markup=kb)
        return
    try:
        group,action,num=data.split(":")
        ident=int(num)
    except (ValueError,AttributeError):
        await q.answer("Ошибка кнопки",show_alert=True)
        return
    if group=="meal" and action in ("add","cancel"):
        with db() as conn:
            # DELETE ... RETURNING не даст записать один draft дважды при двойном нажатии.
            row=conn.execute("""DELETE FROM meal_drafts WHERE id=%s AND chat_id=%s
                RETURNING description,calories,protein,fat,carbs""",(ident,cid)).fetchone()
            if row and action=="add":
                added=conn.execute("""INSERT INTO meals
                    (chat_id,local_date,description,calories,protein,fat,carbs)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",(cid,today_local(),*row)).fetchone()[0]
        if not row:
            await q.answer("Уже обработано",show_alert=True)
            return
        await q.answer("Готово")
        if action=="cancel":
            await q.edit_message_text("❌ Запись отменена.")
        else:
            await q.edit_message_text("✅ Добавлено в дневник:\n"+meal_line(row),reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Сохранить шаблон",callback_data=f"save:template:{added}")]]))
        return
    if group=="save" and action=="template":
        with db() as conn:
            row=conn.execute("""SELECT description,calories,protein,fat,carbs
                FROM meals WHERE id=%s AND chat_id=%s""",(ident,cid)).fetchone()
            if row:
                conn.execute("""INSERT INTO meal_templates(chat_id,description,calories,protein,fat,carbs)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(chat_id,description)
                    DO UPDATE SET calories=EXCLUDED.calories, protein=EXCLUDED.protein,
                    fat=EXCLUDED.fat, carbs=EXCLUDED.carbs""",(cid,*row))
        await q.answer("⭐ Шаблон сохранён" if row else "Запись не найдена",show_alert=not bool(row))
        return
    if group=="template" and action=="add":
        with db() as conn:
            row=conn.execute("""SELECT description,calories,protein,fat,carbs
                FROM meal_templates WHERE id=%s AND chat_id=%s""",(ident,cid)).fetchone()
            if row:
                conn.execute("""INSERT INTO meals(chat_id,local_date,description,calories,protein,fat,carbs)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",(cid,today_local(),*row))
        await q.answer("✅ Добавлено" if row else "Шаблон не найден",show_alert=not bool(row))
        if row:
            await q.message.reply_text("✅ Из шаблона:\n"+meal_line(row))
        return
    await q.answer("Неизвестная кнопка")


def main():
    if not os.environ.get("BOT_TOKEN") or not os.environ.get("DATABASE_URL"):
        raise RuntimeError("Set BOT_TOKEN and DATABASE_URL in Railway Variables")
    initialize()
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("testnotify", test_notify))
    app.add_handler(CommandHandler("testreset", test_reset))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("food", food_cmd))
    app.add_handler(CommandHandler("manual", manual_cmd))
    app.add_handler(CommandHandler("nutrition", nutrition_cmd))
    app.add_handler(CommandHandler("calgoal", goal_cmd))
    app.add_handler(CommandHandler("weight", weight_cmd))
    app.add_handler(CommandHandler("undofood", undofood_cmd))
    app.add_handler(CommandHandler("templates", templates_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO, food_message))
    app.job_queue.run_daily(monday_notify, time=time(7, 0, tzinfo=TZ), days=(1,), name="monday")
    app.job_queue.run_daily(sunday_notify, time=time(20, 0, tzinfo=TZ), days=(0,), name="sunday")
    log.info("Bot is running")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
