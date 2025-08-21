import os, re, time, json, requests, yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

STEAM_APPID = 730
PRICE_URL = "https://steamcommunity.com/market/priceoverview/"
STATE_DIR = Path(".state")
STATE_FILE = STATE_DIR / "state.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def rub_str_to_float(s):
    """Парсим строку цены Steam в float; None если числа нет."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("\u202f","").replace("\xa0","")
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", s)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))

def get_priceoverview(market_hash_name: str, currency: int) -> dict:
    resp = requests.get(
        PRICE_URL,
        params={"appid": STEAM_APPID, "market_hash_name": market_hash_name, "currency": currency},
        timeout=20,
        headers={"User-Agent":"Mozilla/5.0 (compatible; CS2-Monitor/3.0)"}
    )
    resp.raise_for_status()
    return resp.json()

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode":"HTML", "disable_web_page_preview": True}, timeout=20)
    try:
        ok = r.json().get("ok")
    except Exception:
        ok = False
    if not ok:
        print("Telegram error:", r.text)
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
    Приблизительно учитываем сдвиг окна: ушло prev*(dt/1440), пришло остальное.
    """
    if prev_sales24h is None or curr_sales24h is None:
        return None
    try:
        factor = max(0.0, min(1.0, dt_minutes/1440.0))
        est = curr_sales24h - prev_sales24h*(1 - factor)
        return int(est) if est > 0 else 0
    except Exception:
        return None

def build_market_names(cfg):
    ev = cfg["scope"]["event"]
    teams = cfg["scope"]["teams"]["include"]
    team_vars = cfg["scope"]["teams"]["variants"]
    players = cfg["scope"]["players"]["include"]
    player_vars = cfg["scope"]["players"]["variants"]

    # алиасы игроков
    aliases = {k.lower(): v for k,v in (cfg.get("aliases",{}).get("players",{}) or {}).items()}
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

def main():
    cfg = yaml.safe_load(open("config.yaml","r",encoding="utf-8"))
    ccy = int(cfg.get("currency_code", 5))
    min_sales = int(cfg.get("min_daily_sales", 1))
    change_thr = float(cfg.get("change_percent_threshold", 10))
    cooldown_hours = float(cfg.get("cooldown_hours", 6))

    items = build_market_names(cfg)

    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat()

    changed_blocks = []
    buy_list, sell_list = [], []   # падение/рост >= порога
    notes = []

    for it in items:
        key = it["key"]
        name = it["name"]
        try:
            d = get_priceoverview(name, ccy)
            if not d.get("success"):
                notes.append(f"⚠️ {name}: нет данных (success=false)")
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
                    dt_minutes = (now - datetime.fromisoformat(last_ts_iso.replace("Z","+00:00"))).total_seconds()/60.0
                except Exception:
                    dt_minutes = None

            sold_since = estimate_new_sales(last_sales, sales24h, dt_minutes) if dt_minutes is not None else None

            change_pct = None
            if last_median and median:
                change_pct = ((median - last_median) / last_median) * 100.0

            # обновляем state (до алертов — чтобы не потерять точку)
            rec["last"] = {"median": median, "ask": ask, "sales24h": sales24h, "ts": now_iso}
            hist = rec.get("history", [])
            hist.append({"ts": now_iso, "median": median, "sales24h": sales24h})
            cutoff = now - timedelta(days=60)
            rec["history"] = [p for p in hist if datetime.fromisoformat(p["ts"].replace("Z","+00:00")) >= cutoff]
            state[key] = rec

            # условие изменения ±threshold + cooldown
            if change_pct is not None and abs(change_pct) >= change_thr:
                in_cd = False
                la = rec.get("last_alert_ts")
                if la:
                    try:
                        in_cd = (now - datetime.fromisoformat(la.replace("Z","+00:00"))) < timedelta(hours=cooldown_hours)
                    except Exception:
                        in_cd = False
                if not in_cd:
                    sign = "⬆️" if change_pct > 0 else "⬇️"
                    prev_txt = f"{last_median:.2f} ₽" if last_median else "—"
                    curr_txt = f"{median:.2f} ₽" if median else "—"
                    sold_txt = f"{sold_since} (оценка)" if sold_since is not None else "—"
                    block = [
                        f"<b>{name}</b> {sign} {change_pct:+.1f}%",
                        f"было: <b>{prev_txt}</b> → стало: <b>{curr_txt}</b>",
                        f"продано с прошлого запуска: <b>{sold_txt}</b>; sales24h сейчас: <b>{sales24h}</b>"
                    ]
                    changed_blocks.append("\n".join(block))

                    if change_pct <= -change_thr:
                        buy_list.append(f"{name} ({change_pct:+.1f}%)")
                    elif change_pct >= change_thr:
                        sell_list.append(f"{name} ({change_pct:+.1f}%)")

                    rec["last_alert_ts"] = now_iso
                    state[key] = rec

            time.sleep(0.8)

        except Exception as e:
            notes.append(f"⚠️ {name}: ошибка {e}")

    save_state(state)

    ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    msgs = []

    if changed_blocks:
        msgs.append("📊 <b>Изменения ≥10% (Austin 2025)</b>")
        msgs.append("\n\n".join(changed_blocks))

        summary = ["\n<b>Резюме</b>"]
        summary.append("✅ <b>К покупке</b>:\n• " + "\n• ".join(buy_list) if buy_list else "✅ <b>К покупке</b>: —")
        summary.append("💰 <b>К продаже</b>:\n• " + "\n• ".join(sell_list) if sell_list else "💰 <b>К продаже</b>: —")
        msgs.append("\n".join(summary))
    else:
        msgs.append("🤖 Нет изменений ≥10% за интервал.")

    if notes:
        msgs.append("\n📝 Заметки:\n" + "\n".join(notes))

    msgs.append(f"\n<i>{ts}</i>")
    send_telegram("\n\n".join(msgs))

if __name__ == "__main__":
    main()
