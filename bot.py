import os
import sqlite3
import logging
from datetime import datetime, timedelta
from functools import wraps
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
    CallbackQueryHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TIMEZONE = pytz.timezone("Europe/Moscow")
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DB_PATH = os.environ.get("DB_PATH", "broadcasts.db")

NAME, DATE, TIME_STATE, LINK = range(4)

scheduler = AsyncIOScheduler(timezone=TIMEZONE)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                date    TEXT NOT NULL,
                time    TEXT NOT NULL,
                link    TEXT NOT NULL,
                chat_id INTEGER NOT NULL
            )
        """)


def db_add(name, date, time, link, chat_id) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO broadcasts (name, date, time, link, chat_id) VALUES (?, ?, ?, ?, ?)",
            (name, date, time, link, chat_id),
        )
        return cur.lastrowid


def db_get(broadcast_id):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()


def db_list(chat_id):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT * FROM broadcasts WHERE chat_id = ? ORDER BY date, time", (chat_id,)
        ).fetchall()


def db_delete(broadcast_id, chat_id) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM broadcasts WHERE id = ? AND chat_id = ?", (broadcast_id, chat_id)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_dt(date_str: str, time_str: str) -> datetime:
    dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    return TIMEZONE.localize(dt)


def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Reminder senders
# ---------------------------------------------------------------------------

async def send_morning(bot, broadcast_id: int):
    row = db_get(broadcast_id)
    if row:
        _, name, date, time, link, chat_id = row
        await bot.send_message(
            chat_id,
            f"Привет! Сегодня у нас трансляция: «{name}», {time}\n{link}",
        )


async def send_hour(bot, broadcast_id: int):
    row = db_get(broadcast_id)
    if row:
        _, name, date, time, link, chat_id = row
        await bot.send_message(chat_id, f"Трансляция «{name}» через час!\n{link}")


async def send_15min(bot, broadcast_id: int):
    row = db_get(broadcast_id)
    if row:
        _, name, date, time, link, chat_id = row
        await bot.send_message(chat_id, f"Трансляция «{name}» через 15 минут!\n{link}")


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def schedule_jobs(bot, broadcast_id: int, date_str: str, time_str: str):
    now = datetime.now(TIMEZONE)
    broadcast_dt = parse_dt(date_str, time_str)

    jobs = {
        f"morning_{broadcast_id}": (
            broadcast_dt.replace(hour=9, minute=0, second=0, microsecond=0),
            send_morning,
        ),
        f"hour_{broadcast_id}": (broadcast_dt - timedelta(hours=1), send_hour),
        f"min15_{broadcast_id}": (broadcast_dt - timedelta(minutes=15), send_15min),
    }

    for job_id, (run_at, func) in jobs.items():
        if run_at > now:
            scheduler.add_job(
                func,
                DateTrigger(run_date=run_at),
                args=[bot, broadcast_id],
                id=job_id,
                replace_existing=True,
            )


def remove_jobs(broadcast_id: int):
    for job_id in [f"morning_{broadcast_id}", f"hour_{broadcast_id}", f"min15_{broadcast_id}"]:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


async def post_init(app: Application):
    now = datetime.now(TIMEZONE)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT * FROM broadcasts").fetchall()
    for row in rows:
        id_, name, date, time, link, chat_id = row
        if parse_dt(date, time) > now:
            schedule_jobs(app.bot, id_, date, time)
    scheduler.start()
    logger.info("Scheduler started, %d broadcast(s) loaded.", len(rows))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет!\n\n"
        "/add — добавить трансляцию\n"
        "/list — список трансляций\n"
        "/delete — удалить трансляцию"
    )


@owner_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите название трансляции:")
    return NAME


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Введите дату (ДД.ММ.ГГГГ), например: 20.05.2026")
    return DATE


async def step_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        if dt.date() < datetime.now(TIMEZONE).date():
            await update.message.reply_text("Эта дата уже прошла. Введите будущую дату:")
            return DATE
        context.user_data["date"] = date_str
        await update.message.reply_text("Введите время начала (ЧЧ:ММ), например: 19:00")
        return TIME_STATE
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите дату в виде ДД.ММ.ГГГГ:")
        return DATE


async def step_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text.strip()
    try:
        datetime.strptime(time_str, "%H:%M")
        context.user_data["time"] = time_str
        await update.message.reply_text("Введите ссылку на трансляцию:")
        return LINK
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите время в виде ЧЧ:ММ:")
        return TIME_STATE


async def step_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data["name"]
    date = context.user_data["date"]
    time = context.user_data["time"]
    link = update.message.text.strip()
    chat_id = update.effective_chat.id

    broadcast_id = db_add(name, date, time, link, chat_id)
    schedule_jobs(context.bot, broadcast_id, date, time)

    now = datetime.now(TIMEZONE)
    broadcast_dt = parse_dt(date, time)

    reminders = []
    morning = broadcast_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    if morning > now:
        reminders.append(f"• {morning.strftime('%d.%m в 09:00')} — утром")
    if (t := broadcast_dt - timedelta(hours=1)) > now:
        reminders.append(f"• {t.strftime('%d.%m в %H:%M')} — за час")
    if (t := broadcast_dt - timedelta(minutes=15)) > now:
        reminders.append(f"• {t.strftime('%d.%m в %H:%M')} — за 15 минут")

    reminders_text = "\n".join(reminders) if reminders else "нет (все напоминания уже прошли)"

    await update.message.reply_text(
        f"Готово!\n\n"
        f"Название: {name}\n"
        f"Дата: {date} в {time}\n"
        f"Ссылка: {link}\n\n"
        f"Напоминания:\n{reminders_text}"
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


@owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_list(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Нет запланированных трансляций.")
        return

    now = datetime.now(TIMEZONE)
    lines = ["Трансляции:\n"]
    for row in rows:
        id_, name, date, time, link, chat_id = row
        status = "впереди" if parse_dt(date, time) > now else "прошла"
        lines.append(f"[{id_}] «{name}» — {date} в {time} ({status})\n{link}\n")

    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_list(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Нет трансляций для удаления.")
        return

    keyboard = [
        [InlineKeyboardButton(f"[{r[0]}] {r[1]} — {r[2]} {r[3]}", callback_data=f"del_{r[0]}")]
        for r in rows
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="del_cancel")])
    await update.message.reply_text(
        "Выберите трансляцию для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "del_cancel":
        await query.edit_message_text("Отменено.")
        return

    broadcast_id = int(query.data.removeprefix("del_"))
    if db_delete(broadcast_id, update.effective_chat.id):
        remove_jobs(broadcast_id)
        await query.edit_message_text(f"Трансляция #{broadcast_id} удалена.")
    else:
        await query.edit_message_text("Трансляция не найдена.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_name)],
            DATE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_date)],
            TIME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_time)],
            LINK:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_link)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^del_"))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
