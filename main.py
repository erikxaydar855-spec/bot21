"""
MAKTAB DARSLIKLARI BOTI  (aiogram 3.x)

ISHGA TUSHIRISH:   py main.py
  - Birinchi ishga tushganda bot tokenni o'zi so'raydi va eslab qoladi.
  - Admin bo'lish uchun: terminalda chiqqan kodni botga yuboring:  /admin 123456

Hamma narsa tugmalar orqali ishlaydi, buyruq yodlash shart emas.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =============================================================== SOZLAMALAR (ixtiyoriy)
BOT_TOKEN = "9203487277:AAHh-28Db0V6PnA5qPW6RxkMellczrprBaQ"   # bo'sh qoldirsangiz, bot ishga tushganda o'zi so'raydi
ADMIN_IDS = [8496120359]   # bo'sh qoldirsangiz, /admin KOD orqali admin bo'lasiz
REQUIRED_CHANNEL = "@navoiy_shahar_21maktab"   # majburiy obuna kanali ("" qilsangiz, o'chiriladi)
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.db"
LOG_PATH = BASE_DIR / "bot.log"
CONFIG_PATH = BASE_DIR / "config.json"
PAGE_SIZE = 8
MIN_PW_LEN = 6
CHANNEL_URL = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}" if REQUIRED_CHANNEL else ""
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")

ICON = {"text": "📝", "document": "📄", "photo": "🖼", "video": "🎬", "audio": "🎧"}
KIND = {"text": "Matn", "document": "Fayl", "photo": "Rasm", "video": "Video", "audio": "Audio"}
esc = html.escape


def read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_config(data: dict):
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clean_token(raw: str) -> str:
    return (raw or "").strip().strip('"').strip("'").strip()


TOKEN = clean_token(BOT_TOKEN or os.getenv("BOT_TOKEN", "") or read_config().get("token", ""))

# =============================================================== BAZA
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        tg_id INTEGER PRIMARY KEY, name TEXT, joined TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS admins(tg_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS subjects(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS teachers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login TEXT UNIQUE NOT NULL,
        pw TEXT NOT NULL,
        subject_id INTEGER NOT NULL,
        full_name TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions(
        tg_id INTEGER PRIMARY KEY, teacher_id INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS materials(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        grade INTEGER NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        file_id TEXT,
        text TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS applications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL,
        username TEXT,
        full_name TEXT NOT NULL,
        subject_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_mat_sg ON materials(subject_id, grade);
    CREATE INDEX IF NOT EXISTS idx_mat_teacher ON materials(teacher_id, grade);
    """
)
# eski bazadan ko'chirish (oldingi versiyada full_name yo'q edi)
if "full_name" not in [x["name"] for x in DB.execute("PRAGMA table_info(teachers)")]:
    DB.execute("ALTER TABLE teachers ADD COLUMN full_name TEXT")
DB.commit()


def q(sql, *args):
    return DB.execute(sql, args).fetchall()


def q1(sql, *args):
    return DB.execute(sql, args).fetchone()


def run(sql, *args):
    cur = DB.execute(sql, args)
    DB.commit()
    return cur


def count(table, where="", *args):
    return q1(f"SELECT COUNT(*) AS n FROM {table} {where}", *args)["n"]


# =============================================================== ADMINLAR
def _load_admins() -> set:
    ids = {int(x) for x in ADMIN_IDS}
    for part in os.getenv("ADMIN_IDS", "").replace(";", ",").split(","):
        if part.strip().isdigit():
            ids.add(int(part.strip()))
    ids |= {x["tg_id"] for x in q("SELECT tg_id FROM admins")}
    return ids


ADMINS = _load_admins()
SETUP_CODE = str(secrets.randbelow(900000) + 100000)  # birinchi admin uchun bir martalik kod

# =============================================================== PAROL / LOGIN
def hash_pw(pw: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        return hmac.compare_digest(hash_pw(pw, salt).split("$")[1], h)
    except Exception:
        return False


PW_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # o'xshash belgilar (l, i, o, 0, 1) yo'q


def gen_password() -> str:
    return "".join(secrets.choice(PW_ALPHABET) for _ in range(8))


CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "s", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
}


def make_login(full_name: str) -> str:
    first = (full_name.lower().split() or ["ustoz"])[0]
    base = "".join(CYR.get(ch, ch) for ch in first)
    base = re.sub(r"[^a-z0-9]", "", base)[:12] or "ustoz"
    while True:
        login = f"{base}{random.randint(100, 999)}"
        if not q1("SELECT 1 FROM teachers WHERE login = ?", login):
            return login


def get_or_create_subject(name: str):
    name = " ".join(name.split())[:40]
    row = q1("SELECT id, name FROM subjects WHERE name = ? COLLATE NOCASE", name)
    if row:
        return row["id"], row["name"]
    return run("INSERT INTO subjects(name) VALUES(?)", name).lastrowid, name


def create_teacher(full_name: str, subject_name: str):
    """Ustozni yaratadi, login va parolni o'zi tuzadi."""
    sid, sname = get_or_create_subject(subject_name)
    login, pw = make_login(full_name), gen_password()
    tid = run(
        "INSERT INTO teachers(login, pw, subject_id, full_name) VALUES(?,?,?,?)",
        login, hash_pw(pw), sid, full_name,
    ).lastrowid
    return tid, login, pw, sname


def cred_text(title: str, name: str, subject: str, login: str, pw: str) -> str:
    return (
        f"{title}\n\n👤 {esc(name)}\n📘 {esc(subject)}\n\n"
        f"🔐 Login: <code>{esc(login)}</code>\n🔑 Parol: <code>{esc(pw)}</code>\n\n"
        "👆 Ustiga bosing, nusxalanadi."
    )


FAILS = {}  # tg_id -> (xato urinishlar, bloklanish tugash vaqti)


def blocked_seconds(uid: int) -> int:
    return max(0, int(FAILS.get(uid, (0, 0))[1] - time.time()))


def register_fail(uid: int):
    tries = FAILS.get(uid, (0, 0))[0] + 1
    FAILS[uid] = (0, time.time() + 300) if tries >= 5 else (tries, 0)


