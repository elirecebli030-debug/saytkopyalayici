import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from urllib.parse import urlparse

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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8873950422:AAHGCI2J71_Tnc3tleUrUGkW24ZehpL0qMA")
STARS_PRICE = 100                 # Telegram Stars miqdarı
CLONE_BASE_DIR = "clones"         # klonların saxlanacağı qovluq
WGET_TIMEOUT_SEC = 600             # klonlama üçün maksimum vaxt (10 dəqiqə)
ANIMATION_INTERVAL_SEC = 1.4

SPECIAL_USER_ID = 8133937162
ADMIN_IDS = {SPECIAL_USER_ID}       # /admin əmrinə və admin düyməsinə giriş
FREE_USER_IDS = {SPECIAL_USER_ID}   # həmişə ödənişsiz, birbaşa zip

# Botu admin etdiyin loq kanalı — bütün fəaliyyət (kim, nə vaxt, hansı sayt, ödəniş/hak) bura yazılır
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "-1003909741389")

DATA_FILE = "data.json"
# ========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.makedirs(CLONE_BASE_DIR, exist_ok=True)

# Ödəniş/hak seçimi gözləyən klonlar üçün müvəqqəti yaddaş (yaddaşda saxlanılır, restart-da sıfırlanır)
PENDING_JOBS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Sadə JSON əsaslı yaddaş (istifadəçilər, referral, haklar, statistika)
# ---------------------------------------------------------------------------

def _default_data():
    return {
        "users": [],
        "referrals": {},   # referrer_id(str) -> count
        "referred": {},    # referred_user_id(str) -> referrer_id(str)
        "credits": {},     # user_id(str) -> qalan pulsuz klon sayı
        "stats": {"total_clones": 0, "total_zips": 0, "total_stars": 0},
    }


def load_data() -> dict:
    defaults = _default_data()
    if not os.path.exists(DATA_FILE):
        return defaults
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"data.json oxuna bilmədi, boş data ilə başlanır: {e}")
        return defaults

    if not isinstance(data, dict):
        return defaults

    # köhnə/pozulmuş data.json-dakı yanlış tipləri düzəldirik ki, bot çökməsin
    if not isinstance(data.get("users"), list):
        logger.warning("data.json: 'users' sahəsi list deyildi, sıfırlanır.")
        data["users"] = defaults["users"]
    if not isinstance(data.get("referrals"), dict):
        data["referrals"] = defaults["referrals"]
    if not isinstance(data.get("referred"), dict):
        data["referred"] = defaults["referred"]
    if not isinstance(data.get("credits"), dict):
        data["credits"] = defaults["credits"]
    if not isinstance(data.get("stats"), dict):
        data["stats"] = defaults["stats"]
    else:
        for k, v in defaults["stats"].items():
            data["stats"].setdefault(k, v)
    return data


DATA = load_data()
save_data_needed = True


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"data.json yazılmadı: {e}")


# fayl mövcud olmayıbsa və ya düzəldilibsə, dərhal diskə yazaq ki, "data.json faylı yoxdu" problemi olmasın
save_data()


def track_user(user_id: int) -> bool:
    """Yeni istifadəçidirsə True qaytarır."""
    if user_id not in DATA["users"]:
        DATA["users"].append(user_id)
        save_data()
        return True
    return False


def user_label(user) -> str:
    if user.username:
        return f"@{user.username} (`{user.id}`)"
    return f"{user.full_name} (`{user.id}`)"


