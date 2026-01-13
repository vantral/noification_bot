import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path
import json

import gspread
from dateutil import tz
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ChatMemberHandler

# ----------------------------
# CONFIG
# ----------------------------
TELEGRAM_BOT_TOKEN = ""
SPREADSHEET_ID = ""
SERVICE_ACCOUNT_FILE = "service.json"

GROUPS_FILE = Path("groups.json")  # will store chat IDs here
GROUP_CHAT_IDS: set[int] = set()

# Worksheet (tab) name inside the spreadsheet (the single table tab)
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Sheet1")

# Your timezone for "today"
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Moscow")
LOCAL_TZ = tz.gettz(TZ_NAME)

# How often to check (seconds). The user asked: "read each hour".
CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "3600"))

# Optional: if bot was down, you might want catch-up behavior. Default False.
# If True, triggers when deadline <= today (instead of == today).
CATCH_UP_PAST_DEADLINES = os.environ.get("CATCH_UP_PAST_DEADLINES", "false").lower() == "true"

# Mapping tag -> chat_id (FILL THIS)
TAG_TO_CHAT_ID: Dict[str, int] = {
    "@vokat": 139123182,
    "@vantral": 597684697,
    "@meunierquidort": 529807704,
    "@pkspacewalker": 253295525
}

USER_ID_TO_TAG: dict[int, str] = {
    139123182: "@vokat",
    597684697: "@vantral",
    529807704: "@meunierquidort",
    253295525: "@pkspacewalker"
}
# ----------------------------
# TIME / DATE HELPERS
# ----------------------------
def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)

def today_local() -> date:
    return now_local().date()

def parse_date_mixed(s: str) -> Optional[date]:
    """Accepts: '14/01/2026', '07.01.2026', '2026-01-14'."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

def load_groups() -> set[int]:
    if not GROUPS_FILE.exists():
        return set()
    try:
        data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        return set(int(x) for x in data)
    except Exception:
        return set()

def save_groups(groups: set[int]) -> None:
    GROUPS_FILE.write_text(json.dumps(sorted(groups)), encoding="utf-8")

def register_group(chat_id: int) -> None:
    if chat_id not in GROUP_CHAT_IDS:
        GROUP_CHAT_IDS.add(chat_id)
        save_groups(GROUP_CHAT_IDS)
        print(f"[INFO] Registered group chat_id={chat_id}")


# ----------------------------
# DATA MODEL
# ----------------------------
@dataclass
class Row:
    post_date: Optional[date]
    topic: str
    block: str
    who_tag: str
    d1: Optional[date]
    d2: Optional[date]

# ----------------------------
# GOOGLE SHEETS
# ----------------------------
def open_sheet():
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh

def read_rows() -> List[Row]:
    sh = open_sheet()
    ws = sh.worksheet(WORKSHEET_NAME)
    records: List[Dict[str, Any]] = ws.get_all_records(expected_headers=["Date", "Topic", "Block", "Who", "First deadline", "Second deadline"])
    out: List[Row] = []
    for r in records:
        post_date = parse_date_mixed(str(r.get("Date", "")).strip())
        topic = str(r.get("Topic", "")).strip()
        block = str(r.get("Block", "")).strip()
        who = str(r.get("Who", "")).strip()
        d1 = parse_date_mixed(str(r.get("First deadline", "")).strip())
        d2 = parse_date_mixed(str(r.get("Second deadline", "")).strip())
        out.append(Row(post_date, topic, block, who, d1, d2))
    return out

# ----------------------------
# DEADLINE LOGIC
# ----------------------------
def triggers_today(r: Row, ref: date) -> bool:
    deadlines = [r.d1, r.d2, r.post_date]
    if CATCH_UP_PAST_DEADLINES:
        return any(d is not None and d <= ref for d in deadlines)
    return any(d is not None and d == ref for d in deadlines)

def deadlines_ahead_list(r: Row, ref: date) -> List[date]:
    """All deadlines still ahead (>= today) among the two deadline columns."""
    dls: List[date] = []
    for d in (r.d1, r.d2):
        if d and d >= ref:
            dls.append(d)
    return sorted(set(dls))

# ----------------------------
# MESSAGE FORMATTING
# ----------------------------
def build_reminder_message(tag: str, r: Row, ref: date) -> Optional[str]:
    parts: List[str] = [tag]
    if r.post_date:
        parts.append(f"📅 Пост: {r.post_date.strftime('%d.%m.%Y')}")
    if r.topic:
        parts.append(f"🧩 Тема: {r.topic}")
    if r.block:
        parts.append(f"🧱 Блок: {r.block}")

    dls = deadlines_ahead_list(r, ref)
    if dls:
        parts.append("⏳ Дедлайны: " + ", ".join(d.strftime("%d.%m.%Y") for d in dls))

    if len(parts) <= 1:
        return None
    return "\n".join(parts)

def normalise_tag(s: str) -> Optional[str]:
    """Extracts @tag from input; returns '@tag' or None."""
    if not s:
        return None
    s = s.strip()
    m = re.search(r"@[\w_]+", s)
    return m.group(0) if m else None

def format_deadlines_ahead(rows: List[Row], ref: date, tag_filter: Optional[str]) -> str:
    """
    Produces a compact report:
    - group by tag
    - inside each tag, list items with post date/topic/block + deadlines ahead
    """
    grouped: Dict[str, List[Tuple[Row, List[date]]]] = {}

    for r in rows:
        if not r.who_tag:
            continue
        if tag_filter and r.who_tag != tag_filter:
            continue

        dls = deadlines_ahead_list(r, ref)
        if not dls:
            continue  # nothing ahead => skip

        grouped.setdefault(r.who_tag, []).append((r, dls))

    if not grouped:
        if tag_filter:
            return f"{tag_filter}\n✅ Дедлайнов впереди нет (или нет строк с этим тегом)."
        return "✅ Дедлайнов впереди нет."

    lines: List[str] = []
    title = f"📌 Дедлайны впереди (с {ref.strftime('%d.%m.%Y')})"
    if tag_filter:
        title += f" для {tag_filter}"
    lines.append(title)

    # Sort tags for stable output
    for tag in sorted(grouped.keys()):
        lines.append("")
        lines.append(f"{tag[0]} {tag[1:]}")

        # Sort items by nearest upcoming deadline, then by post date
        def item_key(item: Tuple[Row, List[date]]):
            r, dls = item
            nearest = dls[0] if dls else date.max
            return (nearest, r.post_date or date.max, r.topic)

        for r, dls in sorted(grouped[tag], key=item_key):
            meta_bits: List[str] = []
            if r.post_date:
                meta_bits.append(r.post_date.strftime("%d.%m.%Y"))
            if r.topic:
                meta_bits.append(r.topic)
            if r.block:
                meta_bits.append(f"{r.block}")

            meta = " — ".join(meta_bits) if meta_bits else "Без описания"
            dl_str = "\n".join("  ⏳ " + d.strftime("%d.%m.%Y") for d in dls)
            lines.append(f"• {meta}")
            lines.append(dl_str)

    return "\n".join(lines).strip()

# ----------------------------
# TELEGRAM HANDLERS
# ----------------------------
async def cmd_deadlines_ahead(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage:
      /deadlines_ahead
      /deadlines_ahead @vokat
    """
    tag_filter = None
    if context.args:
        tag_filter = normalise_tag(" ".join(context.args))

    rows = read_rows()
    ref = today_local()
    text = format_deadlines_ahead(rows, ref, tag_filter)

    await update.message.reply_text(text, disable_web_page_preview=True)

