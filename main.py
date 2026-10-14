import os
import json
import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import telebot
from telebot import types

from config import *
import checker


# ============================================================
# CONFIG / COMPATIBILITY
# ============================================================

# Keep compatibility with older config.py files that do not define these.
FREE_MODE = globals().get("FREE_MODE", False)
THREADS = max(1, int(globals().get("THREADS", os.getenv("THREADS", "15"))))
MAX_COMBO_LINES = max(
    1, int(globals().get("MAX_COMBO_LINES", os.getenv("MAX_COMBO_LINES", "70000")))
)
STATUS_UPDATE_SECONDS = max(
    4.0,
    float(globals().get("STATUS_UPDATE_SECONDS", os.getenv("STATUS_UPDATE_SECONDS", "8")))
)

START_TIME = datetime.now()

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)

# Normalize authorized users so both old list-based and new set-based configs work.
if "AUTHORIZED_USERS" not in globals():
    AUTHORIZED_USERS = set()

try:
    AUTHORIZED_USERS = set(int(x) for x in AUTHORIZED_USERS)
except (TypeError, ValueError):
    AUTHORIZED_USERS = set()

if ADMIN_ID:
    AUTHORIZED_USERS.add(int(ADMIN_ID))

usage_lock = threading.RLock()
active_lock = threading.RLock()

# uid -> list[Future]
active_futures = {}

# uid -> ThreadPoolExecutor
active_executors = {}

# uid -> status updater stop Event
status_events = {}

