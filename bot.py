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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TIMEZONE = pytz.timezone("Europe/Moscow")
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DB_PATH = os.environ.get("DB_PATH", "broadcasts.db")

NAME, DATE, TIME_STATE, LINK, CHAT_INPUT = range(5)


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
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                date           TEXT NOT NULL,
                time           TEXT NOT NULL,
                link           TEXT NOT NULL,
                chat_id        INTEGER NOT NULL,
                target_chat_id INTEGER NOT NULL DEFAULT 0,
                sent_morning   INTEGER NOT NULL DEFAULT 0,
                sent_hour      INTEGER NOT NULL DEFAULT 0,
                sent_15min     INTEGER NOT NULL DEFAULT 0
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(broadcasts)").fetchall()]
        for col, definition in [
            ("target_chat_id", "INTEGER NOT NULL DEFAULT 0"),
            ("sent_morning",   "INTEGER NOT NULL DEFAULT 0"),
            ("sent_hour",      "INTEGER NOT NULL DEFAULT 0"),
            ("sent_15min",     "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE broadcasts ADD COLUMN {col} {definition}")


def db_add(name, date, time, link, chat_id, target_chat_id) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT INTO broadcasts (name, date, time, link, chat_id, target_chat_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, date, time, link, chat_id, target_chat_id),
        )
        return cur.lastrowid


def db_all():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT * FROM broadcasts").fetchall()


def db_list(chat_id):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT * FROM broadcasts WHERE chat_id = ? ORDER BY date, time", (chat_id,)
        ).fetchall()


