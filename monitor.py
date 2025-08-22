import os, re, time, json, requests, yaml, random, statistics
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
    if not s or not isinstance(s, str): return None
    s = s.replace("\u202f", "").replace("\xa0", "")
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", s)
    if not m: return None
    return float(m.group(1).replace(",", "."))

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=60)
    try: ok = r.json().get("ok")
    except Exception: ok = False
    if not ok: print("Telegram error:", r.text)
    return ok

def send_document(text: str, filename: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    files = {"document": (filename, text.encode("utf-8"), "text/plain; charset=utf-8")}
    data = {"chat_id": CHAT_ID}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    r = requests.post(url, data=data, files=files, timeout=120)
    try: ok = r.json().get("ok")
    except Exception: ok = False
    if not ok: print("Telegram sendDocument error:", r.text)
    return ok

def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}

def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def estimate_new_sales(prev_sales24h, curr_sales24h, dt_minutes):
    if prev_sales24h is None or curr_sales24h is None or dt_minutes is None:
        return None
    try:
        factor = max(0.0, min(1.0, dt_minutes/1440.0))
        est = curr_sales24h - prev_sales24h*(1 - factor)
        return int(est) if est > 0 else 0
    except Exception:
        return None

# ── Имена предметов ───────────────────────────────────────────────────────────
def build_market_names(cfg):
    ev = cfg["scope"]["event"]
    teams = cfg["scope"]["teams"]["include"]
    team_vars = cfg["scope"]["teams"]["variants"]
    players = cfg["scope"]["players"]["include"]
    player_vars = cfg["scope"]["players"]["variants"]

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

# ── Троттлинг/ретраи ──────────────────────────────────────────────────────────
class Throttler:
    def __init__(self, base_delay=2.5, jitter=0.5):
        self.base_delay = float(base_delay); self.jitter = float(jitter); self._last = 0.0
    def wait_slot(self):
        now = time.monotonic()
        need = self.base_delay - (now - self._last)
        if need > 0: time.sleep(need)
        if self.jitter > 0: time.sleep(random.uniform(0, self.jitter))
        self._last = time.monotonic()

def fetch_priceoverview(name, currency, throttler: Throttler, retries=5, backoff=1.8):
    attempt = 0
    while True:
        throttler.wait_slot()
        try:
            resp = requests.get(PRICE_URL,
                params={"appid": STEAM_APPID, "market_hash_name": name, "currency": currency},
                timeout=30, headers={"User-Agent": "Mozilla/5.0 (compatible; CS2-Monitor/3.4)"})
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                sleep_for = float(ra) + 0.5 if ra and ra.isdigit() else ((backoff**attempt)*2.5)
                time.sleep(max(2.0, min(sleep_for, 30.0))); attempt += 1
                if attempt > retries: raise requests.HTTPError("429 after retries")
                continue
            if resp.status_code >= 500:
                time.sleep(min((backoff**attempt)*2.0, 20.0)); attempt += 1
                if attempt > retries: resp.raise_for_status(); continue
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if getattr(e, "response", None) and e.response is not None and e.response.status_code in (502,503,504):
                time.sleep(min((backoff**attempt)*2.0, 20.0)); attempt += 1
                if attempt > retries: raise
                continue
            raise
        except requests.RequestException:
            time.sleep(min((backoff**attempt)*2.0, 20.0)); attempt += 1
            if attempt > retries: raise

# ── Вспомогательные статистики ────────────────────────────────────────────────
def window_values(rec_history, now_utc, days, key):
    cutoff = now_utc - timedelta(days=days)
    vals = []
    for p in rec_history:
        try:
            ts = datetime.fromisoformat(p["ts"].replace("Z","+00:00"))
            if ts >= cutoff:
                v = p.get(key)
                if v is not None: vals.append(v)
        except Exception:
            continue
    return vals

def robust_median(vals):
    vals = [v for v in vals if isinstance(v,(int,float))]
    if not vals: return None
    try: return statistics.median(vals)
    except Exception: return sum(vals)/len(vals)

def robust_mean(vals):
    vals = [v for v in vals if isinstance(v,(int,float))]
    if not vals: return None
    return sum(vals)/len(vals)

def short_window(rec_history, now_utc, minutes, key):
    cutoff = now_utc - timedelta(minutes=minutes)
    vals, ts_vals = [], []
    for p in rec_history:
        try:
            ts = datetime.fromisoformat(p["ts"].replace("Z","+00:00"))
            if ts >= cutoff:
                v = p.get(key)
                if v is not None:
                    vals.append(v); ts_vals.append(ts)
        except Exception:
            continue
    return vals, ts_vals

