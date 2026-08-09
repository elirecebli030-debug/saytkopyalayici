import asyncio
import logging
import os
import re
import shutil
import time
import urllib.request
import uuid
import zipfile
from urllib.parse import urlparse, urljoin

import asyncpg
from telegram import (
    Update,
    LabeledPrice,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# ============ TƏNZİMLƏMƏLƏR ============
BO_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit(
        "XƏTA: BOT_TOKEN environment variable təyin olunmayıb. "
        "Railway/Termux-da BOT_TOKEN dəyişənini @BotFather-dan aldığınız token ilə əlavə edin."
    )

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise SystemExit(
        "XƏTA: DATABASE_URL environment variable təyin olunmayıb. "
        "Railway-də PostgreSQL əlavə edib onun Connection URL-ni DATABASE_URL kimi qoyun."
    )
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

STARS_PRICE = 25                   # Telegram Stars miqdarı
CLONE_BASE_DIR = "clones"          # müvəqqəti fayllar üçün (zip mərhələsi)
CACHE_BASE_DIR = "site_cache"       # saytların qalıcı fayl keşi (domen başına bir qovluq)
WGET_TIMEOUT_SEC = int(os.environ.get("WGET_TIMEOUT_SEC", "1800"))  # default 30 dəqiqə
PARALLEL_WORKERS = int(os.environ.get("PARALLEL_WORKERS", "4"))
ANIMATION_INTERVAL_SEC = 1.4

SPECIAL_USER_ID = 8133937162
ADMIN_IDS = {SPECIAL_USER_ID}
FREE_USER_IDS = {SPECIAL_USER_ID}

LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "-1003909741389")

# Məcburi kanal-üzvlük
CHANNEL_INVITE_LINK = os.environ.get("CHANNEL_INVITE_LINK", "https://t.me/+ZLN6RXwRdj4zYWJi")
# ⚠️ Rəqəmsal chat id (məs: -1001234567890) — boş qalarsa yoxlama AVTOMATİK BAĞLI olur
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
# ========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.makedirs(CLONE_BASE_DIR, exist_ok=True)
os.makedirs(CACHE_BASE_DIR, exist_ok=True)

# Eyni domenin paralel iki dəfə klonlanmasının qarşısını alan kilidlər
_DOMAIN_LOCKS: dict[str, asyncio.Lock] = {}


def get_domain_lock(domain: str) -> asyncio.Lock:
    lock = _DOMAIN_LOCKS.get(domain)
    if lock is None:
        lock = asyncio.Lock()
        _DOMAIN_LOCKS[domain] = lock
    return lock


def cache_dir_for(domain: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9.-]", "_", domain)
    return os.path.join(CACHE_BASE_DIR, safe)


# Ödəniş/hak seçimi gözləyən klonlar üçün müvəqqəti yaddaş (restart-da sıfırlanır — problem deyil, qısa ömürlüdür)
PENDING_JOBS: dict[str, dict] = {}

# Hər istifadəçinin hazırda işləyən klonlama task-ı (cancel düyməsi üçün)
ACTIVE_CLONE_TASKS: dict[int, "asyncio.Task"] = {}


# ---------------------------------------------------------------------------
# PostgreSQL — qalıcı yaddaş (istifadəçilər, referral, haklar, statistika)
# ---------------------------------------------------------------------------

POOL: asyncpg.Pool | None = None