logger = logging.getLogger("bunny_checker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


# ============================================================
# COMPATIBILITY HELPERS
# ============================================================

def get_user_folder(user_id):
    """Compatibility helper for old config.py versions."""
    if "get_user_folder" in globals():
        return get_user_folder_from_config(user_id)

    paths = get_user_paths(user_id)
    base = paths.get("base")
    if base:
        os.makedirs(str(base), exist_ok=True)
        return str(base)

    # Fallback for the original config.py.
    folder = os.path.join(USERS_DIR, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder


def get_user_folder_from_config(user_id):
    # Avoid recursively calling our compatibility function.
    paths = get_user_paths(user_id)
    if isinstance(paths, dict):
        base = paths.get("base")
        if base:
            return str(base)

        for key in ("combo", "list", "keywords"):
            value = paths.get(key)
            if value:
                return os.path.dirname(str(value))

    folder = os.path.join(str(USERS_DIR), str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder


def normalize_paths(user_id):
    """
    Ensure all keys used by main.py exist, while accepting both:
      old config: combo / hits / hits_no_kw
      new config: list / keywords_result / good_logins
    """
    paths = dict(get_user_paths(user_id))

    def first(*keys):
        for key in keys:
            if key in paths and paths[key]:
                return str(paths[key])
        return None

    folder = first("base")
    if folder is None:
        candidate = first("combo", "list", "keywords")
        folder = os.path.dirname(candidate) if candidate else os.path.join(
            str(USERS_DIR), str(user_id)
        )

    os.makedirs(folder, exist_ok=True)

    paths["combo"] = first("combo", "list") or os.path.join(folder, "list.txt")
    paths["list"] = paths["combo"]

    paths["keywords"] = first("keywords") or os.path.join(folder, "keywords.txt")
    paths["hits"] = (
        first("hits", "keywords_result")
        or os.path.join(folder, "keywords_result.txt")
    )
    paths["keywords_result"] = paths["hits"]

    paths["hits_no_kw"] = (
        first("hits_no_kw", "good_logins")
        or os.path.join(folder, "good_logins.txt")
    )
    paths["good_logins"] = paths["hits_no_kw"]

    paths["debug"] = first("debug") or os.path.join(folder, "debug.jsonl")

    return paths


def persist_authorized_users():
    """Use config's persistence function if available."""
    try:
        save_authorized_users()
        return
    except NameError:
        pass
    except Exception:
        logger.exception("Failed to persist authorized users")
        return

    # Compatibility fallback for the original config.py.
    auth_file = globals().get(
        "AUTHORIZED_USERS_FILE",
        os.path.join(str(USERS_DIR), "authorized_users.json")
    )
    try:
        tmp = auth_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(AUTHORIZED_USERS), f, indent=2)
        os.replace(tmp, auth_file)
    except OSError:
        logger.exception("Failed to save authorized users")


def is_authorized(uid):
    return int(uid) in AUTHORIZED_USERS


# ============================================================
# USAGE / FREE PLAN
# ============================================================

def load_usage():
    try:
        if not os.path.exists(USAGE_FILE):
            return {}

        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        logger.exception("Failed to load usage file")
        return {}


def save_usage(data):
    directory = os.path.dirname(str(USAGE_FILE))
    if directory:
        os.makedirs(directory, exist_ok=True)

    temp_file = str(USAGE_FILE) + ".tmp"

    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        os.replace(temp_file, str(USAGE_FILE))
    except (OSError, TypeError, ValueError):
        logger.exception("Failed to save usage")


def get_user_usage(uid):
    usage = load_usage()
    value = usage.get(str(uid), 0)

    # Supports both the old integer format and a future dict format.
    if isinstance(value, dict):
        try:
            return int(value.get("count", 0))
        except (TypeError, ValueError):
            return 0

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def increment_usage(uid):
    with usage_lock:
        usage = load_usage()
        key = str(uid)

        current = usage.get(key, 0)
        if isinstance(current, dict):
            count = int(current.get("count", 0))
            usage[key] = {"count": count + 1}
        else:
            try:
                usage[key] = int(current) + 1
            except (TypeError, ValueError):
                usage[key] = 1

        save_usage(usage)


# ============================================================
# DAILY RESET
# ============================================================

def reset_daily_limits():
    """
    Reset usage every 24h from process start.
    Kept as a daemon thread for compatibility with the existing design.
    """
    while True:
        time.sleep(86400)

        with usage_lock:
            save_usage({})

        users_dir = str(USERS_DIR)
        if not os.path.isdir(users_dir):
            continue

        for uid in os.listdir(users_dir):
            if not os.path.isdir(os.path.join(users_dir, uid)):
                continue

            try:
                bot.send_message(
                    int(uid),
                    "🎁 <b>Your free plan has been reset!</b>\n"
                    "You have <b>4 free checks</b> available for the next 24 hours."
                )
            except Exception:
                logger.exception("Failed to send reset notification to %s", uid)


threading.Thread(target=reset_daily_limits, daemon=True).start()


# ============================================================
# UI HELPERS
# ============================================================

def get_uptime():
    delta = datetime.now() - START_TIME
    total_seconds = max(0, int(delta.total_seconds()))

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{hours}h {minutes}m {seconds}s"


def get_total_users():
    users_dir = str(USERS_DIR)
    if not os.path.isdir(users_dir):
        return 0

    return sum(
        1 for name in os.listdir(users_dir)
        if os.path.isdir(os.path.join(users_dir, name))
    )


def get_progress_bar(percent):
    percent = max(0, min(100, percent))
    bar_length = 15
    filled = int(bar_length * percent / 100)
    return "🟢" * filled + "⚪" * (bar_length - filled)


def generate_status_text(uid):
    with file_lock:
        stats = user_stats.get(uid)

        if not stats:
            return "❌ Session expired."

        checked = int(stats.get("checked", 0))
        total = int(stats.get("total", 0))
        elapsed = max(0.001, time.time() - stats.get("start_time", time.time()))

        percent = (checked / total * 100) if total else 0
        cpm = int((checked / elapsed) * 60)

        time_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))

        detailed_hits = stats.get("detailed_hits", {})
        kw_list = sorted(
            detailed_hits.items(),
            key=lambda item: item[1],
            reverse=True
        )

        top_kws = "\n".join(
            f"• {k}: {v}" for k, v in kw_list[:4]
        )
        more_kws = (
            f"\n+ {len(kw_list) - 4} more services"
            if len(kw_list) > 4
            else ""
        )

        if stats.get("stop_flag"):
            status_msg = "Stopped 🛑"
        elif percent >= 100:
            status_msg = "Completed ✅"
        else:
            status_msg = "Checking..."

        return (
            "📊 <b>Keyword Scan Progress</b>\n"
            f"<code>{get_progress_bar(percent)}</code> {percent:.1f}%\n"
            f"📈 <code>{checked}/{total} | CPM: {cpm}</code>\n"
            f"🕒 <code>{time_str} | {status_msg}</code>\n\n"
            f"🔥 <b>Hits Found: {stats.get('keywords_good', 0)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{top_kws if top_kws else 'Waiting for hits...'}{more_kws}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>Valid:</b> <code>{stats.get('good', 0) + stats.get('keywords_good', 0)}</code>\n"
            f"🎯 <b>Hits (KW):</b> <code>{stats.get('keywords_good', 0)}</code>\n"
            f"🔴 <b>Bad:</b> <code>{stats.get('invalid', 0)}</code> | "
            f"🟠 <b>Retry:</b> <code>{stats.get('retries', 0)}</code>"
        )