async def cmd_my_deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    tag = USER_ID_TO_TAG.get(user.id)
    if not tag:
        await update.message.reply_text(
            "I don't know your tag yet. Ask the admin to add your Telegram user_id to USER_ID_TO_TAG."
        )
        return

    rows = read_rows()
    ref = today_local()

    text = format_deadlines_ahead(rows, ref, tag_filter=tag)
    await update.message.reply_text(text, disable_web_page_preview=True)


async def on_any_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        register_group(chat.id)

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires when the bot's status changes in a chat (added/removed/promoted).
    """
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    new_status = update.my_chat_member.new_chat_member.status
    # statuses: "member", "administrator", "kicked", "left", ...
    if new_status in ("member", "administrator"):
        register_group(chat.id)
    elif new_status in ("left", "kicked"):
        if chat.id in GROUP_CHAT_IDS:
            GROUP_CHAT_IDS.remove(chat.id)
            save_groups(GROUP_CHAT_IDS)
            print(f"[INFO] Removed group chat_id={chat.id}")


# ----------------------------
# SCHEDULED REMINDERS (job queue)
# ----------------------------
async def send_text(application: Application, chat_id: int, text: str):
    try:
        await application.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    except TelegramError as e:
        print(f"[TelegramError] chat_id={chat_id}: {e}")

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = read_rows()
    ref = today_local()

    # Build list of reminder texts (one per row that triggers today)
    reminders: List[str] = []
    for r in rows:
        if not r.who_tag:
            continue
        if not triggers_today(r, ref):
            continue

        msg = build_reminder_message(r.who_tag, r, ref)
        if msg:
            reminders.append(msg)

    if not reminders:
        return

    # Broadcast: send every reminder to every known group
    for group_id in list(GROUP_CHAT_IDS):
        for text in reminders:
            try:
                await context.application.bot.send_message(
                    chat_id=group_id,
                    text=text,
                    disable_web_page_preview=True
                )
            except TelegramError as e:
                print(f"[TelegramError] group_id={group_id}: {e}")


REMINDER_TIME = time(15, 0)  # 15:00

def next_run_at(t: time) -> datetime:
    now = now_local()
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target

async def background_scheduler(app: Application):
    await asyncio.sleep(2)  # let bot start

    while True:
        run_at = next_run_at(REMINDER_TIME)
        sleep_seconds = (run_at - now_local()).total_seconds()
        await asyncio.sleep(max(0, sleep_seconds))

        # do the daily send
        try:
            await scheduled_check(ContextTypes.DEFAULT_TYPE(application=app))
        except Exception as e:
            print("[Scheduler error]", e)

async def cmd_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.message.reply_text(f"Your Telegram user_id is: {user.id}")


async def cmd_my_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.message.reply_text(f"Спи, моя радость, усни")


def main():
    global GROUP_CHAT_IDS
    GROUP_CHAT_IDS = load_groups()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # your command handlers
    app.add_handler(CommandHandler("deadlines_ahead", cmd_deadlines_ahead))
    app.add_handler(CommandHandler("my_deadlines", cmd_my_deadlines))
    app.add_handler(CommandHandler("my_id", cmd_my_id))
    app.add_handler(CommandHandler("my_sleep", cmd_my_sleep))

    # register group chats
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_any_group_message))



    # start background scheduler (your existing approach)
    async def on_startup(app: Application):
        asyncio.create_task(background_scheduler(app))  # your scheduler that calls scheduled_check

    app.post_init = on_startup
    app.run_polling()



if __name__ == "__main__":
    main()