async def db_init(app):
    global POOL
    POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with POOL.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id BIGINT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS referred (
                user_id BIGINT PRIMARY KEY,
                referrer_id BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credits (
                user_id BIGINT PRIMARY KEY,
                amount INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL DEFAULT 0
            );
            """
        )
        for k in ("total_clones", "total_zips", "total_stars"):
            await conn.execute(
                "INSERT INTO stats(key, value) VALUES ($1, 0) ON CONFLICT (key) DO NOTHING", k
            )
    logger.info("PostgreSQL-ə qoşuldu və cədvəllər hazırlandı.")


async def db_close(app):
    if POOL:
        await POOL.close()


async def db_track_user(user_id: int) -> bool:
    """Yeni istifadəçidirsə True qaytarır."""
    async with POOL.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO users(user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
        )
        return result.endswith(" 1")


async def db_user_count() -> int:
    async with POOL.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def db_all_user_ids() -> list[int]:
    async with POOL.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]


async def db_get_credits(user_id: int) -> int:
    async with POOL.acquire() as conn:
        val = await conn.fetchval("SELECT amount FROM credits WHERE user_id=$1", user_id)
        return val or 0


async def db_add_credits(user_id: int, amount: int) -> int:
    async with POOL.acquire() as conn:
        new_val = await conn.fetchval(
            """
            INSERT INTO credits(user_id, amount) VALUES ($1, GREATEST($2, 0))
            ON CONFLICT (user_id) DO UPDATE SET amount = GREATEST(credits.amount + $2, 0)
            RETURNING amount
            """,
            user_id, amount,
        )
        return new_val


async def db_use_credit(user_id: int) -> int:
    async with POOL.acquire() as conn:
        new_val = await conn.fetchval(
            "UPDATE credits SET amount = GREATEST(amount - 1, 0) WHERE user_id=$1 RETURNING amount",
            user_id,
        )
        return new_val or 0


async def db_total_credits() -> int:
    async with POOL.acquire() as conn:
        return await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM credits")


async def db_referral_count(referrer_id: int) -> int:
    async with POOL.acquire() as conn:
        val = await conn.fetchval("SELECT count FROM referrals WHERE referrer_id=$1", referrer_id)
        return val or 0


async def db_top_referrals(limit: int = 5) -> list[tuple[int, int]]:
    async with POOL.acquire() as conn:
        rows = await conn.fetch(
            "SELECT referrer_id, count FROM referrals ORDER BY count DESC LIMIT $1", limit
        )
        return [(r["referrer_id"], r["count"]) for r in rows]


async def db_total_referrals() -> int:
    async with POOL.acquire() as conn:
        return await conn.fetchval("SELECT COALESCE(SUM(count), 0) FROM referrals")


async def db_register_referral(referred_user_id: int, referrer_id: int) -> bool:
    """Hər istifadəçi YALNIZ BİR DƏFƏ referal ola bilər. Özünə-referral bloklanır.
    Uğurlu qeydiyyatda True, əks halda (artıq referal olub / özünə cəhd) False qaytarır."""
    if referred_user_id == referrer_id:
        return False
    async with POOL.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchrow(
                "INSERT INTO referred(user_id, referrer_id) VALUES ($1,$2) "
                "ON CONFLICT DO NOTHING RETURNING user_id",
                referred_user_id, referrer_id,
            )
            if not inserted:
                return False
            await conn.execute(
                """
                INSERT INTO referrals(referrer_id, count) VALUES ($1, 1)
                ON CONFLICT (referrer_id) DO UPDATE SET count = referrals.count + 1
                """,
                referrer_id,
            )
    return True


async def db_incr_stat(key: str, amount: int = 1):
    async with POOL.acquire() as conn:
        await conn.execute(
            "INSERT INTO stats(key, value) VALUES ($1,$2) "
            "ON CONFLICT (key) DO UPDATE SET value = stats.value + $2",
            key, amount,
        )


async def db_get_stats() -> dict:
    async with POOL.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM stats")
        return {r["key"]: r["value"] for r in rows}


def user_label(user) -> str:
    if user.username:
        return f"@{user.username} (`{user.id}`)"
    return f"{user.full_name} (`{user.id}`)"


async def log_event(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Loq kanalına göndərilmədi: {e}")


# ---------------------------------------------------------------------------
# Kanal-üzvlük yoxlaması (məcburi qoşulma)
# ---------------------------------------------------------------------------

_warned_no_channel = False


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    global _warned_no_channel
    if not CHANNEL_ID:
        if not _warned_no_channel:
            logger.warning("CHANNEL_ID təyin olunmayıb — kanal yoxlaması AKTİV DEYİL.")
            _warned_no_channel = True
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Kanal üzvlüyü yoxlanıla bilmədi: {e}")
        return False


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Kanala qoşul", url=CHANNEL_INVITE_LINK)],
            [InlineKeyboardButton("✅ Qatıldım", callback_data="check_sub")],
        ]
    )


GATE_TEXT = (
    "🔒 *Botdan istifadə etmək üçün əvvəlcə kanalımıza qoşulmalısan.*\n\n"
    "1️⃣ Aşağıdakı *Kanala qoşul* düyməsinə bas\n"
    "2️⃣ Qoşulduqdan sonra *✅ Qatıldım* düyməsinə bas"
)
NOT_JOINED_TEXT = "❌ *Kanala girməmisən.*\nZəhmət olmasa qoşul və yenidən cəhd et 👇"


async def show_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(GATE_TEXT, reply_markup=subscription_keyboard(), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(GATE_TEXT, reply_markup=subscription_keyboard(), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Köməkçi funksiyalar
# ---------------------------------------------------------------------------

def normalize_domain(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return None
    if not re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", parsed.netloc.split(":")[0]):
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def site_slug(domain: str) -> str:
    d = domain.lower().split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    first = d.split(".")[0]
    slug = re.sub(r"[^a-z0-9\-]", "-", first).strip("-")
    return slug or "site"


def human_size(num_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def make_progress_bar(elapsed: float, total: float, length: int = 12) -> str:
    ratio = min(1.0, elapsed / total) if total else 1.0
    filled = int(length * ratio)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {int(ratio * 100)}%"


def scan_dir_stats(path: str) -> tuple[int, int]:
    file_count = 0
    total_size = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total_size += os.path.getsize(os.path.join(root, f))
                    file_count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return file_count, total_size


def _make_zip(source_dir: str, zip_path: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, source_dir)
                zf.write(full_path, arcname)


def discover_seed_links(url: str, max_seeds: int) -> list[str]:
    """Ana səhifədən eyni-domenli linkləri çıxarır ki, paralel worker-lər üçün başlanğıc
    nöqtələri olsun. Şəbəkə/parse xətası olarsa boş siyahı qaytarır (təhlükəsiz fallback)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SiteCloneBot/1.0)"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read(800_000).decode(errors="ignore")
    except Exception:
        return []

    base_netloc = urlparse(url).netloc
    start_path = urlparse(url).path or "/"
    seen_paths = set()
    seeds = []
    for h in re.findall(r'href=["\']([^"\'#]+)', html):
        try:
            absu = urljoin(url, h)
            p = urlparse(absu)
            if p.scheme not in ("http", "https") or p.netloc != base_netloc:
                continue
            key = p.path or "/"
            if key in seen_paths or key == start_path:
                continue
            seen_paths.add(key)
            seeds.append(absu)
        except Exception:
            continue
        if len(seeds) >= max_seeds:
            break
    return seeds