# ============================================================
# ADMIN COMMANDS
# ============================================================

@bot.message_handler(commands=["free"])
def enable_free_mode(message):
    global FREE_MODE

    if message.from_user.id != ADMIN_ID:
        return

    FREE_MODE = True
    bot.reply_to(
        message,
        "🔓 <b>Bot is now FREE for all users</b> (4 checks / 24h)."
    )


@bot.message_handler(commands=["paid"])
def enable_paid_mode(message):
    global FREE_MODE

    if message.from_user.id != ADMIN_ID:
        return

    FREE_MODE = False
    bot.reply_to(
        message,
        "🔒 <b>Bot is now PAID only.</b> Access restricted to authorized users."
    )


@bot.message_handler(commands=["add"])
def add_user(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        parts = (message.text or "").split()

        if len(parts) != 2:
            raise ValueError

        new_id = int(parts[1])

        if new_id <= 0:
            raise ValueError

        if new_id not in AUTHORIZED_USERS:
            AUTHORIZED_USERS.add(new_id)
            persist_authorized_users()
            bot.reply_to(
                message,
                f"✅ User <code>{new_id}</code> added to authorized list."
            )
        else:
            bot.reply_to(
                message,
                f"ℹ️ User <code>{new_id}</code> is already authorized."
            )

    except (ValueError, TypeError):
        bot.reply_to(message, "⚠️ Usage: <code>/add [user_id]</code>")


@bot.message_handler(commands=["remove"])
def remove_user(message):
    if message.from_user.id != ADMIN_ID:
        return

    try:
        parts = (message.text or "").split()

        if len(parts) != 2:
            raise ValueError

        target_id = int(parts[1])

        if target_id == ADMIN_ID:
            return bot.reply_to(
                message,
                "⚠️ The main admin cannot be removed."
            )

        if target_id in AUTHORIZED_USERS:
            AUTHORIZED_USERS.remove(target_id)
            persist_authorized_users()

            bot.reply_to(
                message,
                f"🗑 User <code>{target_id}</code> removed."
            )
        else:
            bot.reply_to(
                message,
                f"ℹ️ User <code>{target_id}</code> is not authorized."
            )

    except (ValueError, TypeError):
        bot.reply_to(message, "⚠️ Usage: <code>/remove [user_id]</code>")


@bot.message_handler(commands=["list"])
def list_users(message):
    if message.from_user.id != ADMIN_ID:
        return

    if not AUTHORIZED_USERS:
        return bot.reply_to(
            message,
            "📜 <b>The authorized list is empty.</b>"
        )

    msg_text = (
        "📋 <b>Authorized Users:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )

    for uid in sorted(AUTHORIZED_USERS):
        try:
            chat = bot.get_chat(uid)
            username = (
                f"@{chat.username}"
                if chat.username
                else "No Username"
            )
        except Exception:
            username = "(Info Hidden)"

        msg_text += f"👤 <code>{uid}</code> | {username}\n"

    bot.send_message(message.chat.id, msg_text)


# ============================================================
# USER COMMANDS
# ============================================================

@bot.message_handler(commands=["start"])
def welcome(message):
    uid = message.from_user.id
    uptime = get_uptime()
    total_users = get_total_users()
    auth_status = "✅ Authorized" if is_authorized(uid) else "❌ Free Plan"

    welcome_text = (
        "🐰 <b>Welcome to Bunny Checker Version 0.1</b> 🐰\n\n"
        "🤖 <b>Your Microsoft Keyword Scanner</b>\n\n"
        "🛡 <b>Bot Health:</b>\n"
        "🟢 <b>Status:</b> Online\n"
        f"🕒 <b>Uptime:</b> {uptime}\n"
        f"👤 <b>Total Users:</b> {total_users}\n"
        f"🔑 <b>Your Rank:</b> {auth_status}\n\n"
        "💎 <b>Features:</b>\n"
        f"🔹 Multi-threaded ({THREADS} threads)\n"
        "🔹 Keyword Extraction & Validation\n"
        f"🔹 Supports up to {MAX_COMBO_LINES:,} lines\n\n"
        "📋 <b>Commands:</b>\n"
        "/check – Start a new scan 🚀\n"
        "/cancel – Cancel scan ❌\n\n"
    )

    if uid == ADMIN_ID:
        welcome_text += (
            "🛠 <b>Admin Controls:</b>\n"
            "<code>/add [id]</code> – Authorize user\n"
            "<code>/remove [id]</code> – Revoke access\n"
            "<code>/paid</code> – Paid Plans Only\n"
            "<code>/free</code> – Free access to all\n"
            "<code>/list</code> – View authorized users\n\n"
        )

    welcome_text += (
        "👑 <b>Dev:</b> @ce_q3\n"
        "🤖 <b>Channel:</b> @Hacking_morocco"
    )

    try:
        with open("logo.png", "rb") as photo:
            bot.send_photo(
                message.chat.id,
                photo,
                caption=welcome_text
            )
    except (OSError, telebot.apihelper.ApiTelegramException) as exc:
        logger.warning("Welcome photo failed: %s", exc)
        bot.reply_to(message, welcome_text)


@bot.message_handler(commands=["check"])
def start_check(message):
    uid = message.chat.id

    # Paid mode: only authorized users.
    if not FREE_MODE and not is_authorized(uid):
        return bot.reply_to(
            message,
            "⚠️ <b>Access Denied</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "This bot is currently for <b>paid members only</b>.\n\n"
            "To purchase a subscription, contact the admin."
        )

    # Free mode: 4 checks per usage period.
    if not is_authorized(uid):
        usage_count = get_user_usage(uid)

        if usage_count >= 4:
            return bot.reply_to(
                message,
                "⚠️ <b>Limit Reached</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "You finished your free plan. "
                "Wait for the next reset or contact the admin."
            )

        increment_usage(uid)

    normalize_paths(uid)

    with file_lock:
        user_stats[uid] = {
            "api_type": 1,
            "stop_flag": False,
            "checked": 0,
            "total": 0,
            "good": 0,
            "invalid": 0,
            "keywords_good": 0,
            "detailed_hits": {},
            "retries": 0,
            "errors": 0,
            "start_time": time.time(),
        }

    try:
        bot.send_message(
            uid,
            "🎯 <b>Keyword Scan Initialized.</b>\n"
            "Please send your <b>Keywords</b> (Text or .txt):",
            parse_mode="HTML",
            timeout=60
        )
    except requests.exceptions.ReadTimeout:
        print("Telegram API timeout while sending message")

    bot.register_next_step_handler(message, process_keywords_input)



@bot.message_handler(commands=["cancel"])
def cancel_scan(message):
    uid = message.chat.id

    with active_lock:
        futures = list(active_futures.get(uid, []))
        executor = active_executors.get(uid)
        status_event = status_events.get(uid)

    with file_lock:
        stats = user_stats.get(uid)
        if stats:
            stats["stop_flag"] = True

    if status_event:
        status_event.set()

    cancelled = 0

    for future in futures:
        if future.cancel():
            cancelled += 1

    if executor is None and not futures:
        return bot.reply_to(
            message,
            "❌ No active scan found."
        )

    bot.reply_to(
        message,
        f"🛑 <b>Cancellation requested.</b>\n"
        f"Pending tasks cancelled: <code>{cancelled}</code>"
    )


# ============================================================
# ENGINE
# ============================================================

def _worker_wrapper(acc, kws, uid, paths):
    """
    Wrapper used only to make Future exceptions observable.
    The actual worker remains checker.check_account().
    """
    try:
        return checker.check_account(
            acc,
            kws,
            uid,
            paths
        )
    except Exception:
        logger.exception("Worker exception for user=%s", uid)

        with file_lock:
            stats = user_stats.get(uid)
            if stats:
                stats["errors"] = stats.get("errors", 0) + 1

        raise


def run_checker_task(uid, accounts, paths):
    # Protect against two simultaneous scans for the same user.
    with active_lock:
        if uid in active_executors:
            bot.send_message(
                uid,
                "⚠️ <b>A scan is already running.</b>\n"
                "Use /cancel first."
            )
            return

    with file_lock:
        stats = user_stats.get(uid)

        if not stats:
            return

        stats.update({
            "invalid": 0,
            "good": 0,
            "keywords_good": 0,
            "total": len(accounts),
            "checked": 0,
            "stop_flag": False,
            "start_time": time.time(),
            "detailed_hits": {},
            "retries": 0,
            "errors": 0,
        })

    board = bot.send_message(
        uid,
        "⏳ <b>Initializing Status Board...</b>"
    )

    stop_event = threading.Event()

    with active_lock:
        status_events[uid] = stop_event

    def status_updater():
        while not stop_event.wait(STATUS_UPDATE_SECONDS):
            with file_lock:
                stats = user_stats.get(uid)

            if not stats:
                break

            if stats.get("checked", 0) >= stats.get("total", 0):
                break

            try:
                bot.edit_message_text(
                    generate_status_text(uid),
                    uid,
                    board.message_id
                )
            except telebot.apihelper.ApiTelegramException as exc:
                logger.warning("Status update failed: %s", exc)

        try:
            bot.edit_message_text(
                generate_status_text(uid),
                uid,
                board.message_id
            )
        except telebot.apihelper.ApiTelegramException as exc:
            logger.warning("Final status update failed: %s", exc)

    def thread_worker():
        executor = ThreadPoolExecutor(
            max_workers=THREADS,
            thread_name_prefix=f"checker-{uid}"
        )

        futures = []

        with active_lock:
            active_executors[uid] = executor
            active_futures[uid] = futures

        try:
            with open(paths["keywords"], "r", encoding="utf-8") as f:
                kws = [line.strip() for line in f if line.strip()]

            for acc in accounts:
                with file_lock:
                    stats = user_stats.get(uid)

                if not stats or stats.get("stop_flag"):
                    break

                future = executor.submit(
                    _worker_wrapper,
                    acc,
                    kws,
                    uid,
                    paths
                )
                futures.append(future)

            # Consume every Future so exceptions are observed.
            for future in as_completed(futures):
                if future.cancelled():
                    continue

                try:
                    future.result()
                except Exception:
                    # Already logged by _worker_wrapper.
                    continue

        except Exception:
            logger.exception("Checker task failed for user=%s", uid)

            with file_lock:
                stats = user_stats.get(uid)
                if stats:
                    stats["errors"] = stats.get("errors", 0) + 1

        finally:
            # Stop accepting new work and wait for running workers.
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                # Python versions without cancel_futures.
                executor.shutdown(wait=True)

            with active_lock:
                active_futures.pop(uid, None)
                active_executors.pop(uid, None)
                status_events.pop(uid, None)

            stop_event.set()

        try:
            bot.send_message(
                uid,
                "🏁 <b>Process Ended.</b> Sending results..."
            )

            for key in ("hits", "hits_no_kw"):
                path = paths.get(key)

                if path and os.path.exists(path):
                    if os.path.getsize(path) > 0:
                        with open(path, "rb") as f:
                            bot.send_document(uid, f)

        except Exception:
            logger.exception("Failed to send final results for user=%s", uid)

    threading.Thread(
        target=status_updater,
        name=f"status-{uid}",
        daemon=True
    ).start()

    threading.Thread(
        target=thread_worker,
        name=f"engine-{uid}",
        daemon=True
    ).start()


# ============================================================
# INPUT PROCESSING
# ============================================================

def process_keywords_input(message):
    uid = message.chat.id
    paths = normalize_paths(uid)

    if message.text and message.text.strip().lower() == "/cancel":
        cancel_scan(message)
        return

    try:
        kws = []

        if (
            message.content_type == "document"
            and message.document
            and message.document.file_name
            and message.document.file_name.lower().endswith(".txt")
        ):
            file_info = bot.get_file(message.document.file_id)
            data = bot.download_file(file_info.file_path)

            kws = [
                line.strip()
                for line in data.decode("utf-8", "ignore").splitlines()
                if line.strip()
            ]

        elif message.text:
            kws = [
                item.strip()
                for line in message.text.splitlines()
                for item in line.split(",")
                if item.strip()
            ]

        else:
            bot.reply_to(
                message,
                "⚠️ Please send keywords as text or a .txt file."
            )
            return

        # Remove duplicates while preserving order.
        kws = list(dict.fromkeys(kws))

        if not kws:
            bot.reply_to(
                message,
                "⚠️ No valid keywords were found."
            )
            return

        with open(paths["keywords"], "w", encoding="utf-8") as f:
            f.write("\n".join(kws))

        bot.reply_to(
            message,
            "✅ Keywords Saved.\n"
            "Now upload your <b>Combo (.txt)</b>:"
        )

        bot.register_next_step_handler(message, handle_combo)

    except Exception as exc:
        logger.exception("Keyword input failed for user=%s", uid)

        bot.reply_to(
            message,
            "⚠️ <b>Setup Error</b>\n"
            "Please send a valid keyword file or text."
        )


def handle_combo(message):
    uid = message.chat.id
    paths = normalize_paths(uid)

    if message.text and message.text.strip().lower() == "/cancel":
        cancel_scan(message)
        return

    try:
        if not (
            message.content_type == "document"
            and message.document
            and message.document.file_name
            and message.document.file_name.lower().endswith(".txt")
        ):
            bot.reply_to(
                message,
                "⚠️ Please upload a valid .txt combo file."
            )
            return

        file_info = bot.get_file(message.document.file_id)
        data = bot.download_file(file_info.file_path)

        lines = [
            line.strip()
            for line in data.decode("utf-8", "ignore").splitlines()
            if line.strip()
        ]

        if not lines:
            bot.reply_to(
                message,
                "⚠️ The uploaded file is empty."
            )
            return

        if len(lines) > MAX_COMBO_LINES:
            bot.reply_to(
                message,
                f"⚠️ File contains <b>{len(lines):,}</b> lines.\n"
                f"Maximum allowed: <b>{MAX_COMBO_LINES:,}</b>."
            )
            return

        with open(paths["combo"], "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        for key in ("hits", "hits_no_kw"):
            result_path = paths.get(key)

            if result_path and os.path.exists(result_path):
                os.remove(result_path)

        bot.reply_to(
            message,
            f"⚡ <b>Started check for {len(lines):,} accounts.</b>"
        )

        run_checker_task(uid, lines, paths)

    except (OSError, ValueError, telebot.apihelper.ApiTelegramException):
        logger.exception("Combo input failed for user=%s", uid)

        bot.reply_to(
            message,
            "⚠️ <b>Setup Error</b>\n"
            "Could not process the uploaded file."
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print("Bunny Checker Running...")
    print(f"Threads: {THREADS}")
    print(f"Max combo lines: {MAX_COMBO_LINES:,}")
    print(f"Free mode: {FREE_MODE}")

    bot.infinity_polling(
        timeout=90,
        long_polling_timeout=5
    )
