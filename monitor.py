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
def rub_str_to_float(s):
    """Парсинг цены Steam в float (в рублях). Возвращает None при неудаче."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("\u202f", "").replace("\xa0", "")
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", s)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def send_telegram(msg: str):
    """Короткое текстовое сообщение в Telegram."""
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
        if variant == "paper":
            return f"Sticker | {b} | {ev}"
        if variant == "holo":
            return f"Sticker | {b} (Holo) | {ev}"
        if variant == "gold":
            return f"Sticker | {b} (Gold) | {ev}"
        return None

    for t in teams:
        for v in team_vars:
            n = team_name(t, v)
            if n:
                items.append({"name": n, "key": n})

    for p in players:
        for v in player_vars:
            n = player_name(p, v)
            if n:
                items.append({"name": n, "key": n})

    return items


# ───────────────────── Троттлинг и ретраи запросов ────────────────────
class Throttler:
    def __init__(self, base_delay=2.5, jitter=0.5):
        self.base_delay = float(base_delay)
        self.jitter = float(jitter)
        self._last = 0.0

    def wait_slot(self):
        now = time.monotonic()
        need = self.base_delay - (now - self._last)
        if need > 0:
            time.sleep(need)
        if self.jitter > 0:
            time.sleep(random.uniform(0, self.jitter))
        self._last = time.monotonic()


def fetch_priceoverview(name, currency, throttler: Throttler, retries=5, backoff=1.8):
    attempt = 0
    while True:
        throttler.wait_slot()
        try:
            resp = requests.get(
                PRICE_URL,
                params={"appid": STEAM_APPID, "market_hash_name": name, "currency": currency},
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (compatible; CS2-Monitor/3.4)"},
            )
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                sleep_for = float(ra) + 0.5 if ra and ra.isdigit() else ((backoff ** attempt) * 2.5)
                time.sleep(max(2.0, min(sleep_for, 30.0)))
                attempt += 1
                if attempt > retries:
                    raise requests.HTTPError("429 after retries")
                continue

            if resp.status_code >= 500:
                time.sleep(min((backoff ** attempt) * 2.0, 20.0))
                attempt += 1
                if attempt > retries:
                    resp.raise_for_status()
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.HTTPError as e:
            if getattr(e, "response", None) is not None and e.response.status_code in (502, 503, 504):
                time.sleep(min((backoff ** attempt) * 2.0, 20.0))
                attempt += 1
                if attempt > retries:
                    raise
                continue
            raise

        except requests.RequestException:
            time.sleep(min((backoff ** attempt) * 2.0, 20.0))
            attempt += 1
            if attempt > retries:
                raise


# ──────────────── Подготовка статистик из истории ────────────────────
def window_values(rec_history, now_utc, days, key):
    cutoff = now_utc - timedelta(days=days)
    vals = []
    for p in rec_history:
        try:
            ts = datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
            if ts >= cutoff:
                v = p.get(key)
                if v is not None:
                    vals.append(v)
        except Exception:
            continue
    return vals


def robust_median(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None
    try:
        return statistics.median(vals)
    except Exception:
        return sum(vals) / len(vals)


def robust_mean(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def short_window(rec_history, now_utc, minutes, key):
    cutoff = now_utc - timedelta(minutes=minutes)
    vals, ts_vals = [], []
    for p in rec_history:
        try:
            ts = datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
            if ts >= cutoff:
                v = p.get(key)
                if v is not None:
                    vals.append(v)
                    ts_vals.append(ts)
        except Exception:
            continue
    return vals, ts_vals


def baselines_from_history(rec, now_utc, min_points=12):
    """7-дневные базовые уровни (или аккуратные фолбэки)."""
    hist = rec.get("history", [])
    m7 = window_values(hist, now_utc, 7, "median")
    s7 = window_values(hist, now_utc, 7, "sales24h")
    if len(m7) < min_points or len(s7) < min_points:
        m3 = window_values(hist, now_utc, 3, "median")
        s3 = window_values(hist, now_utc, 3, "sales24h")
        if len(m3) >= max(6, min_points // 2) and len(s3) >= max(6, min_points // 2):
            m_base = robust_median(m3)
            s_base = robust_mean(s3)
            used_days = 3
        else:
            all_m = [p.get("median") for p in hist if p.get("median") is not None]
            all_s = [p.get("sales24h") for p in hist if p.get("sales24h") is not None]
            m_base = robust_median(all_m)
            s_base = robust_mean(all_s)
            used_days = "all"
    else:
        m_base = robust_median(m7)
        s_base = robust_mean(s7)
        used_days = 7
    return m_base, s_base, used_days, len(hist)


# ────────────────────────────── Основной код ──────────────────────────
def main():
    cfg = yaml.safe_load(open("config.yaml", "r", encoding="utf-8"))

    # База/флаги
    ccy = int(cfg.get("currency_code", 5))
    min_sales = int(cfg.get("min_daily_sales", 1))
    change_thr = float(cfg.get("change_percent_threshold", 10))
    enable_change = bool(cfg.get("enable_change_alerts", False))
    cooldown_hours = float(cfg.get("cooldown_hours", 6))

    # Сигналы (долгие)
    sig_cfg = cfg.get("signals", {}) or {}
    p_cfg = sig_cfg.get("price_from_7d_median", {}) or {}
    v_cfg = sig_cfg.get("volume_spike", {}) or {}
    combo_cd_h = float(sig_cfg.get("combo_cooldown_hours", 6))

    soft_pct = float(p_cfg.get("soft_pct", 0.90))
    deep_pct = float(p_cfg.get("deep_pct", 0.85))
    p_min_pts = int(p_cfg.get("min_points", 12))

    spike_mult = float(v_cfg.get("spike_multiplier", 1.5))
    v_min_pts = int(v_cfg.get("min_points", 12))

    # Пампы (короткое окно)
    pump_cfg = sig_cfg.get("pump", {}) or {}
    short_minutes = int(pump_cfg.get("short_window_minutes", 120))
    pump_min_pts = int(pump_cfg.get("min_points", 4))
    price_jump_pct = float(pump_cfg.get("price_jump_pct", 0.08))
    ask_jump_pct = float(pump_cfg.get("ask_jump_pct", 0.10))
    breakout_n = int(pump_cfg.get("breakout_points", 6))
    breakout_eps = float(pump_cfg.get("breakout_extra_pct", 0.03))
    momentum_mult = float(pump_cfg.get("momentum_mult", 1.8))
    confirm_price = float(pump_cfg.get("confirm_price_pct", 0.04))
    pump_cd_min = int(pump_cfg.get("cooldown_minutes", 60))

    # Сеть/антибан
    req_cfg = cfg.get("request", {}) or {}
    throttler = Throttler(
        base_delay=float(req_cfg.get("base_delay_sec", 2.5)),
        jitter=float(req_cfg.get("jitter_sec", 0.5)),
    )
    retries = int(req_cfg.get("retries", 5))
    backoff = float(req_cfg.get("backoff_factor", 1.8))
    shuffle_items = bool(req_cfg.get("shuffle", True))

    # Область мониторинга
    items = build_market_names(cfg)
    if shuffle_items:
        random.Random().shuffle(items)

    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat()

    # Отчёт/сигналы
    report = []
    report.append(f"Монитор Austin 2025 | {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report.append(f"позиций: {len(items)} | min_sales/24ч: {min_sales}")
    report.append("")

    price_signals = []   # (severity, name, cur, base, disc%)
    vol_signals = []     # (name, now24h, base24h, ratio)
    combo_signals = []   # (name, details)
    pump_signals = []    # (name, details)

    changed_entries = []  # для старых Δ-сигналов (если включено)
    buy_list, sell_list = [], []
    notes = []

    for it in items:
        key = it["key"]
        name = it["name"]

        try:
            d = fetch_priceoverview(name, ccy, throttler, retries=retries, backoff=backoff)
            if not d.get("success"):
                notes.append(f"[WARN] {name}: success=false")
                continue

            median = rub_str_to_float(d.get("median_price"))
            ask = rub_str_to_float(d.get("lowest_price"))
            volume_str = (d.get("volume") or "0").replace(",", "")
            m = re.findall(r"\d+", volume_str)
            sales24h = int(m[0]) if m else 0
            if sales24h < min_sales:
                continue

            rec = state.get(key, {"last": None, "history": [], "last_alert_ts": None, "last_alerts": {}})
            last = rec.get("last")
            last_median = last.get("median") if last else None
            last_sales = last.get("sales24h") if last else None
            last_ts_iso = last.get("ts") if last else None

            dt_minutes = None
            if last_ts_iso:
                try:
                    dt_minutes = (now - datetime.fromisoformat(last_ts_iso.replace("Z", "+00:00"))).total_seconds() / 60.0
                except Exception:
                    dt_minutes = None

            sold_since = estimate_new_sales(last_sales, sales24h, dt_minutes)

            # ───── БАЗЫ ИЗ ПРОШЛОЙ ИСТОРИИ (до текущей точки) ─────
            hist_before = rec.get("history", [])
            tmp_rec = {"history": hist_before}

            base_median, base_sales, used_days, hist_len = baselines_from_history(
                tmp_rec, now, min_points=min(p_min_pts, v_min_pts)
            )
            short_meds, _ = short_window(hist_before, now, short_minutes, "median")
            short_sales_vals, _ = short_window(hist_before, now, short_minutes, "sales24h")
            short_base_med = robust_median(short_meds)
            short_base_sales = robust_mean(short_sales_vals)
            base_hourly = (base_sales / 24.0) if base_sales else None

            # ───── Отчётная строка ─────
            line = (
                f"{name}\n  медиана: {('%.2f ₽' % median) if median is not None else '—'}"
                f" | мин. листинг: {('%.2f ₽' % ask) if ask is not None else '—'}"
                f" | продажи24ч: {sales24h}"
            )
            if base_median is not None:
                line += f" | 7д медиана≈ {base_median:.2f} ₽"
            if base_sales is not None:
                line += f" | 7д ср. продажи≈ {base_sales:.1f}"
            if short_base_med is not None:
                line += f" | short≈ {short_base_med:.2f} ₽/{short_minutes}м"
            if sold_since is not None:
                line += f" | продано с прошлого запуска: {sold_since} (оц.)"
            report.append(line)
            report.append("")

                       # ───── КОМБО (долгие цена+объём) ─────
            if severity and (base_sales is not None) and (base_sales > 0) and len(hist_before) >= max(p_min_pts, v_min_pts):
                ratio = sales24h / base_sales
                if ratio >= spike_mult:
                    last_alerts = rec.get("last_alerts", {})
                    last_combo_iso = last_alerts.get("combo")
                    in_cd = False
                    if last_combo_iso:
                        try:
                            in_cd = (now - datetime.fromisoformat(last_combo_iso.replace("Z", "+00:00"))) < timedelta(hours=combo_cd_h)
                        except Exception:
                            in_cd = False
                    if not in_cd:
                        combo_signals.append(
                            (name, f"цена {severity} (−{abs(discount_pct):.1f}%) + объём ×{ratio:.2f} к 7д")
                        )
                        last_alerts["combo"] = now_iso
                        rec["last_alerts"] = last_alerts