async def run_clone(clone_dir: str, url: str) -> tuple[bool, str]:
    """Saytı klonlayır: bir neçə MÜSTƏQİL `wget` prosesi ilə paralel (hər biri ana
    səhifədən tapılan fərqli bir başlanğıc linkdən özünəməxsus rekursiv gəzinti aparır).
    Hər proses `-N` (timestamping) istifadə edir — əvvəlki klonda saxlanılan fayllar
    dəyişməyibsə yenidən yüklənmir (keş effekti). wget2 istifadə olunmur (real testlərdə
    etibarsız çıxdı) — hər worker sübut olunmuş klassik `wget` mühərrikini işlədir."""
    seeds = await asyncio.to_thread(discover_seed_links, url, PARALLEL_WORKERS * 3)

    base_flags = [
        "-r", "-np", "-k", "-E", "-p", "-N",
        "--no-check-certificate",
        "--timeout=15",
        "--tries=2",
    ]

    async def run_one(urls: list[str]) -> str:
        cmd = ["wget", *base_flags, "-P", clone_dir, *urls]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await process.communicate()
            if process.returncode in (0, 8):
                return ""
            return stderr.decode(errors="ignore")[-200:]
        except asyncio.CancelledError:
            if process:
                process.kill()
            raise
        except FileNotFoundError:
            return "Serverdə `wget` quraşdırılmayıb."
        except Exception as e:
            return str(e)

    if len(seeds) >= 2:
        n_groups = min(len(seeds), PARALLEL_WORKERS)
        groups: list[list[str]] = [[] for _ in range(n_groups)]
        groups[0].append(url)
        for i, s in enumerate(seeds):
            groups[i % n_groups].append(s)
    else:
        groups = [[url]]

    try:
        errors = await asyncio.wait_for(
            asyncio.gather(*(run_one(g) for g in groups)), timeout=WGET_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        has_files = os.path.isdir(clone_dir) and any(os.scandir(clone_dir))
        return has_files, "Vaxt limiti bitdi (sayt çox böyük ola bilər)."

    has_files = os.path.isdir(clone_dir) and any(os.scandir(clone_dir))
    if not has_files:
        combined = "; ".join(e for e in errors if e) or "naməlum xəta"
        return False, combined
    return True, ""


def clear_stale_clones(max_age_sec: int = 3600) -> int:
    removed = 0
    now = time.time()
    if not os.path.isdir(CLONE_BASE_DIR):
        return 0
    for entry in os.scandir(CLONE_BASE_DIR):
        try:
            age = now - entry.stat().st_mtime
            if age > max_age_sec:
                if entry.is_dir():
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.remove(entry.path)
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Menyu klaviaturaları
# ---------------------------------------------------------------------------

def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🌐 Necə işləyir?", callback_data="menu_howto")],
        [InlineKeyboardButton("💰 Qiymət", callback_data="menu_price")],
        [InlineKeyboardButton("📊 Statistikam", callback_data="menu_mystats")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="menu_admin")])
    return InlineKeyboardMarkup(buttons)


