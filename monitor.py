import os, re, time, json, requests, yaml
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

def get_priceoverview(market_hash_name: str, currency: int) -> dict:
    resp = requests.get(
        PRICE_URL,
        params={"appid": STEAM_APPID, "market_hash_name": market_hash_name, "currency": currency},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CS2-Monitor/3.1)"}
    )
    resp.raise_for_status()
    return resp.json()

def send_telegram(msg: str):
    """Короткие сообщения (резюме/фолбэк)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
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
    r = requests.post(url, data=data, files=files, timeout=60)
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
    """Оценка «сколько продано за интервал» из rolling 24h.
    Приблизительная модель: из окна 'ушло' prev*(dt/1440), 'пришло' остальное.
    """
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

# ── Основной скрипт ───────────────────────────────────────────────────────────
def main():
    cfg = yaml.safe_load(open("config.yaml", "r", encoding="utf-8"))
    ccy = int(cfg.get("currency_code", 5))
    min_sales = int(cfg.get("min_daily_sales", 1))
    change_thr = float(cfg.get("change_percent_threshold", 10))
    cooldown_hours = float(cfg.get("cooldown_hours", 6))

    items = build_market_names(cfg)
    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat()

    # Для полного отчёта
    report_lines = []
    report_lines.append(f"Austin 2025 monitor | threshold ≥{int(change_thr)}% | {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report_lines.append("")

    # Для короткого резюме
    changed_entries = []  # (abs_change, text_for_report, short_label)
    buy_list, sell_list = [], []
    notes = []

    for it in items:
        key = it["key"]
        name = it["name"]
        try:
            d = get_priceoverview(name, ccy)
            if not d.get("success"):
                notes.append(f"[WARN] {name}: success=false")
                continue

            median = rub_str_to_float(d.get("median_price"))
            ask = rub_str_to_float(d.get("lowest_price"))
            volume_str = (d.get("volume") or "0").replace(",", "")
            m = re.findall(r"\d+", volume_str)
            sales24h = int(m[0]) if m else 0

            # Слишком «тихие» пропускаем (настраивается в config)
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

            # Обновляем state (сохраняем точку независимо от алерта)
            rec["last"] = {"median": median, "ask": ask, "sales24h": sales24h, "ts": now_iso}
            hist = rec.get("history", [])
            hist.append({"ts": now_iso, "median": median, "sales24h": sales24h})
            cutoff = now - timedelta(days=60)
            rec["history"] = [p for p in hist if datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) >= cutoff]
            state[key] = rec

            # Добавляем в полный отчёт «сырые» строки
            line = f"{name}\n  median: {('%.2f ₽' % median) if median else '—'} | ask: {('%.2f ₽' % ask) if ask else '—'} | sales24h: {sales24h}"
            if last_median:
                line += f" | prev_median: {('%.2f ₽' % last_median)}"
            if sold_since is not None:
                line += f" | sold_since: {sold_since} (estimate)"
            report_lines.append(line)

            # Условие изменения ±threshold + cooldown
            if change_pct is not None and abs(change_pct) >= change_thr:
                in_cd = False
                la = rec.get("last_alert_ts")
                if la:
                    try:
                        in_cd = (now - datetime.fromisoformat(la.replace("Z", "+00:00"))) < timedelta(hours=cooldown_hours)
                    except Exception:
                        in_cd = False
                if not in_cd:
                    sign = "UP" if change_pct > 0 else "DOWN"
                    # подробный блок для отчёта
                    block = [
                        f"[{sign} {change_pct:+.1f}%] {name}",
                        f"  was: {last_median:.2f} ₽ → now: {median:.2f} ₽",
                        f"  sold_since_last: {sold_since if sold_since is not None else '—'} | sales24h: {sales24h}"
                    ]
                    changed_entries.append( (abs(change_pct), "\n".join(block), f"{name} ({change_pct:+.1f}%)") )

                    # рекомендации
                    if change_pct <= -change_thr:
                        buy_list.append(f"{name} ({change_pct:+.1f}%)")
                    elif change_pct >= change_thr:
                        sell_list.append(f"{name} ({change_pct:+.1f}%)")

                    rec["last_alert_ts"] = now_iso
                    state[key] = rec

            time.sleep(0.8)  # щадим rate-limit

        except Exception as e:
            notes.append(f"[ERROR] {name}: {e}")

    # Сохраняем состояние
    save_state(state)

    # Собираем полный отчёт (текстовый файл)
    report_lines.insert(1, f"items checked: {len(items)} | min_daily_sales: {min_sales}")
    report_lines.append("")
    if changed_entries:
        report_lines.append("=== CHANGES >= threshold ===")
        changed_entries.sort(key=lambda x: x[0], reverse=True)
        for _, txt, _short in changed_entries:
            report_lines.append(txt)
        report_lines.append("")
    else:
        report_lines.append("No changes >= threshold in this interval.")
        report_lines.append("")

    if notes:
        report_lines.append("=== NOTES ===")
        report_lines.extend(notes)
        report_lines.append("")

    report_lines.append(f"as_of: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    full_report = "\n".join(report_lines)

    # Сохраним файл локально (полезно для upload-artifact шага, если включишь)
    fname = f"cs2_austin_report_{now.strftime('%Y%m%d_%H%M%S')}Z.txt"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(full_report)
    except Exception as e:
        print("Cannot write report file:", e)

    # Короткое резюме (влезает в лимит Телеги)
    header = f"📊 Austin 2025 — изменения ≥{int(change_thr)}%"
    ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = [header,
               f"Изменений за интервал: <b>{len(changed_entries)}</b>",
               f"К покупке: <b>{len(buy_list)}</b>",
               f"К продаже: <b>{len(sell_list)}</b>"]
    if buy_list:
        summary.append("\n<b>Топ к покупке</b>:\n• " + "\n• ".join(buy_list[:5]))
    if sell_list:
        summary.append("\n<b>Топ к продаже</b>:\n• " + "\n• ".join(sell_list[:5]))
    summary.append(f"\n<i>{ts}</i>")
    summary_msg = "\n".join(summary)

    # 1) Резюме коротким сообщением
    send_telegram(summary_msg)

    # 2) Полный отчёт файлом
    ok = send_document(full_report, filename=fname, caption="Полный отчёт (txt)")
    if not ok:
        # Фолбэк: если вдруг документ не ушёл — отправим текстом кусками в <code>
        limit = 3500
        for i in range(0, len(full_report), limit):
            chunk = full_report[i:i+limit]
            send_telegram("<code>" + chunk + "</code>")

if __name__ == "__main__":
    main()
