import os, re, time, json, requests, yaml, random
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Константы / пути ──────────────────────────────────────────────────────────
STEAM_APPID = 730
PRICE_URL = "https://steamcommunity.com/market/priceoverview/"
STATE_DIR = Path(".state")
STATE_FILE = STATE_DIR / "state.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ── Утилиты ───────────────────────────────────────────────────────────────────
def rub_str_to_float(s):
    """Парсим строку цены Steam в float; None если числа нет/формат странный."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("\u202f", "").replace("\xa0", "")
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", s)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))

def send_telegram(msg: str):
    """Короткие сообщения (резюме/фолбэк)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=60)
    try:
        ok = r.json().get("ok")
    except Exception:
        ok = False
    if not ok:
        print("Telegram error:", r.text)
    return ok

def send_document(text: str, filename: str, caption: str = ""):
    """Шлёт .txt документ с полным отчётом (обходит лимит 4096 символов)."""
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
    """Оценка «сколько продано за интервал» из rolling 24h (грубая модель)."""
    if prev_sales24h is None or curr_sales24h is None or dt_minutes is None:
        return None
    try:
        factor = max(0.0, min(1.0, dt_minutes / 1440.0))
        est = curr_sales24h - prev_sales24h * (1 - factor)
        return int(est) if est > 0 else 0
    except Exception:
        return None

def build_market_names(cfg):
    """Генерируем market_hash_name для Austin 2025 по config.yaml."""
    ev = cfg["scope"]["event"]
    teams = cfg["scope"]["teams"]["include"]
    team_vars = cfg["scope"]["teams"]["variants"]
    players = cfg["scope"]["players"]["include"]
    player_vars = cfg["scope"]["players"]["variants"]

    aliases = {k.lower(): v for k, v in (cfg.get("aliases", {}).get("players", {}) or {}).items()}
    def normalize_player(p): return aliases.get(p.lower(), p)

    items = []

    def team_name(base, variant):
        if variant == "paper": return f"Sticker | {base} | {ev}"
        if variant == "holo":  return f"Sticker | {base} (Holo) | {ev}"
        if variant == "foil":  return f"Sticker | {base} (Foil) | {ev}"
        return None

    def player_name(base, variant):
        b = normalize_player(base)
        if variant == "paper": return f"Sticker | {b} | {ev}"
        if variant == "holo":  return f"Sticker | {b} (Holo) | {ev}"
        if variant == "gold":  return f"Sticker | {b} (Gold) | {ev}"
        return None

    for t in teams:
        for v in team_vars:
            n = team_name(t, v)
            if n: items.append({"name": n, "key": n})

    for p in players:
        for v in player_vars:
            n = player_name(p, v)
            if n: items.append({"name": n, "key": n})

    return items

# ── Троттлинг и ретраи ────────────────────────────────────────────────────────
class Throttler:
    def __init__(self, base_delay=2.2, jitter=0.4):
        self.base_delay = float(base_delay)
        self.jitter = float(jitter)
        self._last = 0.0  # monotonic

    def wait_slot(self):
        now = time.monotonic()
        elapsed = now - self._last
        need = self.base_delay - elapsed
        if need > 0:
            time.sleep(need)
        if self.jitter > 0:
            time.sleep(random.uniform(0, self.jitter))
        self._last = time.monotonic()

def fetch_priceoverview(name, currency, throttler: Throttler, retries=5, backoff=1.8):
    """GET с выдержкой пауз, ретраями на 429/5xx и уважением Retry-After."""
    attempt = 0
    while True:
        throttler.wait_slot()
        try:
            resp = requests.get(
                PRICE_URL,
                params={"appid": STEAM_APPID, "market_hash_name": name, "currency": currency},
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (compatible; CS2-Monitor/3.2)"}
            )
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                sleep_for = float(ra) + 0.5 if ra and ra.isdigit() else ((backoff ** attempt) * 2.5)
                sleep_for = min(max(sleep_for, 2.0), 30.0)
                time.sleep(sleep_for)
                attempt += 1
                if attempt > retries:
                    raise requests.HTTPError(f"429 after {retries} retries")
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
            if getattr(e, "response", None) and e.response is not None:
                code = e.response.status_code
                if code in (502, 503, 504):
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