def menu_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Geri", callback_data="menu_back")]])


def cancel_clone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Klonlamağı dayandır", callback_data="cancel_clone")]])


WELCOME_TEXT = (
    "👋 *Sayt Kopyalayıcı Bot*a xoş gəldin!\n\n"
    "🌐 İstənilən saytın tam statik köçürməsini bir neçə saniyəyə əldə edin.\n\n"
    "*Necə işləyir:*\n"
    "1️⃣ Domeni göndər — məsələn: `example.com`\n"
    "2️⃣ Sayt klonlanana qədər canlı statistika izlə\n"
    f"3️⃣ {STARS_PRICE} ⭐ ödə və ZIP arxivini al\n\n"
    "Başlamaq üçün bir domen göndər 👇"
)


# ---------------------------------------------------------------------------
# /start, referral, kanal yoxlaması
# ---------------------------------------------------------------------------

async def maybe_register_referral(context: ContextTypes.DEFAULT_TYPE, user):
    referrer_id = context.user_data.pop("pending_referrer_id", None)
    if referrer_id is None:
        return
    ok = await db_register_referral(user.id, referrer_id)
    if ok:
        count = await db_referral_count(referrer_id)
        try:
            await context.bot.send_message(
                referrer_id,
                f"🎉 Referral linkinlə yeni istifadəçi qoşuldu!\n👥 Ümumi referral: {count}",
            )
        except Exception:
            pass
    else:
        # artıq başqasının referal-ı olub və ya özünə cəhd edib — sükutla keç
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = await db_track_user(user.id)
    context.user_data.pop("awaiting_broadcast", None)
    context.user_data.pop("awaiting_credit_grant", None)

    if is_new:
        await log_event(context, f"🆕 Yeni istifadəçi: {user_label(user)}")

    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            candidate = arg[4:]
            if candidate.isdigit():
                context.user_data["pending_referrer_id"] = int(candidate)

    if user.id not in ADMIN_IDS and not await is_subscribed(context, user.id):
        await show_gate(update, context)
        return

    await maybe_register_referral(context, user)
    await send_welcome(update, context)


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message:
        await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=main_menu_keyboard(user_id))
    elif update.callback_query:
        await update.callback_query.edit_message_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=main_menu_keyboard(user_id))


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if await is_subscribed(context, user.id):
        await query.answer("✅ Təsdiqləndi!")
        await maybe_register_referral(context, user)
        await send_welcome(update, context)
    else:
        await query.answer("❌ Kanala qoşulmamısan!", show_alert=True)
        try:
            await query.edit_message_text(NOT_JOINED_TEXT, reply_markup=subscription_keyboard(), parse_mode="Markdown")
        except Exception:
            pass


async def cancel_clone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    task = ACTIVE_CLONE_TASKS.get(user_id)
    if task and not task.done():
        task.cancel()
        await query.answer("Dayandırılır...")
    else:
        await query.answer("Aktiv klonlama tapılmadı.", show_alert=True)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    action = query.data
    await query.answer()

    if action == "menu_howto":
        text = (
            "🌐 *Necə işləyir?*\n\n"
            "1️⃣ Klonlamaq istədiyin saytın domenini yaz (məs. `example.com`)\n"
            "2️⃣ Bot saytı canlı statistika ilə köçürür\n"
            f"3️⃣ Hazır olanda {STARS_PRICE} ⭐ ödəyib (və ya hakkın varsa pulsuz) ZIP arxivini alırsan"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=menu_back_keyboard())

    elif action == "menu_price":
        text = f"💰 *Qiymət:* hər sayt klonu — *{STARS_PRICE} ⭐* (Telegram Stars)."
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=menu_back_keyboard())

    elif action == "menu_mystats":
        ref_count = await db_referral_count(user_id)
        credits = await db_get_credits(user_id)
        text = (
            "📊 *Sənin statistikan:*\n\n"
            f"🔗 Referral etdiyin istifadəçi: *{ref_count}*\n"
            f"🎟 Pulsuz hakların: *{credits}*"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=menu_back_keyboard())

    elif action == "menu_admin":
        if user_id not in ADMIN_IDS:
            await query.answer("İcazən yoxdur.", show_alert=True)
            return
        await query.edit_message_text("🛠 *Admin Panel*\n\nAşağıdan bir funksiya seç 👇", parse_mode="Markdown", reply_markup=admin_menu_keyboard())

    elif action == "menu_back":
        await query.edit_message_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=main_menu_keyboard(user_id))


