import os
import json
import threading
from pathlib import Path

# Telegram configuration
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8610040728:AAG3BVwlQKrjH1Y2BNKyYdR08z__3-geQ9o")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7665891917"))

# Runtime configuration
THREADS = int(os.getenv("THREADS", "15"))
MAX_COMBO_LINES = int(os.getenv("MAX_COMBO_LINES", "70000"))
STATUS_UPDATE_SECONDS = float(os.getenv("STATUS_UPDATE_SECONDS", "8"))

# Base paths are anchored to this file, so the program does not depend
# on the directory from which Python was launched.
BASE_DIR = Path(__file__).resolve().parent
USERS_DIR = BASE_DIR / "users_data"
USAGE_FILE = BASE_DIR / "usage.json"
AUTHORIZED_USERS_FILE = BASE_DIR / "authorized_users.json"

# Shared synchronization
file_lock = threading.RLock()

# In-memory statistics
user_stats = {}

# Authorized users: persisted in authorized_users.json when modified.
def load_authorized_users():
    try:
        with AUTHORIZED_USERS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return set(int(x) for x in data)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        raw = os.getenv("AUTHORIZED_USERS", "")
        if raw.strip():
            return {int(x.strip()) for x in raw.split(",") if x.strip()}
        return set()

AUTHORIZED_USERS = load_authorized_users()

def save_authorized_users():
    AUTHORIZED_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTHORIZED_USERS_FILE.with_suffix(".tmp")
    with file_lock:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(sorted(AUTHORIZED_USERS), f, ensure_ascii=False, indent=2)
        os.replace(tmp, AUTHORIZED_USERS_FILE)

def get_user_paths(user_id):
    user_dir = USERS_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    return {
        "base": user_dir,
        "list": user_dir / "list.txt",
        "keywords": user_dir / "keywords.txt",
        "keywords_result": user_dir / "keywords_result.txt",
        "good_logins": user_dir / "good_logins.txt",
        "debug": user_dir / "debug.jsonl",
    }

def ensure_user_files(user_id):
    paths = get_user_paths(user_id)
    for key in ("list", "keywords", "keywords_result", "good_logins", "debug"):
        paths[key].touch(exist_ok=True)
    return paths