# ── Основной скрипт ───────────────────────────────────────────────────────────
def main():
    cfg = yaml.safe_load(open("config.yaml", "r", encoding="utf-8"))
    ccy = int(cfg.get("currency_code", 5))
    min_sales = int(cfg.get("min_daily_sales", 1))
    change_thr = float(cfg.get("change_percent_threshold", 10))
    cooldown_hours = float(cfg.get("cooldown_hours", 6))

    req_cfg = cfg.get("request", {}) or {}
    base_delay = float(req_cfg.get("base_delay_sec", 2.2))
    jitter = float(req_cfg.get("jitter_sec", 0.4))
    retries = int(req_cfg.get("retries", 5))
    backoff = float(req_cfg.get("backoff_factor", 1.8))
    shuffle_items = bool(req_cfg.get("shuffle", True))

    throttler = Throttler(base_delay=base_delay, jitter=jitter)

    items = build_market_names(cfg)
    if shuffle_items:
        rnd = random.Random()
        rnd.shuffle(items)

    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat()

    # Полный отчёт
    report_lines = []
    report_lines.append(f"Монитор Austin 2025 | порог ≥{int(change_thr)}% | {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report_lines.append(f"позиций проверено: {len(items)} | мин. продажи/24ч: {min_sales} | задержка≈{base_delay}s±{jitter}s | повторы={retries}")
    report_lines.append("")

    # Для резюме
    changed_entries = []  # (abs_change, text_for_report, short_label)
    buy_list, sell_list = [], []
    notes = []

    for it in items:
        key = it["key"]
        name = it["name"]
        try:
            d = fetch_priceoverview(name, ccy, throttler=throttler, retries=retries, backoff=backoff)
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

            rec = state.get(key, {"last": None, "history": [], "last_alert_ts": None})
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

            change_pct = None
            if last_median and median:
                change_pct = ((median - last_median) / last_median) * 100.0

            # Обновляем state
            rec["last"] = {"median": median, "ask": ask, "sales24h": sales24h, "ts": now_iso}
            hist = rec.get("history", [])
            hist.append({"ts": now_iso, "median": median, "sales24h": sales24h})
            cutoff = now - timedelta(days=60)
            rec["history"] = [p for p in hist if datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) >= cutoff]
            state[key] = rec

            # Строка в отчёт (РУССКИЕ МЕТКИ + ПУСТАЯ СТРОКА ПОСЛЕ)
            line = f"{name}\n  медиана: {('%.2f ₽' % median) if median else '—'} | мин. листинг: {('%.2f ₽' % ask) if ask else '—'} | продажи24ч: {sales24h}"
            if last_median:
                line += f" | было (медиана): {('%.2f ₽' % last_median)}"
            if sold_since is not None:
                line += f" | продано с прошлого запуска: {sold_since} (оценка)"
            report_lines.append(line)
            report_lines.append("")  # ← пустая строка после каждого предмета

            # Изменение ±threshold + cooldown
            if change_pct is not None and abs(change_pct) >= change_thr:
                in_cd = False
                la = rec.get("last_alert_ts")
                if la:
                    try:
                        in_cd = (now - datetime.fromisoformat(la.replace("Z", "+00:00"))) < timedelta(hours=cooldown_hours)
                    except Exception:
                        in_cd = False
                if not in_cd:
                    sign = "ВЫРОС" if change_pct > 0 else "УПАЛ"
                    block = [
                        f"[{sign} {change_pct:+.1f}%] {name}",
                        f"  было: {last_median:.2f} ₽ → стало: {median:.2f} ₽",
                        f"  продано с прошлого запуска: {sold_since if sold_since is not None else '—'} | продажи24ч: {sales24h}"
                    ]
                    changed_entries.append( (abs(change_pct), "\n".join(block), f"{name} ({change_pct:+.1f}%)") )
                    if change_pct <= -change_thr:
                        buy_list.append(f"{name} ({change_pct:+.1f}%)")
                    elif change_pct >= change_thr:
                        sell_list.append(f"{name} ({change_pct:+.1f}%)")
                    rec["last_alert_ts"] = now_iso
                    state[key] = rec

        except Exception as e:
            notes.append(f"[ERROR] {name}: {e}")

    # Сохраняем состояние
    save_state(state)

    # Собираем полный отчёт
    report_lines.append("—" * 40)
    if changed_entries:
        report_lines.append("ИЗМЕНЕНИЯ ≥ порога:")
        changed_entries.sort(key=lambda x: x[0], reverse=True)
        for _, txt, _short in changed_entries:
            report_lines.append(txt)
            report_lines.append("")  # пустая строка между блоками изменений
    else:
        report_lines.append("Нет изменений ≥ порога за текущий интервал.")
    report_lines.append("")

    if notes:
        report_lines.append("ЗАМЕТКИ:")
        report_lines.extend(notes)
        report_lines.append("")

    report_lines.append(f"as_of: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    full_report = "\n".join(report_lines)

    # Сохраняем .txt (удобно для артефакта)
    fname = f"cs2_austin_report_{now.strftime('%Y%m%d_%H%M%S')}Z.txt"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(full_report)
    except Exception as e:
        print("Cannot write report file:", e)

    # Короткое резюме
    header = f"📊 Austin 2025 — изменения ≥{int(change_thr)}%"
    ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = [header,
               f"Изменений за интервал: <b>{len(changed_entries)}</b>",
               f"К покупке: <b>{len(buy_list)}</b>",
               f"К продаже: <b>{len(sell_list)}</b>",
               f"<i>{ts}</i>"]
    send_telegram("\n".join(summary))

    # Полный отчёт файлом
    ok = send_document(full_report, filename=fname, caption="Полный отчёт (txt)")
    if not ok:
        # Фолбэк: отправим кусками в <code>
        limit = 3500
        for i in range(0, len(full_report), limit):
            send_telegram("<code>" + full_report[i:i+limit] + "</code>")

if __name__ == "__main__":
    main()