# ---------------------------------------------------------------------------
# Sayt klonlama axını
# ---------------------------------------------------------------------------

async def handle_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if context.user_data.get("awaiting_broadcast") and user_id in ADMIN_IDS:
        await do_broadcast(update, context)
        return

    if context.user_data.get("awaiting_credit_grant") and user_id in ADMIN_IDS:
        await do_grant_credit(update, context)
        return

    if user_id not in ADMIN_IDS and not await is_subscribed(context, user_id):
        await show_gate(update, context)
        return

    await db_track_user(user_id)
    raw_text = update.message.text
    url = normalize_domain(raw_text)

    if not url:
        await update.message.reply_text(
            "❌ Düzgün domen formatı deyil.\nMəsələn: `example.com` və ya `https://example.com`",
            parse_mode="Markdown",
        )
        return

    domain = urlparse(url).netloc
    slug = site_slug(domain)
    clone_dir = cache_dir_for(domain)
    is_repeat = os.path.isdir(clone_dir) and any(os.scandir(clone_dir))
    os.makedirs(clone_dir, exist_ok=True)

    domain_lock = get_domain_lock(domain)
    if domain_lock.locked():
        await update.message.reply_text(
            f"⏳ *{domain}* hazırda başqa sorğu tərəfindən yenilənir, sıra gözlənilir...",
            parse_mode="Markdown",
        )

    async with domain_lock:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        start_label = "yenilənir (keşdən)" if is_repeat else "klonlanmağa başlanır"
        status_msg = await update.message.reply_text(
            f"🚀 *{domain}* {start_label}...", parse_mode="Markdown", reply_markup=cancel_clone_keyboard()
        )
        await log_event(context, f"🌐 {user_label(user)} klonlamağa başladı: `{domain}`" + (" (keş yeniləməsi)" if is_repeat else ""))

        stop_animation = asyncio.Event()
        start_time = time.time()
        spinner_frames = ["◐", "◓", "◑", "◒"]

        async def animate():
            i = 0
            while not stop_animation.is_set():
                elapsed = time.time() - start_time
                remaining = WGET_TIMEOUT_SEC - elapsed
                file_count, total_size = scan_dir_stats(clone_dir)
                bar = make_progress_bar(elapsed, WGET_TIMEOUT_SEC)
                spin = spinner_frames[i % len(spinner_frames)]
                text = (
                    f"{spin} *{domain}* {'yenilənir' if is_repeat else 'klonlanır'}...\n\n"
                    f"{bar}\n"
                    f"📄 Fayllar: *{file_count}*\n"
                    f"💾 Ölçü: *{human_size(total_size)}*\n"
                    f"⏱ Keçən vaxt: `{format_time(elapsed)}`\n"
                    f"⏳ Limitə qalan: `{format_time(remaining)}`"
                )
                try:
                    await status_msg.edit_text(text, parse_mode="Markdown")
                except Exception:
                    pass
                i += 1
                await asyncio.sleep(ANIMATION_INTERVAL_SEC)

        animation_task = asyncio.create_task(animate())

        current_task = asyncio.current_task()
        ACTIVE_CLONE_TASKS[user_id] = current_task
        cancelled = False

        try:
            success, error_text = await run_clone(clone_dir, url)
        except asyncio.CancelledError:
            cancelled = True
            success, error_text = False, ""
        finally:
            stop_animation.set()
            await animation_task
            if ACTIVE_CLONE_TASKS.get(user_id) is current_task:
                ACTIVE_CLONE_TASKS.pop(user_id, None)

    if cancelled:
        if not is_repeat:
            shutil.rmtree(clone_dir, ignore_errors=True)
        try:
            await status_msg.edit_text(
                f"🛑 *{domain}* klonlanması dayandırıldı.\n\n"
                "Yenidən göndərmək istəyirsinizsə saytın ünvanını göndərin.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception:
            pass
        await log_event(context, f"🛑 {user_label(user)} — `{domain}` klonlamasını dayandırdı.")
        return

    if not success:
        if not is_repeat:
            shutil.rmtree(clone_dir, ignore_errors=True)
        await status_msg.edit_text(
            f"❌ *{domain}* kopyalanmadı.\nSəbəb: {error_text or 'naməlum xəta'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([]),
        )
        await log_event(context, f"❌ {user_label(user)} — `{domain}` klonlanmadı: {error_text or 'naməlum xəta'}")
        return

    total_elapsed = time.time() - start_time
    file_count, total_size = scan_dir_stats(clone_dir)
    job_id = uuid.uuid4().hex[:8]

    await db_incr_stat("total_clones", 1)

    status_word = "yeniləndi (keşdən)" if is_repeat else "uğurla klonlandı"
    summary = (
        f"✅ *{domain}* {status_word}!\n\n"
        f"📄 Fayllar: *{file_count}*\n"
        f"💾 Ölçü: *{human_size(total_size)}*\n"
        f"⏱ Çəkilən vaxt: `{format_time(total_elapsed)}`\n"
    )

    await log_event(
        context,
        f"✅ {user_label(user)} — `{domain}` klonlandı ({file_count} fayl, {human_size(total_size)}, {format_time(total_elapsed)})",
    )

    # 1) Admin / həmişəlik pulsuz istifadəçi
    if user_id in FREE_USER_IDS:
        await status_msg.edit_text(summary + "\n📦 Arxiv hazırlanır...", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([]))
        await send_clone_zip(context, update.effective_chat.id, clone_dir, slug)
        return

    credits = await db_get_credits(user_id)

    # 2) İstifadəçinin pulsuz hakkı varsa — seçim təklif olunur
    if credits > 0:
        PENDING_JOBS[job_id] = {
            "clone_dir": clone_dir,
            "slug": slug,
            "domain": domain,
            "chat_id": update.effective_chat.id,
            "user_id": user_id,
        }
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"🎟 Hakdan istifadə et ({credits} qalıb)", callback_data=f"credit_use:{job_id}")],
                [InlineKeyboardButton(f"⭐ {STARS_PRICE} ulduz ödə", callback_data=f"credit_pay:{job_id}")],
            ]
        )
        await status_msg.edit_text(summary + "\nÖdəniş üsulunu seç 👇", parse_mode="Markdown", reply_markup=kb)
        return

    # 3) Standart axın — ulduz ödənişi
    await status_msg.edit_text(
        summary + f"\nFaylları yükləmək üçün {STARS_PRICE} ⭐ ödəniş et 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([]),
    )

    payload = f"{clone_dir}|{slug}|{domain}"
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=f"{domain} — Sayt kopyası",
        description=f"{domain} saytının tam klonlanmış fayllarının ZIP arxivi ({human_size(total_size)}, {file_count} fayl)",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{domain} klonu", amount=STARS_PRICE)],
    )