def db_mark_sent(broadcast_id: int, column: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE broadcasts SET {column} = 1 WHERE id = ?", (broadcast_id,))


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


async def check_owner_cb(update: Update) -> bool:
    """Returns True if caller is owner; silently rejects otherwise."""
    if update.effective_user.id == OWNER_ID:
        return True
    await update.callback_query.answer("Нет доступа.", show_alert=True)
    return False


def extract_forwarded_chat_id(message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin and hasattr(origin, "chat"):
        return origin.chat.id
    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat:
        return fwd_chat.id
    return None


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить трансляцию", callback_data="menu_add")],
        [InlineKeyboardButton("📋 Список трансляций",   callback_data="menu_list")],
        [InlineKeyboardButton("🗑️ Удалить трансляцию",  callback_data="menu_delete")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Главное меню", callback_data="menu_back")]
    ])


# ---------------------------------------------------------------------------
# Reminder poller (runs every 5 minutes via JobQueue)
# ---------------------------------------------------------------------------

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    rows = db_all()

    for row in rows:
        id_, name, date, time, link, chat_id, target_chat_id, sent_morning, sent_hour, sent_15min = row
        broadcast_dt = parse_dt(date, time)

        reminders = [
            (
                broadcast_dt.replace(hour=9, minute=0, second=0, microsecond=0),
                sent_morning,
                "sent_morning",
                f"Привет! Сегодня у нас трансляция: «{name}», {time}\n{link}",
            ),
            (
                broadcast_dt - timedelta(hours=1),
                sent_hour,
                "sent_hour",
                f"Трансляция «{name}» через час!\n{link}",
            ),
            (
                broadcast_dt - timedelta(minutes=15),
                sent_15min,
                "sent_15min",
                f"Трансляция «{name}» через 15 минут!\n{link}",
            ),
        ]

        for reminder_dt, already_sent, col, text in reminders:
            if not already_sent and now >= reminder_dt:
                try:
                    await context.bot.send_message(target_chat_id, text)
                    db_mark_sent(id_, col)
                    logger.info("Sent %s for broadcast #%d", col, id_)
                except Exception as e:
                    logger.error("Failed to send %s for broadcast #%d: %s", col, id_, e)


# ---------------------------------------------------------------------------
# #вопрос — forward tagged messages to owner
# ---------------------------------------------------------------------------

async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    chat_name = chat.title or chat.username or str(chat.id)

    # Build a direct link to the message (works for public chats and supergroups)
    msg_link = None
    if chat.username:
        msg_link = f"https://t.me/{chat.username}/{message.message_id}"
    elif str(chat.id).startswith("-100"):
        pure_id = str(chat.id)[4:]
        msg_link = f"https://t.me/c/{pure_id}/{message.message_id}"

    header = f"❓ <b>Новый вопрос</b>\nЧат: «{chat_name}» (<code>{chat.id}</code>)"
    if msg_link:
        header += f'\n<a href="{msg_link}">Перейти к сообщению ↗</a>'

    try:
        await context.bot.send_message(OWNER_ID, header, parse_mode="HTML")
        await context.bot.forward_message(
            chat_id=OWNER_ID,
            from_chat_id=chat.id,
            message_id=message.message_id,
        )
        logger.info("Forwarded question from chat %d to owner", chat.id)
    except Exception as e:
        logger.error("Failed to forward question from %s: %s", chat.id, e)


# ---------------------------------------------------------------------------
# /start — main menu
# ---------------------------------------------------------------------------

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Выберите действие:",
        reply_markup=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Menu callbacks
# ---------------------------------------------------------------------------

async def cb_menu_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_owner_cb(update):
        return
    await query.answer()
    await query.edit_message_text("Выберите действие:", reply_markup=main_menu_keyboard())


async def cb_menu_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_owner_cb(update):
        return
    await query.answer()

    rows = db_list(update.effective_chat.id)
    if not rows:
        await query.edit_message_text(
            "Нет запланированных трансляций.",
            reply_markup=back_keyboard(),
        )
        return

    now = datetime.now(TIMEZONE)
    lines = ["<b>Трансляции:</b>\n"]
    for row in rows:
        id_, name, date, time, link, chat_id, target_chat_id, *_ = row
        status = "впереди ✅" if parse_dt(date, time) > now else "прошла"
        lines.append(
            f"[{id_}] «{name}» — {date} в {time} ({status})\n"
            f"Чат: <code>{target_chat_id}</code>\n"
            f"{link}\n"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


async def cb_menu_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_owner_cb(update):
        return
    await query.answer()

    rows = db_list(update.effective_chat.id)
    if not rows:
        await query.edit_message_text(
            "Нет трансляций для удаления.",
            reply_markup=back_keyboard(),
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 [{r[0]}] {r[1]} — {r[2]} {r[3]}", callback_data=f"del_{r[0]}")]
        for r in rows
    ]
    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="menu_back")])
    await query.edit_message_text(
        "Выберите трансляцию для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_owner_cb(update):
        return
    await query.answer()

    broadcast_id = int(query.data.removeprefix("del_"))
    if db_delete(broadcast_id, update.effective_chat.id):
        await query.edit_message_text(
            f"✅ Трансляция #{broadcast_id} удалена.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await query.edit_message_text(
            "Трансляция не найдена.",
            reply_markup=main_menu_keyboard(),
        )


# ---------------------------------------------------------------------------
# Add broadcast — conversation
# ---------------------------------------------------------------------------

@owner_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите название трансляции:")
    return NAME


async def cb_menu_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_owner_cb(update):
        return
    await query.answer()
    await query.edit_message_text("Введите название трансляции:")
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
    context.user_data["link"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите ID чата для публикации напоминаний.\n\n"
        "Не знаете ID? Напишите /chatid прямо в той группе/канале — бот пришлёт его."
    )
    return CHAT_INPUT


async def step_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    target_chat_id = extract_forwarded_chat_id(message)

    if target_chat_id is None:
        try:
            target_chat_id = int(message.text.strip())
        except (ValueError, AttributeError):
            await message.reply_text(
                "Не понял. Введите ID чата числом (например: -1001234567890):"
            )
            return CHAT_INPUT

    name = context.user_data["name"]
    date = context.user_data["date"]
    time = context.user_data["time"]
    link = context.user_data["link"]
    owner_chat_id = update.effective_chat.id

    db_add(name, date, time, link, owner_chat_id, target_chat_id)

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

    await message.reply_text(
        f"✅ <b>Трансляция добавлена!</b>\n\n"
        f"Название: {name}\n"
        f"Дата: {date} в {time}\n"
        f"Ссылка: {link}\n"
        f"Чат публикации: <code>{target_chat_id}</code>\n\n"
        f"Напоминания:\n{reminders_text}",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /chatid — use in a group to get its ID
# ---------------------------------------------------------------------------

async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"ID этого чата: <code>{chat.id}</code>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Legacy text commands (still work alongside buttons)
# ---------------------------------------------------------------------------

@owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_list(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Нет запланированных трансляций.", reply_markup=main_menu_keyboard())
        return

    now = datetime.now(TIMEZONE)
    lines = ["<b>Трансляции:</b>\n"]
    for row in rows:
        id_, name, date, time, link, chat_id, target_chat_id, *_ = row
        status = "впереди ✅" if parse_dt(date, time) > now else "прошла"
        lines.append(
            f"[{id_}] «{name}» — {date} в {time} ({status})\n"
            f"Чат: <code>{target_chat_id}</code>\n"
            f"{link}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


@owner_only
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_list(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Нет трансляций для удаления.", reply_markup=main_menu_keyboard())
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 [{r[0]}] {r[1]} — {r[2]} {r[3]}", callback_data=f"del_{r[0]}")]
        for r in rows
    ]
    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="menu_back")])
    await update.message.reply_text(
        "Выберите трансляцию для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.job_queue.run_repeating(check_reminders, interval=300, first=10)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", cmd_add),
            CallbackQueryHandler(cb_menu_add, pattern="^menu_add$"),
        ],
        states={
            NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_name)],
            DATE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_date)],
            TIME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_time)],
            LINK:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_link)],
            CHAT_INPUT: [MessageHandler(filters.ALL & ~filters.COMMAND, step_chat)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("delete", cmd_delete))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(cb_menu_list,   pattern="^menu_list$"))
    app.add_handler(CallbackQueryHandler(cb_menu_delete, pattern="^menu_delete$"))
    app.add_handler(CallbackQueryHandler(cb_menu_back,   pattern="^menu_back$"))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^del_\d+$"))

    # #вопрос handler — any group/channel where bot is present
    app.add_handler(MessageHandler(
        (filters.Regex(r"(?i)#вопрос") | filters.CaptionRegex(r"(?i)#вопрос"))
        & ~filters.ChatType.PRIVATE,
        handle_question,
    ))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
