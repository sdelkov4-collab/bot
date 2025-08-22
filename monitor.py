import os
import re
import time
import json
import yaml
import random
import requests
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ────────────────────────── Константы и пути ──────────────────────────
STEAM_APPID = 730
PRICE_URL = "https://steamcommunity.com/market/priceoverview/"
STATE_DIR = Path(".state")
STATE_FILE = STATE_DIR / "state.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# ───────────────────────────── Утилиты ────────────────────────────────
def rub_str_to_float(s: str):
    """Парсинг цены Steam в float (рубли)."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("\u202f", "").replace("\xa0", "")
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", s)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def send_telegram(msg: str):
    """Короткое сообщение в Telegram (HTML)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=60,
    )
    try:
        ok = r.json().get("ok")
    except Exception:
        ok = False
    if not ok:
        print("Telegram error:", r.text)
    return ok


def send_document(text: str, filename: str, caption: str = ""):
    """Отправка полного отчёта .txt файлом (обходит лимит 4096 символов)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    files = {"document": (filename, text.encode("utf-8"), "text/plain; charset=utf-8")}
    data = {"chat_id": CHAT_ID}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    r = requests.post(url, data=data, files=files, timeout=120)
    try:
        ok = r.json().get("ok")
    except Exception:
        ok = False
    if not ok:
        print("Telegram sendDocument error:", r.text)
    return ok


def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def estimate_new_sales(prev_sales24h, curr_sales24h, dt_minutes):
    """
    Грубая оценка «сколько продано с прошлого запуска», если у нас только rolling-24h.
    """
    if prev_sales24h is None or curr_sales24h is None or dt_minutes is None:
        return None
    try:
        factor = max(0.0, min(1.0, dt_minutes / 1440.0))
        est = curr_sales24h - prev_sales24h * (1 - factor)
        return int(est) if est > 0 else 0
    except Exception:
        return None


# ────────────── Генерация market_hash_name по config.yaml ─────────────
def build_market_names(cfg):
    ev = cfg["scope"]["event"]
    teams = cfg["scope"]["teams"]["include"]
    team_vars = cfg["scope"]["teams"]["variants"]
    players = cfg["scope"]["players"]["include"]
    player_vars = cfg["scope"]["players"]["variants"]

    aliases = {k.lower(): v for k, v in (cfg.get("aliases", {}).get("players", {}) or {}).items()}

    def normalize_player(p):
        return aliases.get(p.lower(), p)

    items = []

    def team_name(base, variant):
        if variant == "paper":
            return f"Sticker | {base} | {ev}"
        if variant == "holo":
            return f"Sticker | {base} (Holo) | {ev}"
        if variant == "foil":
            return f"Sticker | {base} (Foil) | {ev}"
        return None

    def player_name(base, variant):
        b = normalize_player(base)