async def credit_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, _, job_id = query.data.partition(":")
    job = PENDING_JOBS.get(job_id)

    if not job:
        await query.answer("Bu sorğunun vaxtı bitib.", show_alert=True)
        return
    if query.from_user.id != job["user_id"]:
        await query.answer("Bu sənin sorğun deyil.", show_alert=True)
        return

    await query.answer()
    PENDING_JOBS.pop(job_id, None)

    if action == "credit_use":
        new_balance = await db_use_credit(job["user_id"])
        await query.edit_message_text(
            f"🎟 Hakdan istifadə olundu! Qalan hak: *{new_balance}*\n\n📦 Arxiv hazırlanır...",
            parse_mode="Markdown",
        )
        await log_event(
            context,
            f"🎟 {user_label(query.from_user)} — `{job['domain']}` üçün hakdan istifadə etdi (qalan: {new_balance})",
        )
        await send_clone_zip(context, job["chat_id"], job["clone_dir"], job["slug"])
    else:
        payload = f"{job['clone_dir']}|{job['slug']}|{job['domain']}"
        await query.edit_message_text("⭐ Ulduz ödənişinə keçilir...")
        await context.bot.send_invoice(
            chat_id=job["chat_id"],
            title=f"{job['domain']} — Sayt kopyası",
            description=f"{job['domain']} saytının tam klonlanmış fayllarının ZIP arxivi",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{job['domain']} klonu", amount=STARS_PRICE)],
        )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    parts = payload.split("|")
    clone_dir = parts[0] if len(parts) > 0 else ""
    slug = parts[1] if len(parts) > 1 else "site"
    paid_domain = parts[2] if len(parts) > 2 else slug

    await db_incr_stat("total_stars", payment.total_amount)

    if not os.path.isdir(clone_dir):
        await update.message.reply_text("⚠️ Ödəniş qəbul olundu, amma fayllar tapılmadı. Zəhmət olmasa dəstəklə əlaqə saxla.")
        return

    await update.message.reply_text("💳 Ödəniş təsdiqləndi! Arxiv hazırlanır...")

    buyer = update.effective_user
    await log_event(
        context,
        f"💰 *{paid_domain}* saytı {user_label(buyer)} tərəfindən *{payment.total_amount} ⭐*-a klonlanıb alınıb.",
    )
    await send_clone_zip(context, update.effective_chat.id, clone_dir, slug)