# =============================================================== YORDAMCHILAR
def ikb(*rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


def grade_rows(prefix: str, counts: dict = None, per_row: int = 4):
    rows, row = [], []
    for g in range(1, 12):
        label = f"{g}-sinf" if counts is None else f"{g}-sinf ({counts.get(g, 0)})"
        row.append((label, f"{prefix}{g}"))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def clamp_page(page: int, total: int) -> int:
    return max(0, min(page, max(0, (total - 1) // PAGE_SIZE)))


def pager_row(prefix: str, page: int, total: int):
    pages = max(1, -(-total // PAGE_SIZE))
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(("◀️", f"{prefix}:{page - 1}"))
    row.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(("▶️", f"{prefix}:{page + 1}"))
    return [row]


def get_teacher(tg_id: int):
    return q1(
        """SELECT t.id, t.login, t.pw, t.full_name, t.subject_id, s.name AS subject
           FROM sessions ss
           JOIN teachers t ON t.id = ss.teacher_id
           JOIN subjects s ON s.id = t.subject_id
           WHERE ss.tg_id = ?""",
        tg_id,
    )


def tname(t) -> str:
    return t["full_name"] or t["login"]


def clean_name(text: str) -> str:
    return " ".join((text or "").split())[:60]


def split_title(raw: str):
    """Matnning birinchi qatori - nom, qolgani - matn."""
    raw = (raw or "").strip()
    first, _, rest = raw.partition("\n")
    first, rest = first.strip(), rest.strip()
    if len(first) <= 80:
        return first, rest
    return first[:60].rstrip() + "…", raw


async def safe_delete(m: Message):
    try:
        await m.delete()
    except Exception:
        pass


async def show(target, text, kb=None, **kw):
    """Tugmadan kelgan bo'lsa xabarni tahrirlaydi, aks holda yangi xabar yuboradi."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, **kw)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.bot.send_message(target.from_user.id, text, reply_markup=kb, **kw)
        except AttributeError:
            await target.bot.send_message(target.from_user.id, text, reply_markup=kb, **kw)
        try:
            await target.answer()
        except TelegramBadRequest:
            pass
    else:
        await target.answer(text, reply_markup=kb, **kw)


MAT_SQL = """SELECT m.*, s.name AS subject, t.full_name AS tfull, t.login AS tlogin
             FROM materials m
             JOIN subjects s ON s.id = m.subject_id
             LEFT JOIN teachers t ON t.id = m.teacher_id
             WHERE m.id = ?"""


async def send_material(bot: Bot, chat_id: int, m, markup):
    who = m["tfull"] or m["tlogin"]
    head = f"📖 {m['subject']} · {m['grade']}-sinf\n{m['title']}" + (f"\n👨‍🏫 {who}" if who else "")
    body = (m["text"] or "").strip()

    async def send_text(full: str):
        chunks = [full[i : i + 4000] for i in range(0, len(full), 4000)] or [full]
        for i, ch in enumerate(chunks):
            await bot.send_message(chat_id, ch, reply_markup=markup if i == len(chunks) - 1 else None)

    if m["kind"] == "text":
        return await send_text(f"{head}\n\n{body}" if body else head)

    sender = {
        "document": bot.send_document,
        "photo": bot.send_photo,
        "video": bot.send_video,
        "audio": bot.send_audio,
    }[m["kind"]]
    full = f"{head}\n\n{body}" if body else head
    if len(full) <= 1024:
        await sender(chat_id, m["file_id"], caption=full, reply_markup=markup)
    else:
        await sender(chat_id, m["file_id"], caption=head)
        await send_text(body)


def parse_content(m: Message):
    """Xabardan (tur, file_id, nom, matn) ni ajratadi."""
    if m.text:
        title, rest = split_title(m.text)
        return "text", None, title, rest
    cap = (m.caption or "").strip()
    stamp = time.strftime("%d.%m %H:%M")
    if m.document:
        kind, fid, default = "document", m.document.file_id, (m.document.file_name or f"Fayl {stamp}")
    elif m.photo:
        kind, fid, default = "photo", m.photo[-1].file_id, f"Rasm {stamp}"
    elif m.video:
        kind, fid, default = "video", m.video.file_id, (getattr(m.video, "file_name", None) or f"Video {stamp}")
    elif m.audio:
        kind, fid, default = "audio", m.audio.file_id, (m.audio.title or m.audio.file_name or f"Audio {stamp}")
    else:
        return None, None, None, None
    if cap:
        title, rest = split_title(cap)
    else:
        title, rest = default[:100], ""
    return kind, fid, title, rest


# =============================================================== HOLATLAR
class Login(StatesGroup):
    login = State()
    password = State()


class Apply(StatesGroup):
    name = State()
    subject_text = State()


class AddMat(StatesGroup):
    content = State()


class RenameMat(StatesGroup):
    title = State()


class ChangePw(StatesGroup):
    old = State()
    new = State()


class Search(StatesGroup):
    query = State()


class AdminAdd(StatesGroup):
    name = State()
    subject_text = State()


class AdminSubj(StatesGroup):
    new = State()
    rename = State()


LOGIN_KB = ikb(
    [("👤 Mening profilim yo'q", "guest")],
    [("🎓 Ustoz bo'lish uchun ariza", "apply")],
)
CANCEL_KB = ikb([("❌ Bekor qilish", "go:home")])

r = Router()
r.message.filter(F.chat.type == "private")
r.callback_query.filter(F.message.chat.type == "private")


# =============================================================== MAJBURIY OBUNA
SUB_CACHE = {}          # tg_id -> obuna tasdiqlangan vaqt tugashi
CHANNEL_WARNED = False  # kanal xatosi haqida adminlarga bir marta xabar beriladi
SUB_TEXT = (
    "📢 Botdan foydalanish uchun avval maktabimiz kanaliga obuna bo'ling:\n\n"
    f"{REQUIRED_CHANNEL}\n\n"
    "Obuna bo'lgach, «✅ Obuna bo'ldim» tugmasini bosing."
)


def sub_kb(back: bool = False):
    rows = [
        [InlineKeyboardButton(text="📢 Kanalga o'tish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="sub:check")],
    ]
    if back:
        rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="a:chan")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_channel_problem(bot: Bot):
    global CHANNEL_WARNED
    if CHANNEL_WARNED:
        return
    CHANNEL_WARNED = True
    for admin_id in list(ADMINS):
        try:
            await bot.send_message(
                admin_id,
                f"⚠️ Obunani tekshirib bo'lmadi.\nBotni {REQUIRED_CHANNEL} kanaliga ADMINISTRATOR qilib qo'shing.\n"
                "Shu paytgacha foydalanuvchilar kanalsiz ham kira oladi.",
            )
        except Exception:
            pass


async def check_subscription(bot: Bot, uid: int):
    """True - obuna bo'lgan, False - yo'q, None - tekshirib bo'lmadi."""
    if SUB_CACHE.get(uid, 0) > time.time():
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logging.warning("Obunani tekshirib bo'lmadi: %s", e)
        await notify_channel_problem(bot)
        return None
    except TelegramNetworkError:
        return None
    ok = member.status in ("creator", "administrator", "member") or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )
    if ok:
        SUB_CACHE[uid] = time.time() + 600  # 10 daqiqa eslab qoladi
    return ok


class SubscriptionMiddleware(BaseMiddleware):
    """Admindan boshqa hamma kanalga obuna bo'lmaguncha botdan foydalana olmaydi."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not REQUIRED_CHANNEL or not user or user.id in ADMINS:
            return await handler(event, data)
        if isinstance(event, Message):
            if event.chat.type != "private" or (event.text or "").startswith("/admin"):
                return await handler(event, data)
        elif getattr(event, "data", None) == "sub:check":
            return await handler(event, data)
        if await check_subscription(event.bot, user.id) is False:
            await show(event, SUB_TEXT, sub_kb())
            return None
        return await handler(event, data)


r.message.outer_middleware(SubscriptionMiddleware())
r.callback_query.outer_middleware(SubscriptionMiddleware())


# =============================================================== BOSH EKRANLAR
async def ask_login(target, state: FSMContext, note: str = ""):
    await state.clear()
    await state.set_state(Login.login)
    await show(
        target,
        f"{note}Assalomu alaykum! 👋\n\n"
        "🎓 Ustoz bo'lsangiz — loginingizni yozib yuboring.\n"
        "👤 Profilingiz bo'lmasa — pastdagi tugmani bosing.",
        LOGIN_KB,
    )


async def teacher_menu(target, t, note: str = ""):
    n = count("materials", "WHERE teacher_id = ?", t["id"])
    await show(
        target,
        f"{note}🏠 Ustoz paneli\n\n👤 {tname(t)}\n📘 Fan: {t['subject']}\n📚 Materiallaringiz: {n} ta",
        ikb(
            [("➕ Material qo'shish", "t:add")],
            [("📚 Materiallarim", "t:list")],
            [("👁 O'quvchi ko'rinishi", "u:menu")],
            [("🔑 Parolni o'zgartirish", "t:pw"), ("🚪 Chiqish", "t:out")],
        ),
    )


async def admin_menu(target, note: str = ""):
    pending = count("applications", "WHERE status = 'pending'")
    await show(
        target,
        f"{note}🛠 Admin paneli\n\n"
        f"👥 Ustozlar: {count('teachers')}\n📘 Fanlar: {count('subjects')}\n📚 Materiallar: {count('materials')}",
        ikb(
            [("➕ Ustoz qo'shish", "a:add")],
            [("👥 Ustozlar", "a:teachers"), (f"📥 Arizalar ({pending})", "a:apps")],
            [("📘 Fanlar", "a:subjects"), ("📊 Statistika", "a:stats")],
            [("📢 Kanal holati", "a:chan")],
            [("👁 Foydalanuvchi ko'rinishi", "u:menu")],
        ),
    )


async def home(target, state: FSMContext, note: str = ""):
    await state.clear()
    uid = target.from_user.id
    if uid in ADMINS:
        await admin_menu(target, note)
    elif get_teacher(uid):
        await teacher_menu(target, get_teacher(uid), note)
    else:
        await ask_login(target, state, note)


async def need_teacher(c: CallbackQuery, state: FSMContext):
    t = get_teacher(c.from_user.id)
    if not t:
        await ask_login(c, state)
    return t


async def need_admin(c: CallbackQuery) -> bool:
    if c.from_user.id in ADMINS:
        return True
    await c.answer("Bu bo'lim faqat adminlar uchun.", show_alert=True)
    return False


# =============================================================== BUYRUQLAR
@r.message(CommandStart())
@r.message(Command("cancel"))
async def cmd_start(m: Message, state: FSMContext):
    await home(m, state)


@r.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Sizning Telegram ID raqamingiz: {m.from_user.id}")


@r.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "ℹ️ Yordam\n\n"
        "👤 O'quvchi: «Mening profilim yo'q» tugmasini bosing, fan va sinfni tanlang.\n"
        "🎓 Ustoz: login-parol bilan kiring yoki «Ustoz bo'lish uchun ariza» yuboring.\n\n"
        "/start — bosh menyu\n/cancel — bekor qilish"
    )


@r.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    uid = m.from_user.id
    if uid in ADMINS:
        return await home(m, state)
    parts = (m.text or "").split()
    if len(parts) == 2 and not ADMINS and parts[1] == SETUP_CODE:
        run("INSERT OR IGNORE INTO admins(tg_id) VALUES(?)", uid)
        ADMINS.add(uid)
        return await home(m, state, note="✅ Siz admin bo'ldingiz!\n\n")
    await m.answer("Bu buyruq faqat adminlar uchun.")


# =============================================================== KIRISH
@r.callback_query(F.data.in_({"go:home", "t:menu", "a:menu"}))
async def cb_home(c: CallbackQuery, state: FSMContext):
    await home(c, state)


@r.callback_query(F.data == "sub:check")
async def cb_sub_check(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    if REQUIRED_CHANNEL and uid not in ADMINS:
        if await check_subscription(c.bot, uid) is False:
            return await c.answer("Siz hali obuna bo'lmagansiz. Avval kanalga obuna bo'ling.", show_alert=True)
    await home(c, state, note="✅ Rahmat! Endi botdan foydalanishingiz mumkin.\n\n")


@r.callback_query(F.data == "login")
async def cb_login(c: CallbackQuery, state: FSMContext):
    await ask_login(c, state)


@r.message(Login.login, F.text, ~F.text.startswith("/"))
async def got_login(m: Message, state: FSMContext):
    await state.update_data(login=m.text.strip().lower())
    await state.set_state(Login.password)
    await m.answer("🔑 Endi parolingizni yozing:", reply_markup=LOGIN_KB)


@r.message(Login.password, F.text, ~F.text.startswith("/"))
async def got_password(m: Message, state: FSMContext):
    uid = m.from_user.id
    await safe_delete(m)  # parol chatda qolmasin
    wait = blocked_seconds(uid)
    if wait:
        return await m.answer(f"⏳ Juda ko'p xato urinish. {wait} soniyadan keyin qayta urining.", reply_markup=LOGIN_KB)
    data = await state.get_data()
    teacher = q1("SELECT * FROM teachers WHERE login = ?", data.get("login", ""))
    if teacher and check_pw(m.text.strip(), teacher["pw"]):
        FAILS.pop(uid, None)
        run("INSERT OR REPLACE INTO sessions(tg_id, teacher_id) VALUES(?, ?)", uid, teacher["id"])
        await state.clear()
        return await teacher_menu(m, get_teacher(uid), note="✅ Xush kelibsiz!\n\n")
    register_fail(uid)
    await state.set_state(Login.login)
    await m.answer(
        "❌ Login yoki parol noto'g'ri.\n\nLoginni qayta yozing.\n"
        "Parolni unutgan bo'lsangiz, administratorga murojaat qiling.",
        reply_markup=LOGIN_KB,
    )


@r.callback_query(F.data == "t:out")
async def cb_t_out(c: CallbackQuery, state: FSMContext):
    run("DELETE FROM sessions WHERE tg_id = ?", c.from_user.id)
    await ask_login(c, state, note="Siz tizimdan chiqdingiz.\n\n")


# =============================================================== USTOZLIKKA ARIZA
def subject_buttons(prefix: str):
    rows = [[(s["name"], f"{prefix}{s['id']}")] for s in q("SELECT id, name FROM subjects ORDER BY name")]
    rows.append([("✏️ Boshqa fan (o'zim yozaman)", f"{prefix}new")])
    return rows


def app_text(a) -> str:
    who = f"@{a['username']}" if a["username"] else f"ID {a['tg_id']}"
    return f"🆕 Ustozlik arizasi\n\n👤 Ism: {a['full_name']}\n📘 Fan: {a['subject_name']}\n💬 Telegram: {who}"


def app_kb(app_id: int, back: bool = False):
    rows = [[("✅ Qabul qilish", f"apok:{app_id}"), ("❌ Rad etish", f"apno:{app_id}")]]
    if back:
        rows.append([("⬅️ Arizalar", "a:apps")])
    return ikb(*rows)


@r.callback_query(F.data == "apply")
async def cb_apply(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    if get_teacher(uid) or uid in ADMINS:
        return await home(c, state)
    if q1("SELECT 1 FROM applications WHERE tg_id = ? AND status = 'pending'", uid):
        return await show(
            c,
            "⏳ Arizangiz yuborilgan, admin ko'rib chiqmoqda.\nNatija shu yerga keladi.",
            ikb([("👤 Darsliklarni ko'rish", "guest")]),
        )
    await state.clear()
    await state.set_state(Apply.name)
    await show(c, "🎓 Ustoz bo'lish uchun ariza\n\n1/2: Ism-familiyangizni yozing:", ikb([("⬅️ Orqaga", "go:home")]))


@r.message(Apply.name, F.text, ~F.text.startswith("/"))
async def apply_name(m: Message, state: FSMContext):
    name = clean_name(m.text)
    if len(name) < 3:
        return await m.answer("Ism-familiyani to'liqroq yozing.", reply_markup=CANCEL_KB)
    await state.update_data(name=name)
    await m.answer(
        "2/2: Qaysi fandan dars berasiz? Tanlang:",
        reply_markup=ikb(*subject_buttons("as:"), [("❌ Bekor qilish", "go:home")]),
    )


async def submit_application(bot: Bot, user, name: str, subject: str) -> int:
    app_id = run(
        "INSERT INTO applications(tg_id, username, full_name, subject_name) VALUES(?,?,?,?)",
        user.id, getattr(user, "username", None), name, subject,
    ).lastrowid
    a = q1("SELECT * FROM applications WHERE id = ?", app_id)
    for admin_id in list(ADMINS):
        try:
            await bot.send_message(admin_id, app_text(a), reply_markup=app_kb(app_id))
        except Exception:
            logging.warning("Adminga (%s) ariza yuborib bo'lmadi", admin_id)
    return app_id


APPLIED_TEXT = (
    "✅ Arizangiz yuborildi!\n\nAdmin ko'rib chiqadi. Qabul qilinsangiz, login va parol "
    "shu yerga keladi va avtomatik kirasiz."
)


@r.callback_query(F.data.startswith("as:"))
async def cb_apply_subject(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    if not name:
        return await home(c, state)
    key = c.data.split(":")[1]
    if key == "new":
        await state.set_state(Apply.subject_text)
        return await show(c, "Fan nomini yozing (masalan: Biologiya):", CANCEL_KB)
    s = q1("SELECT name FROM subjects WHERE id = ?", int(key))
    if not s:
        return await c.answer("Fan topilmadi", show_alert=True)
    await submit_application(c.bot, c.from_user, name, s["name"])
    await state.clear()
    await show(c, APPLIED_TEXT, ikb([("👤 Darsliklarni ko'rish", "guest")]))


@r.message(Apply.subject_text, F.text, ~F.text.startswith("/"))
async def apply_subject_text(m: Message, state: FSMContext):
    subject = clean_name(m.text)
    data = await state.get_data()
    if len(subject) < 2 or not data.get("name"):
        return await m.answer("Fan nomini to'g'ri yozing.", reply_markup=CANCEL_KB)
    await submit_application(m.bot, m.from_user, data["name"], subject)
    await state.clear()
    await m.answer(APPLIED_TEXT, reply_markup=ikb([("👤 Darsliklarni ko'rish", "guest")]))


# =============================================================== ADMIN: ARIZALAR
@r.callback_query(F.data == "a:apps")
async def cb_a_apps(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    await state.clear()
    apps = q("SELECT * FROM applications WHERE status = 'pending' ORDER BY id LIMIT 30")
    if not apps:
        return await show(c, "📥 Yangi arizalar yo'q.", ikb([("⬅️ Admin paneli", "a:menu")]))
    rows = [[(f"📥 {a['full_name']} · {a['subject_name']}"[:60], f"ap:{a['id']}")] for a in apps]
    rows.append([("⬅️ Admin paneli", "a:menu")])
    await show(c, f"📥 Kutilayotgan arizalar: {len(apps)} ta", ikb(*rows))


@r.callback_query(F.data.startswith("ap:"))
async def cb_ap(c: CallbackQuery):
    if not await need_admin(c):
        return
    a = q1("SELECT * FROM applications WHERE id = ?", int(c.data.split(":")[1]))
    if not a or a["status"] != "pending":
        return await show(c, "Bu ariza allaqachon ko'rib chiqilgan.", ikb([("⬅️ Arizalar", "a:apps")]))
    await show(c, app_text(a), app_kb(a["id"], back=True))


@r.callback_query(F.data.startswith("apok:"))
async def cb_ap_ok(c: CallbackQuery):
    if not await need_admin(c):
        return
    a = q1("SELECT * FROM applications WHERE id = ?", int(c.data.split(":")[1]))
    if not a or a["status"] != "pending":
        return await c.answer("Bu ariza allaqachon ko'rib chiqilgan.", show_alert=True)
    tid, login, pw, sname = create_teacher(a["full_name"], a["subject_name"])
    run("UPDATE applications SET status = 'approved' WHERE id = ?", a["id"])
    run("INSERT OR REPLACE INTO sessions(tg_id, teacher_id) VALUES(?, ?)", a["tg_id"], tid)
    delivered = True
    try:
        await c.bot.send_message(
            a["tg_id"],
            cred_text("🎉 Arizangiz qabul qilindi!\nEndi siz ustozsiz.", a["full_name"], sname, login, pw)
            + "\n\nBotga boshqa qurilmadan kirsangiz ham shu login-parol kerak bo'ladi.",
            reply_markup=ikb([("🏠 Ustoz paneliga o'tish", "go:home")]),
            parse_mode="HTML",
        )
    except Exception:
        delivered = False
    if delivered:
        await show(
            c, f"✅ Qabul qilindi: {a['full_name']} ({sname})\nLogin va parol ustozga yuborildi.",
            ikb([("📥 Arizalar", "a:apps"), ("🛠 Admin paneli", "a:menu")]),
        )
    else:
        await show(
            c,
            cred_text("✅ Qabul qilindi, lekin ustozga xabar yetmadi.\nMa'lumotni o'zingiz yuboring:", a["full_name"], sname, login, pw),
            ikb([("🛠 Admin paneli", "a:menu")]),
            parse_mode="HTML",
        )


@r.callback_query(F.data.startswith("apno:"))
async def cb_ap_no(c: CallbackQuery):
    if not await need_admin(c):
        return
    a = q1("SELECT * FROM applications WHERE id = ?", int(c.data.split(":")[1]))
    if not a or a["status"] != "pending":
        return await c.answer("Bu ariza allaqachon ko'rib chiqilgan.", show_alert=True)
    run("UPDATE applications SET status = 'rejected' WHERE id = ?", a["id"])
    try:
        await c.bot.send_message(a["tg_id"], "❌ Afsus, ustozlik arizangiz qabul qilinmadi.\nSavollar bo'lsa, administratorga murojaat qiling.")
    except Exception:
        pass
    await show(c, f"❌ Rad etildi: {a['full_name']}", ikb([("📥 Arizalar", "a:apps"), ("🛠 Admin paneli", "a:menu")]))


# =============================================================== ADMIN: USTOZ QO'SHISH
@r.callback_query(F.data == "a:add")
async def cb_a_add(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    await state.clear()
    await state.set_state(AdminAdd.name)
    await show(
        c,
        "➕ Yangi ustoz\n\n1/2: Ustozning ism-familiyasini yozing:\n(login va parolni bot o'zi yaratadi)",
        CANCEL_KB,
    )


@r.message(AdminAdd.name, F.text, ~F.text.startswith("/"))
async def admin_add_name(m: Message, state: FSMContext):
    if m.from_user.id not in ADMINS:
        return await home(m, state)
    name = clean_name(m.text)
    if len(name) < 3:
        return await m.answer("Ism-familiyani to'liqroq yozing.", reply_markup=CANCEL_KB)
    await state.update_data(name=name)
    await m.answer(
        f"2/2: {name} qaysi fandan dars beradi? Tanlang:",
        reply_markup=ikb(*subject_buttons("aas:"), [("❌ Bekor qilish", "go:home")]),
    )


async def finish_admin_add(target, state: FSMContext, subject_name: str):
    data = await state.get_data()
    name = data.get("name")
    if not name:
        return await home(target, state)
    _, login, pw, sname = create_teacher(name, subject_name)
    await state.clear()
    await show(
        target,
        cred_text("✅ Ustoz qo'shildi!\nShu ma'lumotni ustozga yuboring:", name, sname, login, pw),
        ikb([("➕ Yana ustoz qo'shish", "a:add")], [("🛠 Admin paneli", "a:menu")]),
        parse_mode="HTML",
    )


@r.callback_query(F.data.startswith("aas:"))
async def cb_admin_add_subject(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    key = c.data.split(":")[1]
    if key == "new":
        await state.set_state(AdminAdd.subject_text)
        return await show(c, "Yangi fan nomini yozing (masalan: Kimyo):", CANCEL_KB)
    s = q1("SELECT name FROM subjects WHERE id = ?", int(key))
    if not s:
        return await c.answer("Fan topilmadi", show_alert=True)
    await finish_admin_add(c, state, s["name"])


@r.message(AdminAdd.subject_text, F.text, ~F.text.startswith("/"))
async def admin_add_subject_text(m: Message, state: FSMContext):
    if m.from_user.id not in ADMINS:
        return await home(m, state)
    subject = clean_name(m.text)
    if len(subject) < 2:
        return await m.answer("Fan nomini to'g'ri yozing.", reply_markup=CANCEL_KB)
    await finish_admin_add(m, state, subject)


# =============================================================== ADMIN: USTOZLAR RO'YXATI
async def render_admin_teachers(c: CallbackQuery, page: int):
    total = count("teachers")
    if total == 0:
        return await show(c, "Hali ustozlar yo'q.", ikb([("➕ Ustoz qo'shish", "a:add")], [("⬅️ Admin paneli", "a:menu")]))
    page = clamp_page(page, total)
    rows_db = q(
        """SELECT t.id, t.login, t.full_name, s.name AS subject,
                  (SELECT COUNT(*) FROM materials x WHERE x.teacher_id = t.id) AS n
           FROM teachers t JOIN subjects s ON s.id = t.subject_id
           ORDER BY s.name, t.full_name LIMIT ? OFFSET ?""",
        PAGE_SIZE, page * PAGE_SIZE,
    )
    rows = [[(f"👨‍🏫 {tname(x)} · {x['subject']} ({x['n']})"[:60], f"at:{x['id']}:{page}")] for x in rows_db]
    rows += pager_row("a:teachers", page, total)
    rows.append([("➕ Ustoz qo'shish", "a:add")])
    rows.append([("⬅️ Admin paneli", "a:menu")])
    await show(c, f"👥 Ustozlar: {total} ta\nBatafsil ko'rish uchun bosing:", ikb(*rows))


@r.callback_query(F.data.startswith("a:teachers"))
async def cb_a_teachers(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    await state.clear()
    parts = c.data.split(":")
    await render_admin_teachers(c, int(parts[2]) if len(parts) > 2 else 0)


@r.callback_query(F.data.startswith("at:"))
async def cb_at(c: CallbackQuery):
    if not await need_admin(c):
        return
    _, tid, page = c.data.split(":")
    t = q1(
        """SELECT t.*, s.name AS subject FROM teachers t JOIN subjects s ON s.id = t.subject_id WHERE t.id = ?""",
        int(tid),
    )
    if not t:
        return await render_admin_teachers(c, int(page))
    n = count("materials", "WHERE teacher_id = ?", t["id"])
    await show(
        c,
        f"👨‍🏫 {tname(t)}\n\n📘 Fan: {t['subject']}\n🔐 Login: {t['login']}\n📚 Materiallar: {n} ta",
        ikb(
            [("🔄 Yangi parol berish", f"ar:{tid}:{page}")],
            [("🗑 Ustozni o'chirish", f"ad:{tid}:{page}")],
            [("⬅️ Ustozlar", f"a:teachers:{page}")],
        ),
    )


@r.callback_query(F.data.startswith("ar:"))
async def cb_ar(c: CallbackQuery):
    if not await need_admin(c):
        return
    _, tid, page = c.data.split(":")
    await show(
        c,
        "🔄 Yangi parol yaratilsinmi?\nEski parol ishlamay qoladi, ustoz tizimdan chiqadi.",
        ikb([("✅ Ha", f"ary:{tid}:{page}"), ("❌ Yo'q", f"at:{tid}:{page}")]),
    )


@r.callback_query(F.data.startswith("ary:"))
async def cb_ary(c: CallbackQuery):
    if not await need_admin(c):
        return
    _, tid, page = c.data.split(":")
    t = q1("SELECT t.*, s.name AS subject FROM teachers t JOIN subjects s ON s.id = t.subject_id WHERE t.id = ?", int(tid))
    if not t:
        return await render_admin_teachers(c, int(page))
    pw = gen_password()
    run("UPDATE teachers SET pw = ? WHERE id = ?", hash_pw(pw), t["id"])
    run("DELETE FROM sessions WHERE teacher_id = ?", t["id"])
    await show(
        c,
        cred_text("✅ Yangi parol yaratildi.\nUstozga yuboring:", tname(t), t["subject"], t["login"], pw),
        ikb([("⬅️ Ustozlar", f"a:teachers:{page}")]),
        parse_mode="HTML",
    )


@r.callback_query(F.data.startswith("ad:"))
async def cb_ad(c: CallbackQuery):
    if not await need_admin(c):
        return
    _, tid, page = c.data.split(":")
    t = q1("SELECT * FROM teachers WHERE id = ?", int(tid))
    if not t:
        return await render_admin_teachers(c, int(page))
    n = count("materials", "WHERE teacher_id = ?", t["id"])
    await show(
        c,
        f"🗑 {tname(t)} o'chirilsinmi?\nUning {n} ta materiali bor. Ularni nima qilamiz?",
        ikb(
            [("🗑 Faqat ustozni (materiallar qoladi)", f"ady:{tid}:k")],
            [("🗑 Ustoz va uning materiallarini", f"ady:{tid}:d")],
            [("❌ Bekor qilish", f"at:{tid}:{page}")],
        ),
    )


@r.callback_query(F.data.startswith("ady:"))
async def cb_ady(c: CallbackQuery):
    if not await need_admin(c):
        return
    _, tid, mode = c.data.split(":")
    tid = int(tid)
    run("DELETE FROM sessions WHERE teacher_id = ?", tid)
    run("DELETE FROM teachers WHERE id = ?", tid)
    if mode == "d":
        run("DELETE FROM materials WHERE teacher_id = ?", tid)
    await show(c, "🗑 Ustoz o'chirildi.", ikb([("👥 Ustozlar", "a:teachers"), ("🛠 Admin paneli", "a:menu")]))


# =============================================================== ADMIN: FANLAR
async def render_subjects(c, note: str = ""):
    subs = q(
        """SELECT s.id, s.name,
                  (SELECT COUNT(*) FROM teachers t WHERE t.subject_id = s.id) AS nt,
                  (SELECT COUNT(*) FROM materials m WHERE m.subject_id = s.id) AS nm
           FROM subjects s ORDER BY s.name"""
    )
    rows = [[(f"📘 {s['name']} · {s['nt']} ustoz · {s['nm']} material"[:60], f"asb:{s['id']}")] for s in subs]
    rows.append([("➕ Yangi fan", "a:subnew")])
    rows.append([("⬅️ Admin paneli", "a:menu")])
    await show(c, f"{note}📘 Fanlar: {len(subs)} ta" if subs else f"{note}Hali fanlar yo'q.", ikb(*rows))


@r.callback_query(F.data == "a:subjects")
async def cb_a_subjects(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    await state.clear()
    await render_subjects(c)


@r.callback_query(F.data == "a:subnew")
async def cb_a_subnew(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    await state.set_state(AdminSubj.new)
    await show(c, "Yangi fan nomini yozing:", ikb([("❌ Bekor qilish", "a:subjects")]))


@r.message(AdminSubj.new, F.text, ~F.text.startswith("/"))
async def admin_subj_new(m: Message, state: FSMContext):
    if m.from_user.id not in ADMINS:
        return await home(m, state)
    name = clean_name(m.text)
    if len(name) < 2:
        return await m.answer("Fan nomini to'g'ri yozing.")
    _, sname = get_or_create_subject(name)
    await state.clear()
    await m.answer(f"✅ Fan qo'shildi: {sname}", reply_markup=ikb([("📘 Fanlar", "a:subjects"), ("🛠 Admin paneli", "a:menu")]))


@r.callback_query(F.data.startswith("asb:"))
async def cb_asb(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    await state.clear()
    sid = int(c.data.split(":")[1])
    s = q1("SELECT * FROM subjects WHERE id = ?", sid)
    if not s:
        return await render_subjects(c)
    nt = count("teachers", "WHERE subject_id = ?", sid)
    nm = count("materials", "WHERE subject_id = ?", sid)
    await show(
        c,
        f"📘 {s['name']}\n\n👨‍🏫 Ustozlar: {nt}\n📚 Materiallar: {nm}",
        ikb(
            [("✏️ Nomini o'zgartirish", f"asr:{sid}")],
            [("🗑 Fanni o'chirish", f"asd:{sid}")],
            [("⬅️ Fanlar", "a:subjects")],
        ),
    )


@r.callback_query(F.data.startswith("asr:"))
async def cb_asr(c: CallbackQuery, state: FSMContext):
    if not await need_admin(c):
        return
    sid = int(c.data.split(":")[1])
    await state.set_state(AdminSubj.rename)
    await state.update_data(sid=sid)
    await show(c, "Fanning yangi nomini yozing:", ikb([("❌ Bekor qilish", f"asb:{sid}")]))


@r.message(AdminSubj.rename, F.text, ~F.text.startswith("/"))
async def admin_subj_rename(m: Message, state: FSMContext):
    if m.from_user.id not in ADMINS:
        return await home(m, state)
    name = clean_name(m.text)
    sid = (await state.get_data()).get("sid")
    if len(name) < 2 or not sid:
        return await m.answer("Fan nomini to'g'ri yozing.")
    try:
        run("UPDATE subjects SET name = ? WHERE id = ?", name, sid)
    except sqlite3.IntegrityError:
        return await m.answer("Bu nomli fan allaqachon bor. Boshqa nom yozing.")
    await state.clear()
    await m.answer(f"✅ Fan nomi o'zgartirildi: {name}", reply_markup=ikb([("📘 Fanlar", "a:subjects")]))


@r.callback_query(F.data.startswith("asd:"))
async def cb_asd(c: CallbackQuery):
    if not await need_admin(c):
        return
    sid = int(c.data.split(":")[1])
    if count("teachers", "WHERE subject_id = ?", sid) or count("materials", "WHERE subject_id = ?", sid):
        return await c.answer("Bu fanda ustoz yoki material bor. Avval ularni o'chiring.", show_alert=True)
    run("DELETE FROM subjects WHERE id = ?", sid)
    await render_subjects(c, note="🗑 Fan o'chirildi.\n\n")


# =============================================================== ADMIN: STATISTIKA
@r.callback_query(F.data == "a:stats")
async def cb_a_stats(c: CallbackQuery):
    if not await need_admin(c):
        return
    top = q(
        """SELECT s.name, COUNT(m.id) AS n FROM subjects s LEFT JOIN materials m ON m.subject_id = s.id
           GROUP BY s.id ORDER BY n DESC LIMIT 8"""
    )
    lines = "\n".join(f"• {x['name']}: {x['n']} ta" for x in top) or "—"
    await show(
        c,
        f"📊 Statistika\n\n👥 Foydalanuvchilar: {count('users')}\n👨‍🏫 Ustozlar: {count('teachers')}\n"
        f"📘 Fanlar: {count('subjects')}\n📚 Materiallar: {count('materials')}\n\nFanlar bo'yicha:\n{lines}",
        ikb([("⬅️ Admin paneli", "a:menu")]),
    )


# =============================================================== ADMIN: KANAL HOLATI
@r.callback_query(F.data == "a:chan")
async def cb_a_chan(c: CallbackQuery):
    if not await need_admin(c):
        return
    back = ikb([("⬅️ Admin paneli", "a:menu")])
    if not REQUIRED_CHANNEL:
        return await show(c, "📢 Majburiy obuna o'chirilgan.", back)
    try:
        me = await c.bot.me()
        st = (await c.bot.get_chat_member(REQUIRED_CHANNEL, me.id)).status
        if st in ("administrator", "creator"):
            bot_line = "✅ Bot kanalda ADMIN. Obuna tekshiruvi ishlaydi."
        else:
            bot_line = f"❌ Bot kanalda admin emas (holati: {st}).\nObuna tekshirilmaydi, hamma kira oladi!"
    except Exception:
        bot_line = "❌ Bot kanalni topa olmayapti.\nKanal nomini va botning kanalga admin qilinganini tekshiring."
    me_sub = await check_subscription(c.bot, c.from_user.id)
    mine = {True: "✅ obuna bo'lgansiz", False: "❌ obuna bo'lmagansiz", None: "❓ aniqlab bo'lmadi"}[me_sub]
    await show(
        c,
        f"📢 Majburiy obuna\n\nKanal: {REQUIRED_CHANNEL}\n{bot_line}\nSiz: {mine}\n\n"
        "ℹ️ Adminlar obunasiz ham kira oladi, shuning uchun sinash uchun boshqa Telegram akkauntdan /start bosing.",
        ikb([("👁 Foydalanuvchi nimani ko'radi", "a:chanprev")], [("⬅️ Admin paneli", "a:menu")]),
    )


@r.callback_query(F.data == "a:chanprev")
async def cb_a_chanprev(c: CallbackQuery):
    if not await need_admin(c):
        return
    await show(c, SUB_TEXT, sub_kb(back=True))


# =============================================================== USTOZ: MATERIAL QO'SHISH
@r.callback_query(F.data == "t:add")
async def cb_t_add(c: CallbackQuery, state: FSMContext):
    if not await need_teacher(c, state):
        return
    await state.clear()
    await show(c, "➕ Qaysi sinf uchun material qo'shasiz?", ikb(*grade_rows("ag:"), [("⬅️ Orqaga", "go:home")]))


@r.callback_query(F.data.startswith("ag:"))
async def cb_add_grade(c: CallbackQuery, state: FSMContext):
    if not await need_teacher(c, state):
        return
    grade = int(c.data.split(":")[1])
    await state.clear()
    await state.set_state(AddMat.content)
    await state.update_data(grade=grade)
    await show(
        c,
        f"📥 {grade}-sinf\n\nMateriallarni yuboring:\n"
        "• 📝 matn — birinchi qator mavzu nomi bo'ladi\n"
        "• 📄 fayl, 🖼 rasm, 🎬 video, 🎧 audio — izohi nom bo'ladi\n\n"
        "Istaganingizcha ko'p yuborishingiz mumkin.\nTugagach «Tugatdim» ni bosing.",
        ikb([("✅ Tugatdim", "t:done")], [("🔄 Boshqa sinf", "t:add")]),
    )


@r.callback_query(F.data == "t:done")
async def cb_t_done(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    await state.clear()
    await teacher_menu(c, t, note="✅ Materiallar saqlandi.\n\n")


@r.message(AddMat.content, lambda m: not (m.text or "").startswith("/"))
async def add_content(m: Message, state: FSMContext):
    t = get_teacher(m.from_user.id)
    if not t:
        return await ask_login(m, state)
    grade = (await state.get_data()).get("grade")
    if not grade:
        return await home(m, state)
    kind, file_id, title, text = parse_content(m)
    if kind is None:
        return await m.answer(
            "Bu turdagi xabar qo'llab-quvvatlanmaydi.\nMatn, fayl, rasm, video yoki audio yuboring.",
            reply_markup=ikb([("✅ Tugatdim", "t:done")]),
        )
    mid = run(
        "INSERT INTO materials(teacher_id, subject_id, grade, title, kind, file_id, text) VALUES(?,?,?,?,?,?,?)",
        t["id"], t["subject_id"], grade, title, kind, file_id, text,
    ).lastrowid
    n = count("materials", "WHERE teacher_id = ? AND grade = ?", t["id"], grade)
    await m.answer(
        f"✅ Saqlandi: {ICON[kind]} {title}\n{grade}-sinf bo'yicha jami: {n} ta\n\nYana yuborishingiz mumkin.",
        reply_markup=ikb([("✏️ Nomini o'zgartirish", f"rn:{mid}")], [("✅ Tugatdim", "t:done")]),
    )


# =============================================================== USTOZ: MATERIALLARIM
@r.callback_query(F.data == "t:list")
async def cb_t_list(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    await state.clear()
    counts = q("SELECT grade, COUNT(*) AS n FROM materials WHERE teacher_id = ? GROUP BY grade ORDER BY grade", t["id"])
    if not counts:
        return await show(c, "Hali material qo'shmagansiz.", ikb([("➕ Material qo'shish", "t:add")], [("🏠 Menyu", "go:home")]))
    rows, row = [], []
    for x in counts:
        row.append((f"{x['grade']}-sinf ({x['n']})", f"tl:{x['grade']}:0"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("🏠 Menyu", "go:home")])
    await show(c, "📚 Materiallarim\nSinfni tanlang:", ikb(*rows))


async def render_tl(c: CallbackQuery, t, grade: int, page: int, note: str = ""):
    total = count("materials", "WHERE teacher_id = ? AND grade = ?", t["id"], grade)
    if total == 0:
        return await show(c, f"{note}Bu sinfda material qolmadi.", ikb([("⬅️ Orqaga", "t:list")]))
    page = clamp_page(page, total)
    mats = q(
        "SELECT id, kind, title FROM materials WHERE teacher_id = ? AND grade = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        t["id"], grade, PAGE_SIZE, page * PAGE_SIZE,
    )
    rows = [[(f"{ICON[x['kind']]} {x['title'][:40]}", f"tm:{x['id']}:{grade}:{page}")] for x in mats]
    rows += pager_row(f"tl:{grade}", page, total)
    rows.append([("⬅️ Orqaga", "t:list")])
    await show(c, f"{note}{grade}-sinf · {total} ta material\nBirini tanlang:", ikb(*rows))


@r.callback_query(F.data.startswith("tl:"))
async def cb_tl(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    await state.clear()
    _, g, page = c.data.split(":")
    await render_tl(c, t, int(g), int(page))


@r.callback_query(F.data.startswith("tm:"))
async def cb_tm(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    await state.clear()
    _, mid, g, page = c.data.split(":")
    x = q1("SELECT * FROM materials WHERE id = ? AND teacher_id = ?", int(mid), t["id"])
    if not x:
        return await render_tl(c, t, int(g), int(page))
    await show(
        c,
        f"{ICON[x['kind']]} {x['title']}\n\n{x['grade']}-sinf · {KIND[x['kind']]}\nQo'shilgan: {str(x['created'])[:16]}",
        ikb(
            [("👁 Ko'rish", f"tv:{mid}:{g}:{page}")],
            [("✏️ Nomini o'zgartirish", f"rn:{mid}")],
            [("🗑 O'chirish", f"dm:{mid}:{g}:{page}")],
            [("⬅️ Orqaga", f"tl:{g}:{page}")],
        ),
    )


@r.callback_query(F.data.startswith("tv:"))
async def cb_tv(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    _, mid, g, page = c.data.split(":")
    x = q1(MAT_SQL, int(mid))
    if not x or x["teacher_id"] != t["id"]:
        return await c.answer("Material topilmadi.", show_alert=True)
    await c.answer()
    try:
        await send_material(c.bot, c.from_user.id, x, ikb([("⬅️ Orqaga", f"tm:{mid}:{g}:{page}")]))
    except TelegramBadRequest:
        await c.bot.send_message(c.from_user.id, "⚠️ Materialni yuborib bo'lmadi. Uni qayta yuklang.")


@r.callback_query(F.data.startswith("rn:"))
async def cb_rn(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    mid = int(c.data.split(":")[1])
    x = q1("SELECT title FROM materials WHERE id = ? AND teacher_id = ?", mid, t["id"])
    if not x:
        return await c.answer("Material topilmadi.", show_alert=True)
    await state.set_state(RenameMat.title)
    await state.update_data(mid=mid)
    await show(c, f"✏️ Hozirgi nom:\n{x['title']}\n\nYangi nomni yozing:", CANCEL_KB)


@r.message(RenameMat.title, F.text, ~F.text.startswith("/"))
async def rename_title(m: Message, state: FSMContext):
    t = get_teacher(m.from_user.id)
    if not t:
        return await ask_login(m, state)
    mid = (await state.get_data()).get("mid")
    title = " ".join(m.text.split())[:100]
    x = q1("SELECT grade FROM materials WHERE id = ? AND teacher_id = ?", mid, t["id"]) if mid else None
    if not x or not title:
        await state.clear()
        return await home(m, state)
    run("UPDATE materials SET title = ? WHERE id = ?", title, mid)
    await state.clear()
    await m.answer(
        f"✅ Nom o'zgartirildi:\n{title}",
        reply_markup=ikb(
            [("➕ Yana material qo'shish", f"ag:{x['grade']}")],
            [("📚 Materiallarim", "t:list")],
            [("🏠 Menyu", "go:home")],
        ),
    )


@r.callback_query(F.data.startswith("dm:"))
async def cb_del_ask(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    _, mid, g, page = c.data.split(":")
    x = q1("SELECT title FROM materials WHERE id = ? AND teacher_id = ?", int(mid), t["id"])
    if not x:
        return await render_tl(c, t, int(g), int(page))
    await show(
        c,
        f"🗑 O'chirilsinmi?\n\n{x['title']}",
        ikb([("✅ Ha, o'chirish", f"dy:{mid}:{g}:{page}"), ("❌ Yo'q", f"tm:{mid}:{g}:{page}")]),
    )


@r.callback_query(F.data.startswith("dy:"))
async def cb_del_yes(c: CallbackQuery, state: FSMContext):
    t = await need_teacher(c, state)
    if not t:
        return
    _, mid, g, page = c.data.split(":")
    run("DELETE FROM materials WHERE id = ? AND teacher_id = ?", int(mid), t["id"])
    await render_tl(c, t, int(g), int(page), note="🗑 O'chirildi.\n\n")


# =============================================================== USTOZ: PAROL
@r.callback_query(F.data == "t:pw")
async def cb_t_pw(c: CallbackQuery, state: FSMContext):
    if not await need_teacher(c, state):
        return
    await state.clear()
    await state.set_state(ChangePw.old)
    await show(c, "🔑 Hozirgi parolingizni yozing:", CANCEL_KB)


@r.message(ChangePw.old, F.text, ~F.text.startswith("/"))
async def pw_old(m: Message, state: FSMContext):
    await safe_delete(m)
    t = get_teacher(m.from_user.id)
    if not t:
        return await ask_login(m, state)
    if not check_pw(m.text.strip(), t["pw"]):
        return await m.answer("❌ Parol noto'g'ri. Qayta yozing:", reply_markup=CANCEL_KB)
    await state.set_state(ChangePw.new)
    await m.answer(f"✅ To'g'ri. Endi yangi parolni yozing (kamida {MIN_PW_LEN} belgi):", reply_markup=CANCEL_KB)


@r.message(ChangePw.new, F.text, ~F.text.startswith("/"))
async def pw_new(m: Message, state: FSMContext):
    await safe_delete(m)
    t = get_teacher(m.from_user.id)
    if not t:
        return await ask_login(m, state)
    new = m.text.strip()
    if len(new) < MIN_PW_LEN:
        return await m.answer(f"Parol kamida {MIN_PW_LEN} belgi bo'lishi kerak. Qayta yozing:", reply_markup=CANCEL_KB)
    run("UPDATE teachers SET pw = ? WHERE id = ?", hash_pw(new), t["id"])
    await state.clear()
    await m.answer("✅ Parol o'zgartirildi.", reply_markup=ikb([("🏠 Menyu", "go:home")]))


# =============================================================== O'QUVCHI (PROFILSIZ FOYDALANUVCHI)
@r.callback_query(F.data.in_({"guest", "u:menu"}))
async def cb_guest(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    subs = q(
        """SELECT s.id, s.name, COUNT(m.id) AS n
           FROM subjects s LEFT JOIN materials m ON m.subject_id = s.id
           GROUP BY s.id ORDER BY s.name"""
    )
    rows = [[(f"📘 {s['name']} ({s['n']})", f"s:{s['id']}")] for s in subs]
    rows.append([("🎒 Sinf bo'yicha", "u:bygrade"), ("🔍 Qidirish", "u:search")])
    if uid in ADMINS:
        rows.append([("🛠 Admin paneli", "go:home")])
    elif get_teacher(uid):
        rows.append([("🏠 Ustoz paneli", "go:home")])
    else:
        rows.append([("🔐 Ustoz kirishi", "login")])
        rows.append([("🎓 Ustoz bo'lish uchun ariza", "apply")])
    text = "📚 Fanni tanlang:" if subs else "Hozircha fanlar qo'shilmagan."
    await show(c, text, ikb(*rows))


@r.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@r.callback_query(F.data == "u:bygrade")
async def cb_bygrade(c: CallbackQuery, state: FSMContext):
    await state.clear()
    counts = {x["grade"]: x["n"] for x in q("SELECT grade, COUNT(*) AS n FROM materials GROUP BY grade")}
    await show(c, "🎒 Nechanchi sinfdasiz?", ikb(*grade_rows("ug:", counts), [("⬅️ Orqaga", "u:menu")]))


@r.callback_query(F.data.startswith("ug:"))
async def cb_ug(c: CallbackQuery):
    grade = int(c.data.split(":")[1])
    subs = q(
        """SELECT s.id, s.name, COUNT(m.id) AS n FROM subjects s
           JOIN materials m ON m.subject_id = s.id AND m.grade = ?
           GROUP BY s.id ORDER BY s.name""",
        grade,
    )
    back = [("⬅️ Orqaga", "u:bygrade")]
    if not subs:
        return await show(c, f"{grade}-sinf bo'yicha hozircha material yo'q.", ikb(back))
    rows = [[(f"📘 {s['name']} ({s['n']})", f"g:{s['id']}:{grade}")] for s in subs]
    rows.append(back)
    await show(c, f"{grade}-sinf. Fanni tanlang:", ikb(*rows))


@r.callback_query(F.data.startswith("s:"))
async def cb_subject(c: CallbackQuery, state: FSMContext):
    await state.clear()
    sid = int(c.data.split(":")[1])
    s = q1("SELECT name FROM subjects WHERE id = ?", sid)
    if not s:
        return await c.answer("Fan topilmadi", show_alert=True)
    counts = {
        x["grade"]: x["n"]
        for x in q("SELECT grade, COUNT(*) AS n FROM materials WHERE subject_id = ? GROUP BY grade", sid)
    }
    rows = grade_rows(f"g:{sid}:", counts) + [[("⬅️ Orqaga", "u:menu")]]
    await show(c, f"📘 {s['name']}\nSinfni tanlang:", ikb(*rows))


@r.callback_query(F.data.startswith("g:"))
async def cb_grade(c: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = c.data.split(":")
    sid, grade = int(parts[1]), int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    back = [("⬅️ Orqaga", f"s:{sid}")]
    total = count("materials", "WHERE subject_id = ? AND grade = ?", sid, grade)
    if total == 0:
        return await show(c, f"{grade}-sinf bo'yicha hozircha material yo'q.", ikb(back))
    page = clamp_page(page, total)
    mats = q(
        "SELECT id, kind, title FROM materials WHERE subject_id = ? AND grade = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        sid, grade, PAGE_SIZE, page * PAGE_SIZE,
    )
    rows = [[(f"{ICON[x['kind']]} {x['title'][:45]}", f"m:{x['id']}:{page}")] for x in mats]
    rows += pager_row(f"g:{sid}:{grade}", page, total)
    rows.append(back)
    await show(c, f"{grade}-sinf materiallari ({total} ta):", ikb(*rows))


@r.callback_query(F.data.startswith("m:"))
async def cb_material(c: CallbackQuery):
    parts = c.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    m = q1(MAT_SQL, int(parts[1]))
    if not m:
        return await c.answer("Material topilmadi (o'chirilgan bo'lishi mumkin).", show_alert=True)
    back = ikb(
        [("⬅️ Ro'yxatga qaytish", f"g:{m['subject_id']}:{m['grade']}:{page}")],
        [("🏠 Bosh menyu", "u:menu")],
    )
    await c.answer()
    try:
        await send_material(c.bot, c.from_user.id, m, back)
    except TelegramBadRequest:
        await c.bot.send_message(
            c.from_user.id, "⚠️ Materialni yuborib bo'lmadi. Ustoz uni qayta yuklashi kerak.", reply_markup=back
        )


# ---- qidiruv
@r.callback_query(F.data == "u:search")
async def cb_search(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Search.query)
    await show(c, "🔍 Nimani qidiramiz?\nMavzu, fan yoki so'z yozing:", ikb([("⬅️ Orqaga", "u:menu")]))


@r.message(Search.query, F.text, ~F.text.startswith("/"))
async def do_search(m: Message, state: FSMContext):
    words = m.text.casefold().split()
    found = []
    for x in q(
        """SELECT m.id, m.title, m.grade, m.text, s.name AS subject
           FROM materials m JOIN subjects s ON s.id = m.subject_id ORDER BY m.id DESC"""
    ):
        hay = f"{x['title']} {x['text'] or ''} {x['subject']} {x['grade']}-sinf".casefold()
        if all(w in hay for w in words):
            found.append(x)
    await state.clear()
    again, menu = [("🔍 Qayta qidirish", "u:search")], [("🏠 Bosh menyu", "u:menu")]
    if not found:
        return await m.answer("Hech narsa topilmadi 😕\nBoshqa so'z bilan urinib ko'ring.", reply_markup=ikb(again, menu))
    shown = found[:30]
    rows = [[(f"{x['subject']} · {x['grade']}-sinf · {x['title']}"[:60], f"m:{x['id']}:0")] for x in shown]
    rows += [again, menu]
    extra = f"\n(Dastlabki {len(shown)} tasi ko'rsatildi)" if len(found) > len(shown) else ""
    await m.answer(f"Topildi: {len(found)} ta{extra}", reply_markup=ikb(*rows))


@r.message(Search.query)
async def search_bad(m: Message):
    await m.answer("Iltimos, qidiruv so'zini matn ko'rinishida yozing.")


# =============================================================== QOLGAN XABARLAR
@r.message(StateFilter(None))
async def fallback(m: Message, state: FSMContext):
    await home(m, state)


# =============================================================== MIDDLEWARE / XATOLAR
class TouchUser(BaseMiddleware):
    """Foydalanuvchilarni statistika uchun yozib boradi."""

    def __init__(self):
        self.seen = set()

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and u.id not in self.seen:
            self.seen.add(u.id)
            try:
                run("INSERT OR IGNORE INTO users(tg_id, name) VALUES(?, ?)", u.id, u.full_name)
            except Exception:
                logging.exception("users jadvaliga yozib bo'lmadi")
        return await handler(event, data)


async def on_error(event: ErrorEvent):
    logging.error("Xatolik: %s", event.exception, exc_info=event.exception)
    upd = event.update
    try:
        if upd.callback_query:
            await upd.callback_query.answer("Xatolik yuz berdi. Qayta urinib ko'ring.", show_alert=True)
        elif upd.message:
            await upd.message.answer("⚠️ Xatolik yuz berdi. /start ni bosib qayta urinib ko'ring.")
    except Exception:
        pass
    return True


# =============================================================== ISHGA TUSHIRISH
async def connect_bot():
    """Tokenni tekshiradi; yo'q yoki noto'g'ri bo'lsa, terminalda so'raydi va eslab qoladi."""
    global TOKEN
    for _ in range(5):
        if not TOKEN:
            print("\n" + "=" * 60)
            print("Bot tokeni kerak. Uni Telegramda @BotFather dan oling:")
            print("  /newbot  yoki  /mybots -> botingiz -> API Token")
            print("=" * 60)
            try:
                TOKEN = clean_token(await asyncio.to_thread(input, "Tokenni shu yerga qo'ying va Enter bosing: "))
            except (EOFError, KeyboardInterrupt):
                return None
        if not TOKEN_RE.match(TOKEN):
            print("\nToken formati noto'g'ri. U shunday ko'rinadi: 123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
            TOKEN = ""
            continue
        try:
            bot = Bot(TOKEN)
        except Exception as e:
            print(f"\nToken qabul qilinmadi: {e}")
            TOKEN = ""
            continue
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            print("\nTelegram bu tokenni qabul qilmadi. Tokenni to'liq nusxalaganingizni tekshiring.")
            await bot.session.close()
            TOKEN = ""
            continue
        except TelegramNetworkError as e:
            print(f"\nTelegramga ulanib bo'lmadi: {e}\nInternet yoki VPN'ni tekshiring.")
            await bot.session.close()
            return None
        cfg = read_config()
        if cfg.get("token") != TOKEN:
            cfg["token"] = TOKEN
            write_config(cfg)
        return bot, me
    print("\nBir necha marta urinildi. Tokenni tekshirib, qayta ishga tushiring.")
    return None


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    res = await connect_bot()
    if not res:
        return
    bot, me = res

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(TouchUser())
    dp.errors.register(on_error)
    dp.include_router(r)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Bosh menyu"),
                BotCommand(command="cancel", description="Amalni bekor qilish"),
                BotCommand(command="help", description="Yordam"),
            ]
        )
        if REQUIRED_CHANNEL:
            try:
                st = (await bot.get_chat_member(REQUIRED_CHANNEL, me.id)).status
                ok_admin = st in ("administrator", "creator")
            except Exception:
                ok_admin = False
            if not ok_admin:
                print("\n" + "!" * 60)
                print(f"DIQQAT: bot {REQUIRED_CHANNEL} kanalida ADMIN emas.")
                print("Obunani tekshirish ishlashi uchun botni kanalga administrator qilib qo'shing:")
                print("  Kanal -> Sozlamalar -> Administratorlar -> Administrator qo'shish -> botingiz")
                print("!" * 60)
        print(f"\nBot ishga tushdi: https://t.me/{me.username}")
        if not ADMINS:
            print("\n" + "=" * 60)
            print("ADMIN BO'LISH UCHUN botingizga shuni yuboring:")
            print(f"        /admin {SETUP_CODE}")
            print("=" * 60)
        print("\nTo'xtatish uchun Ctrl+C bosing.\n")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot to'xtatildi.")