def baselines_from_history(rec, now_utc, min_points=12):
    hist = rec.get("history", [])
    m7 = window_values(hist, now_utc, 7, "median")
    s7 = window_values(hist, now_utc, 7, "sales24h")
    if len(m7) < min_points or len(s7) < min_points:
        m3 = window_values(hist, now_utc, 3, "median")
        s3 = window_values(hist, now_utc, 3, "sales24h")
        if len(m3) >= max(6, min_points//2) and len(s3) >= max(6, min_points//2):
            m_base = robust_median(m3); s_base = robust_mean(s3); used_days = 3
        else:
            all_m = [p.get("median") for p in hist if p.get("median") is not None]
            all_s = [p.get("sales24h") for p in hist if p.get("sales24h") is not None]
            m_base = robust_median(all_m); s_base = robust_mean(all_s); used_days = "all"
    else:
        m_base = robust_median(m7); s_base = robust_mean(s7); used_days = 7
    return m_base, s_base, used_days, len(hist)

# ── Основной скрипт ───────────────────────────────────────────────────────────
def main():
    cfg = yaml.safe_load(open("config.yaml","r",encoding="utf-8"))
    ccy = int(cfg.get("currency_code", 5))
    min_sales = int(cfg.get("min_daily_sales", 1))
    change_thr = float(cfg.get("change_percent_threshold", 10))
    enable_change = bool(cfg.get("enable_change_alerts", False))
    cooldown_hours = float(cfg.get("cooldown_hours", 6))

    sig_cfg = cfg.get("signals", {}) or {}
    # долгие сигналы
    p_cfg = sig_cfg.get("price_from_7d_median", {}) or {}
    v_cfg = sig_cfg.get("volume_spike", {}) or {}
    combo_cd_h = float(sig_cfg.get("combo_cooldown_hours", 6))
    soft_pct = float(p_cfg.get("soft_pct", 0.90))
    deep_pct = float(p_cfg.get("deep_pct", 0.85))
    p_min_pts = int(p_cfg.get("min_points", 12))
    spike_mult = float(v_cfg.get("spike_multiplier", 1.5))
    v_min_pts = int(v_cfg.get("min_points", 12))
    # короткие (pump)
    pump_cfg = sig_cfg.get("pump", {}) or {}
    short_minutes = int(pump_cfg.get("short_window_minutes", 120))
    pump_min_pts = int(pump_cfg.get("min_points", 4))
    price_jump_pct = float(pump_cfg.get("price_jump_pct", 0.08))
    ask_jump_pct   = float(pump_cfg.get("ask_jump_pct", 0.10))
    breakout_n     = int(pump_cfg.get("breakout_points", 6))
    breakout_eps   = float(pump_cfg.get("breakout_extra_pct", 0.03))
    momentum_mult  = float(pump_cfg.get("momentum_mult", 1.8))
    confirm_price  = float(pump_cfg.get("confirm_price_pct", 0.04))
    pump_cd_min    = int(pump_cfg.get("cooldown_minutes", 60))

    req_cfg = cfg.get("request", {}) or {}
    throttler = Throttler(base_delay=float(req_cfg.get("base_delay_sec",2.5)),
                          jitter=float(req_cfg.get("jitter_sec",0.5)))
    retries = int(req_cfg.get("retries",5))
    backoff = float(req_cfg.get("backoff_factor",1.8))
    shuffle_items = bool(req_cfg.get("shuffle",True))

    items = build_market_names(cfg)
    if shuffle_items: random.Random().shuffle(items)

    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat()

    report = []
    report.append(f"Монитор Austin 2025 | {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report.append(f"позиций: {len(items)} | min_sales/24ч: {min_sales}")
    report.append("")

    # Коллекции сигналов
    price_signals = []
    ask_signals = []
    vol_signals = []
    combo_signals = []
    pump_signals = []   # новые: (name, details)

    changed_entries = []
    buy_list, sell_list = [], []
    notes = []

    for it in items:
        key = it["key"]; name = it["name"]
        try:
            d = fetch_priceoverview(name, ccy, throttler, retries=retries, backoff=backoff)
            if not d.get("success"): notes.append(f"[WARN] {name}: success=false"); continue

            median = rub_str_to_float(d.get("median_price"))
            ask = rub_str_to_float(d.get("lowest_price"))
            volume_str = (d.get("volume") or "0").replace(",", "")
            m = re.findall(r"\d+", volume_str)
            sales24h = int(m[0]) if m else 0
            if sales24h < min_sales: continue

            rec = state.get(key, {"last": None, "history": [], "last_alert_ts": None, "last_alerts": {}})
            last = rec.get("last")
            last_median = last.get("median") if last else None
            last_sales = last.get("sales24h") if last else None
            last_ts_iso = last.get("ts") if last else None
            dt_minutes = None
            if last_ts_iso:
                try: dt_minutes = (now - datetime.fromisoformat(last_ts_iso.replace("Z","+00:00"))).total_seconds()/60.0
                except Exception: dt_minutes = None

            sold_since = estimate_new_sales(last_sales, sales24h, dt_minutes)

            # обновляем историю
            hist = rec.get("history", [])
            hist.append({"ts": now_iso, "median": median, "sales24h": sales24h})
            cutoff = now - timedelta(days=60)
            rec["history"] = [p for p in hist if datetime.fromisoformat(p["ts"].replace("Z","+00:00")) >= cutoff]

            # Базы
            base_median, base_sales, used_days, hist_len = baselines_from_history(rec, now, min_points=min(p_min_pts, v_min_pts))
            short_meds, short_ts = short_window(rec["history"], now, short_minutes, "median")
            short_sales, _ = short_window(rec["history"], now, short_minutes, "sales24h")
            short_base_med = robust_median(short_meds)
            short_base_sales = robust_mean(short_sales)
            base_hourly = (base_sales/24.0) if base_sales else None

            # Строка для отчёта
            line = f"{name}\n  медиана: {('%.2f ₽' % median) if median else '—'} | мин. листинг: {('%.2f ₽' % ask) if ask else '—'} | продажи24ч: {sales24h}"
            if base_median: line += f" | 7д медиана≈ {base_median:.2f} ₽"
            if base_sales:  line += f" | 7д ср. продажи≈ {base_sales:.0f}"
            if short_base_med: line += f" | short≈ {short_base_med:.2f} ₽/{short_minutes}м"
            if sold_since is not None: line += f" | продано с прошлого запуска: {sold_since} (оц.)"
            report.append(line); report.append("")

            # ── Долгие сигналы к 7д базам ─────────────────────────────────────
            severity = None; discount_pct = None
            if median and base_median:
                discount_pct = (1 - (median / base_median)) * 100.0
                if median <= base_median * deep_pct and len(rec["history"]) >= p_min_pts:
                    severity = "deep"
                elif median <= base_median * soft_pct and len(rec["history"]) >= p_min_pts:
                    severity = "soft"
                if severity:
                    price_signals.append( (severity, name, median, base_median, discount_pct) )

            if base_sales and len(rec["history"]) >= v_min_pts:
                ratio = sales24h / base_sales if base_sales > 0 else None
                if ratio and ratio >= spike_mult:
                    vol_signals.append( (name, sales24h, base_sales, ratio) )

            if severity and base_sales and len(rec["history"]) >= max(p_min_pts, v_min_pts):
                ratio = sales24h / base_sales if base_sales else None
                if ratio and ratio >= spike_mult:
                    last_alerts = rec.get("last_alerts", {})
                    last_combo_iso = last_alerts.get("combo")
                    in_cd = False
                    if last_combo_iso:
                        try: in_cd = (now - datetime.fromisoformat(last_combo_iso.replace("Z","+00:00"))) < timedelta(hours=combo_cd_h)
                        except Exception: in_cd = False
                    if not in_cd:
                        combo_signals.append( (name, f"цена {severity} (−{abs(discount_pct):.1f}%) + объём ×{ratio:.2f} к 7д") )
                        last_alerts["combo"] = now_iso
                        rec["last_alerts"] = last_alerts

            # ── ПАМП-СИГНАЛЫ (короткое окно) ─────────────────────────────────
            # 1) price-jump vs short base
            if median and short_base_med and len(short_meds) >= pump_min_pts:
                if median >= short_base_med * (1 + price_jump_pct):
                    pump_signals.append((name, f"PRICE-JUMP: {median:.2f} ₽ vs short {short_base_med:.2f} ₽ (+{(median/short_base_med-1)*100:.1f}%)"))

            # 2) ask-jump vs short base (ранний индикатор)
            if ask and short_base_med and len(short_meds) >= pump_min_pts:
                if ask >= short_base_med * (1 + ask_jump_pct):
                    pump_signals.append((name, f"ASK-JUMP: ask {ask:.2f} ₽ vs short {short_base_med:.2f} ₽ (+{(ask/short_base_med-1)*100:.1f}%)"))

            # 3) breakout vs last N points
            if median and len(short_meds) >= max(pump_min_pts, breakout_n):
                local_max = max(short_meds[-breakout_n:]) if breakout_n <= len(short_meds) else max(short_meds)
                if median >= local_max * (1 + breakout_eps):
                    pump_signals.append((name, f"BREAKOUT: {median:.2f} ₽ > лок.макс {local_max:.2f} ₽ (+{(median/local_max-1)*100:.1f}%)"))

            # 4) momentum по продажам + подтверждение по цене
            if base_hourly and sold_since is not None and dt_minutes and dt_minutes > 0 and last_median:
                cur_hourly = sold_since / (dt_minutes/60.0)
                if cur_hourly >= base_hourly * momentum_mult and median >= last_median * (1 + confirm_price):
                    pump_signals.append((name, f"MOMENTUM: темп {cur_hourly:.1f}/ч vs бэйз {base_hourly:.1f}/ч ×{cur_hourly/max(base_hourly,1e-9):.2f}; цена +{(median/last_median-1)*100:.1f}%"))

            # финальные обновления
            rec["last"] = {"median": median, "ask": ask, "sales24h": sales24h, "ts": now_iso}
            state[key] = rec

        except Exception as e:
            notes.append(f"[ERROR] {name}: {e}")

    save_state(state)

    # ── Формируем отчёт ────────────────────────────────────────────────────────
    report.append("—"*40)

    if price_signals:
        report.append("СИГНАЛЫ ЦЕНЫ (к 7д медиане):")
        for sev, name, cur, base, disc in sorted(price_signals, key=lambda x: (x[0]!="deep", x[4])):
            tag = "ГЛУБОКИЙ" if sev=="deep" else "МЯГКИЙ"
            report.append(f"[{tag}] {name}\n  было≈{base:.2f} ₽ → сейчас {cur:.2f} ₽ (-{abs(disc):.1f}%)\n")
    else:
        report.append("Нет ценовых сигналов к 7д медиане.")
    report.append("")

    if vol_signals:
        report.append("СИГНАЛЫ ОБЪЁМА (к 7д среднему):")
        for name, now_s, base_s, ratio in sorted(vol_signals, key=lambda x: x[3], reverse=True):
            report.append(f"{name}\n  продажи24ч: {now_s} vs 7д ср.: {base_s:.0f} (×{ratio:.2f})\n")
    else:
        report.append("Нет объёмных сигналов.")
    report.append("")

    if combo_signals:
        report.append("КОМБО (долгие цена+объём):")
        for name, details in combo_signals:
            report.append(f"{name}\n  {details}\n")
    else:
        report.append("Комбо-сигналов нет.")
    report.append("")

    if pump_signals:
        report.append("⚡ ПАМП-СИГНАЛЫ (короткое окно):")
        for name, details in pump_signals:
            report.append(f"{name}\n  {details}\n")
    else:
        report.append("Нет памп-сигналов по короткому окну.")
    report.append("")

    if enable_change:
        if changed_entries:
            report.append("ИЗМЕНЕНИЯ ≥ порога:")
            changed_entries.sort(key=lambda x: x[0], reverse=True)
            for _, txt, _ in changed_entries:
                report.append(txt); report.append("")
        else:
            report.append("Нет изменений ≥ порога за интервал.")
            report.append("")

    if notes:
        report.append("ЗАМЕТКИ:")
        report.extend(notes); report.append("")

    report.append(f"as_of: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    full_report = "\n".join(report)

    # Сохраняем .txt
    fname = f"cs2_austin_report_{now.strftime('%Y%m%d_%H%M%S')}Z.txt"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(full_report)
    except Exception as e:
        print("Cannot write report file:", e)

    # Короткое резюме
    header = "📊 Austin 2025 — сигналы"
    ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        header,
        f"Цена (к 7д): {len(price_signals)}",
        f"Объёмные: {len(vol_signals)}",
        f"Комбо: {len(combo_signals)}",
        f"Памп (short): {len(pump_signals)}",
    ]
    if enable_change:
        lines.append(f"Δ за интервал: {len(changed_entries)}")
    lines.append(f"<i>{ts}</i>")
    send_telegram("\n".join(lines))

    ok = send_document(full_report, filename=fname, caption="Полный отчёт (txt)")
    if not ok:
        limit = 3500
        for i in range(0, len(full_report), limit):
            chunk = full_report[i:i + limit]
            send_telegram("<code>" + chunk + "</code>")

if __name__ == "__main__":
    main()