async def send_clone_zip(context: ContextTypes.DEFAULT_TYPE, chat_id: int, clone_dir: str, slug: str):
    """Keş qovluğunu zip edib göndərir. Keş qovluğunun özü SİLİNMİR (növbəti sorğuda
    təkrar istifadə olunsun deyə) — yalnız müvəqqəti zip faylı təmizlənir."""
    zip_path = os.path.join(CLONE_BASE_DIR, f"{uuid.uuid4().hex[:10]}.zip")
    display_name = f"{slug}-clone.zip"

    await asyncio.to_thread(_make_zip, clone_dir, zip_path)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        with open(zip_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(f, filename=display_name),
                caption=f"✅ Buyur! *{display_name}* hazırdır.",
                parse_mode="Markdown",
            )
        await db_incr_stat("total_zips", 1)
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")],
            [InlineKeyboardButton("👥 İstifadəçilər", callback_data="admin_users")],
            [InlineKeyboardButton("🔗 Referral TOP", callback_data="admin_referrals")],
            [InlineKeyboardButton("🎟 Hak ver", callback_data="admin_grant_credit")],
            [InlineKeyboardButton("📢 Bildiriş göndər", callback_data="admin_broadcast")],
            [InlineKeyboardButton("🗑 Müvəqqəti faylları təmizlə", callback_data="admin_clear_cache")],
            [InlineKeyboardButton("🗑🌐 Sayt keşini tam təmizlə", callback_data="admin_clear_sitecache")],
            [InlineKeyboardButton("❌ Bağla", callback_data="admin_close")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Geri", callback_data="admin_back")]])


def admin_cancel_keyboard(target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Ləğv et", callback_data=f"admin_cancel_{target}")]])


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Bu əmr yalnız admin üçündür.")
        return
    await update.message.reply_text("🛠 *Admin Panel*\n\nAşağıdan bir funksiya seç 👇", parse_mode="Markdown", reply_markup=admin_menu_keyboard())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id not in ADMIN_IDS:
        await query.answer("İcazən yoxdur.", show_alert=True)
        return

    action = query.data
    await query.answer()

    if action == "admin_stats":
        stats = await db_get_stats()
        active_credits = await db_total_credits()
        total_users = await db_user_count()
        total_refs = await db_total_referrals()
        cache_sites, cache_size = scan_dir_stats(CACHE_BASE_DIR)
        text = (
            "📊 *Bot Statistikası*\n\n"
            f"👥 İstifadəçilər: *{total_users}*\n"
            f"🌐 Klonlanan saytlar: *{stats.get('total_clones', 0)}*\n"
            f"📦 Göndərilən ZIP-lər: *{stats.get('total_zips', 0)}*\n"
            f"⭐ Qazanılan ulduzlar: *{stats.get('total_stars', 0)}*\n"
            f"🔗 Referral qeydiyyatları: *{total_refs}*\n"
            f"🎟 Aktiv haklar (cəmi): *{active_credits}*\n"
            f"💾 Keşdəki fayllar: *{cache_sites}* fayl, *{human_size(cache_size)}*"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_back_keyboard())

    elif action == "admin_users":
        total_users = await db_user_count()
        text = f"👥 *Ümumi istifadəçi sayı:* {total_users}"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_back_keyboard())

    elif action == "admin_referrals":
        top = await db_top_referrals(5)
        if not top:
            text = "🔗 Hələ referral qeydiyyatı yoxdur."
        else:
            lines = [f"{i + 1}. `{uid}` — {count} referral" for i, (uid, count) in enumerate(top)]
            text = "🔗 *TOP Referral-lar:*\n\n" + "\n".join(lines)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_back_keyboard())

    elif action == "admin_grant_credit":
        context.user_data["awaiting_credit_grant"] = True
        await query.edit_message_text(
            "🎟 İstifadəçi ID və hak sayını boşluqla göndər.\nMəsələn: `123456789 3`",
            parse_mode="Markdown",
            reply_markup=admin_cancel_keyboard("credit"),
        )

    elif action == "admin_cancel_credit":
        context.user_data["awaiting_credit_grant"] = False
        await query.edit_message_text("🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_keyboard())

    elif action == "admin_broadcast":
        context.user_data["awaiting_broadcast"] = True
        await query.edit_message_text(
            "📢 İndi bütün istifadəçilərə göndəriləcək mesajı yaz:",
            reply_markup=admin_cancel_keyboard("broadcast"),
        )

    elif action == "admin_cancel_broadcast":
        context.user_data["awaiting_broadcast"] = False
        await query.edit_message_text("🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_keyboard())

    elif action == "admin_clear_cache":
        removed = clear_stale_clones()
        await query.edit_message_text(f"🗑 Təmizləndi: *{removed}* köhnə qovluq/fayl silindi.", parse_mode="Markdown", reply_markup=admin_back_keyboard())

    elif action == "admin_clear_sitecache":
        count = 0
        if os.path.isdir(CACHE_BASE_DIR):
            for entry in os.scandir(CACHE_BASE_DIR):
                try:
                    shutil.rmtree(entry.path) if entry.is_dir() else os.remove(entry.path)
                    count += 1
                except OSError:
                    pass
        await query.edit_message_text(
            f"🗑🌐 Sayt keşi tam təmizləndi: *{count}* sayt silindi. Növbəti klonlamalar sıfırdan gedəcək.",
            parse_mode="Markdown", reply_markup=admin_back_keyboard(),
        )

    elif action == "admin_back":
        await query.edit_message_text("🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_keyboard())

    elif action == "admin_close":
        await query.edit_message_text("Admin panel bağlandı.", reply_markup=InlineKeyboardMarkup([]))


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_broadcast"] = False
    text = update.message.text
    status = await update.message.reply_text("📢 Göndərilir...")

    sent, failed = 0, 0
    for uid in await db_all_user_ids():
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(f"✅ Bildiriş göndərildi.\nUğurlu: {sent}\nUğursuz: {failed}")
    await log_event(context, f"📢 {user_label(update.effective_user)} bildiriş göndərdi — uğurlu: {sent}, uğursuz: {failed}")


async def do_grant_credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_credit_grant"] = False
    text = update.message.text.strip()
    parts = text.split()

    if len(parts) != 2 or not parts[0].isdigit() or not re.match(r"^-?\d+$", parts[1]):
        await update.message.reply_text("❌ Format səhvdir. Nümunə: `123456789 3`", parse_mode="Markdown")
        return

    target_id, amount = int(parts[0]), int(parts[1])
    new_balance = await db_add_credits(target_id, amount)

    await update.message.reply_text(
        f"✅ İstifadəçi `{target_id}` üçün hak yeniləndi. Yeni balans: *{new_balance}*",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            target_id,
            f"🎁 Sənə {amount} pulsuz sayt klonlama haqqı verildi!\n🎟 Cəmi hakların: {new_balance}",
        )
    except Exception:
        pass

    await log_event(
        context,
        f"🎁 {user_label(update.effective_user)} — `{target_id}` istifadəçisinə {amount} hak verdi (yeni balans: {new_balance})",
    )


# ---------------------------------------------------------------------------
# Başlanğıc
# ---------------------------------------------------------------------------

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # eyni anda bir neçə istifadəçiyə xidmət et (biri klonlayanda başqası bloklanmasın)
        .post_init(db_init)
        .post_shutdown(db_close)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_domain))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(cancel_clone_callback, pattern="^cancel_clone$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(credit_choice_callback, pattern="^credit_(use|pay):"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    logger.info("Bot işə düşdü...")
    app.run_polling()


if __name__ == "__main__":
    main()