async def log_event(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Admin loq kanalına fəaliyyət mesajı göndərir (kanal ID düzgün deyilsə səssizcə keçir)."""
    if not LOG_CHANNEL_ID:
        return
    try:
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Loq kanalına göndərilmədi: {e}")


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
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = track_user(user.id)
    context.user_data.pop("awaiting_broadcast", None)
    context.user_data.pop("awaiting_credit_grant", None)

    if is_new:
        await log_event(context, f"🆕 Yeni istifadəçi: {user_label(user)}")

    # referral qeydiyyatı — birbaşa /start-da sayılır (kanal yoxlaması yoxdur)
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            referrer_id = arg[4:]
            if (
                referrer_id.isdigit()
                and referrer_id != str(user.id)
                and str(user.id) not in DATA["referred"]
            ):
                DATA["referred"][str(user.id)] = referrer_id
                DATA["referrals"][referrer_id] = DATA["referrals"].get(referrer_id, 0) + 1
                save_data()
                try:
                    await context.bot.send_message(
                        int(referrer_id),
                        f"🎉 Referral linkinlə yeni istifadəçi qoşuldu!\n"
                        f"👥 Ümumi referral: {DATA['referrals'][referrer_id]}",
                    )
                except Exception:
                    pass

    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=main_menu_keyboard(user.id))


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
        ref_count = DATA["referrals"].get(str(user_id), 0)
        credits = DATA["credits"].get(str(user_id), 0)
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

    track_user(user_id)
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
    job_id = uuid.uuid4().hex[:8]
    clone_dir = os.path.join(CLONE_BASE_DIR, f"{domain}-{job_id}")
    os.makedirs(clone_dir, exist_ok=True)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    status_msg = await update.message.reply_text(f"🚀 *{domain}* klonlanmağa başlanır...", parse_mode="Markdown")
    await log_event(context, f"🌐 {user_label(user)} klonlamağa başladı: `{domain}`")

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
                f"{spin} *{domain}* klonlanır...\n\n"
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

    cmd = ["wget", "-r", "-np", "-k", "-E", "-p", "-P", clone_dir, url]

    success = False
    error_text = ""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=WGET_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            process.kill()
            error_text = "Vaxt limiti bitdi (sayt çox böyük ola bilər)."
        else:
            if process.returncode in (0, 8):
                has_files = any(os.scandir(clone_dir))
                success = has_files
                if not has_files:
                    error_text = "Saytdan heç bir fayl endirilmədi."
            else:
                error_text = stderr.decode(errors="ignore")[-300:]
    except FileNotFoundError:
        error_text = "Serverdə `wget` quraşdırılmayıb."
    except Exception as e:
        error_text = str(e)
    finally:
        stop_animation.set()
        await animation_task

    if not success:
        shutil.rmtree(clone_dir, ignore_errors=True)
        await status_msg.edit_text(f"❌ *{domain}* kopyalanmadı.\nSəbəb: {error_text or 'naməlum xəta'}", parse_mode="Markdown")
        await log_event(context, f"❌ {user_label(user)} — `{domain}` klonlanmadı: {error_text or 'naməlum xəta'}")
        return

    total_elapsed = time.time() - start_time
    file_count, total_size = scan_dir_stats(clone_dir)

    DATA["stats"]["total_clones"] = DATA["stats"].get("total_clones", 0) + 1
    save_data()

    summary = (
        f"✅ *{domain}* uğurla klonlandı!\n\n"
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
        await status_msg.edit_text(summary + "\n📦 Arxiv hazırlanır...", parse_mode="Markdown")
        await send_clone_zip(context, update.effective_chat.id, clone_dir, slug)
        return

    credits = DATA["credits"].get(str(user_id), 0)

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
    await status_msg.edit_text(summary + f"\nFaylları yükləmək üçün {STARS_PRICE} ⭐ ödəniş et 👇", parse_mode="Markdown")

    payload = f"{clone_dir}|{slug}"
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
        uid_str = str(job["user_id"])
        DATA["credits"][uid_str] = max(0, DATA["credits"].get(uid_str, 0) - 1)
        save_data()
        await query.edit_message_text(
            f"🎟 Hakdan istifadə olundu! Qalan hak: *{DATA['credits'][uid_str]}*\n\n📦 Arxiv hazırlanır...",
            parse_mode="Markdown",
        )
        await log_event(
            context,
            f"🎟 {user_label(query.from_user)} — `{job['domain']}` üçün hakdan istifadə etdi (qalan: {DATA['credits'][uid_str]})",
        )
        await send_clone_zip(context, job["chat_id"], job["clone_dir"], job["slug"])
    else:
        payload = f"{job['clone_dir']}|{job['slug']}"
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
    clone_dir, _, slug = payload.partition("|")
    slug = slug or "site"

    DATA["stats"]["total_stars"] = DATA["stats"].get("total_stars", 0) + payment.total_amount
    save_data()

    if not os.path.isdir(clone_dir):
        await update.message.reply_text("⚠️ Ödəniş qəbul olundu, amma fayllar tapılmadı. Zəhmət olmasa dəstəklə əlaqə saxla.")
        return

    await update.message.reply_text("💳 Ödəniş təsdiqləndi! Arxiv hazırlanır...")
    await log_event(
        context,
        f"⭐ {user_label(update.effective_user)} — {payment.total_amount} ulduz ödədi (`{slug}`)",
    )
    await send_clone_zip(context, update.effective_chat.id, clone_dir, slug)


async def send_clone_zip(context: ContextTypes.DEFAULT_TYPE, chat_id: int, clone_dir: str, slug: str):
    zip_path = clone_dir + ".zip"
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
        DATA["stats"]["total_zips"] = DATA["stats"].get("total_zips", 0) + 1
        save_data()
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
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
            [InlineKeyboardButton("🗑 Keşi təmizlə", callback_data="admin_clear_cache")],
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
        stats = DATA["stats"]
        active_credits = sum(DATA["credits"].values())
        text = (
            "📊 *Bot Statistikası*\n\n"
            f"👥 İstifadəçilər: *{len(DATA['users'])}*\n"
            f"🌐 Klonlanan saytlar: *{stats.get('total_clones', 0)}*\n"
            f"📦 Göndərilən ZIP-lər: *{stats.get('total_zips', 0)}*\n"
            f"⭐ Qazanılan ulduzlar: *{stats.get('total_stars', 0)}*\n"
            f"🔗 Referral qeydiyyatları: *{sum(DATA['referrals'].values())}*\n"
            f"🎟 Aktiv haklar (cəmi): *{active_credits}*"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_back_keyboard())

    elif action == "admin_users":
        text = f"👥 *Ümumi istifadəçi sayı:* {len(DATA['users'])}"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_back_keyboard())

    elif action == "admin_referrals":
        top = sorted(DATA["referrals"].items(), key=lambda x: x[1], reverse=True)[:5]
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

    elif action == "admin_back":
        await query.edit_message_text("🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_keyboard())

    elif action == "admin_close":
        await query.edit_message_text("Admin panel bağlandı.", reply_markup=InlineKeyboardMarkup([]))


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_broadcast"] = False
    text = update.message.text
    status = await update.message.reply_text("📢 Göndərilir...")

    sent, failed = 0, 0
    for uid in DATA["users"]:
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

    target_id, amount = parts[0], int(parts[1])
    DATA["credits"][target_id] = max(0, DATA["credits"].get(target_id, 0) + amount)
    save_data()

    await update.message.reply_text(
        f"✅ İstifadəçi `{target_id}` üçün hak yeniləndi. Yeni balans: *{DATA['credits'][target_id]}*",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            int(target_id),
            f"🎁 Sənə {amount} pulsuz sayt klonlama haqqı verildi!\n🎟 Cəmi hakların: {DATA['credits'][target_id]}",
        )
    except Exception:
        pass

    await log_event(
        context,
        f"🎁 {user_label(update.effective_user)} — `{target_id}` istifadəçisinə {amount} hak verdi (yeni balans: {DATA['credits'][target_id]})",
    )


# ---------------------------------------------------------------------------
# Başlanğıc
# ---------------------------------------------------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_domain))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(credit_choice_callback, pattern="^credit_(use|pay):"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    logger.info("Bot işə düşdü...")
    app.run_polling()


if __name__ == "__main__":
    main()
