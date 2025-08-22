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
                timeout=30, headers={"User-Agent": "Mozilla/5.0 (compatible; CS2-Monitor/3.3)"})
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

# ── Базовые статистики из истории ─────────────────────────────────────────────
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

def baselines_from_history(rec, now_utc, min_points=12):
    hist = rec.get("history", [])
    # 7 дней
    m7 = window_values(hist, now_utc, 7, "median")
    s7 = window_values(hist, now_utc, 7, "sales24h")
    if len(m7) < min_points or len(s7) < min_points:
        # фолбэк 3 дня
        m3 = window_values(hist, now_utc, 3, "median")
        s3 = window_values(hist, now_utc, 3, "sales24h")
        if len(m3) >= max(6, min_points//2) and len(s3) >= max(6, min_points//2):
            m_base = robust_median(m3); s_base = robust_mean(s3); used_days = 3
        else:
            # фолбэк «всё, что есть»
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
    p_cfg = sig_cfg.get("price_from_7d_median", {}) or {}
    v_cfg = sig_cfg.get("volume_spike", {}) or {}
    combo_cd_h = float(sig_cfg.get("combo_cooldown_hours", 6))
    soft_pct = float(p_cfg.get("soft_pct", 0.90))
    deep_pct = float(p_cfg.get("deep_pct", 0.85))
    p_min_pts = int(p_cfg.get("min_points", 12))
    spike_mult = float(v_cfg.get("spike_multiplier", 1.5))
    v_min_pts = int(v_cfg.get("min_points", 12))

    req_cfg = cfg.get("request", {}) or {}
    throttler = Throttler(base_delay=float(req_cfg.get("base_delay_sec",2.5)),
                          jitter=float(req_cfg.get("jitter_sec",0.5)))
    retries = int(req_cfg.get("retries",5))
    backoff = float(req_cfg.get("backoff_factor",1.8))
    shuffle_items = bool(req_cfg.get("shuffle",True))

    items = build_market_names(cfg)
    if shuffle_items:
        random.Random().shuffle(items)

    state = load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat()

    report = []
    report.append(f"Монитор Austin 2025 | {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    report.append(f"позиций: {len(items)} | min_sales/24h: {min_sales}")
    report.append("")

    # коллекции сигналов
    price_signals = []   # (severity, name, now_med, base_med, discount_pct)
    vol_signals = []     # (name, now_sales, base_sales, ratio)
    combo_signals = []   # (name, details)

    changed_entries = [] # старые сигналы изменения (если включены)
    buy_list, sell_list = [], []
    notes = []

    for it in items:
        key = it["key"]; name = it["name"]
        try:
            d = fetch_priceoverview(name, ccy, throttler, retries=retries, backoff=backoff)
            if not d.get("success"):
                notes.append(f"[WARN] {name}: success=false"); continue

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

            # БАЗОВЫЕ уровни
            base_median, base_sales, used_days, hist_len = baselines_from_history(rec, now, min_points=min(p_min_pts, v_min_pts))
            # отчётная строка (русские метки + пустая строка)
            line = f"{name}\n  медиана: {('%.2f ₽' % median) if median else '—'} | мин. листинг: {('%.2f ₽' % ask) if ask else '—'} | продажи24ч: {sales24h}"
            if base_median: line += f" | 7д медиана≈ {base_median:.2f} ₽"
            if base_sales:  line += f" | 7д ср. продажи≈ {base_sales:.0f}"
            if sold_since is not None: line += f" | продано с прошлого запуска: {sold_since} (оценка)"
            report.append(line); report.append("")

            # Сигнал цены (дисконт к 7д медиане)
            severity = None; discount_pct = None
            if median and base_median:
                discount_pct = (1 - (median / base_median)) * 100.0
                if median <= base_median * deep_pct and len(rec["history"]) >= p_min_pts:
                    severity = "deep"  # ≤85%
                elif median <= base_median * soft_pct and len(rec["history"]) >= p_min_pts:
                    severity = "soft"  # ≤90%
                if severity:
                    price_signals.append( (severity, name, median, base_median, discount_pct) )

            # Сигнал объёма (спайк > +50% к 7д среднему)
            if base_sales and len(rec["history"]) >= v_min_pts:
                ratio = sales24h / base_sales if base_sales > 0 else None
                if ratio and ratio >= spike_mult:
                    vol_signals.append( (name, sales24h, base_sales, ratio) )

            # Комбо-сигнал (оба условия)
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

            # Старый сигнал «изменение за интервал», если включён
            if enable_change:
                change_pct = None
                if last_median and median:
                    change_pct = ((median - last_median) / last_median) * 100.0
                if change_pct is not None and abs(change_pct) >= change_thr:
                    la = rec.get("last_alert_ts")
                    in_cd = False
                    if la:
                        try: in_cd = (now - datetime.fromisoformat(la.replace("Z","+00:00"))) < timedelta(hours=cooldown_hours)
                        except Exception: in_cd = False
                    if not in_cd:
                        sign = "ВЫРОС" if change_pct > 0 else "УПАЛ"
                        block = [f"[{sign} {change_pct:+.1f}%] {name}",
                                 f"  было: {last_median:.2f} ₽ → стало: {median:.2f} ₽",
                                 f"  продано с прошлого запуска: {sold_since if sold_since is not None else '—'} | продажи24ч: {sales24h}"]
                        changed_entries.append( (abs(change_pct), "\n".join(block), f"{name} ({change_pct:+.1f}%)") )
                        if change_pct <= -change_thr: buy_list.append(f"{name} ({change_pct:+.1f}%)")
                        elif change_pct >= change_thr: sell_list.append(f"{name} ({change_pct:+.1f}%)")
                        rec["last_alert_ts"] = now_iso

            # финально сохранить last
            rec["last"] = {"median": median, "ask": ask, "sales24h": sales24h, "ts": now_iso}
            state[key] = rec

        except Exception as e:
            notes.append(f"[ERROR] {name}: {e}")

    save_state(state)

    # ── Формируем отчёт и резюме ───────────────────────────────────────────────
    report.append("—"*40)
    if price_signals:
        report.append("СИГНАЛЫ ЦЕНЫ (дисконт к 7д медиане):")
        for sev, name, cur, base, disc in sorted(price_signals, key=lambda x: (x[0]!="deep", x[4])):
            tag = "ГЛУБОКИЙ" if sev=="deep" else "МЯГКИЙ"
            report.append(f"[{tag}] {name}\n  было≈{base:.2f} ₽ → сейчас {cur:.2f} ₽ (-{abs(disc):.1f}%)\n")
    else:
        report.append("Нет ценовых сигналов к 7д медиане.")
    report.append("")

    if vol_signals:
        report.append("СИГНАЛЫ ОБЪЁМА (спайк к 7д среднему):")
        for name, now_s, base_s, ratio in sorted(vol_signals, key=lambda x: x[3], reverse=True):
            report.append(f"{name}\n  продажи24ч: {now_s} vs 7д ср.: {base_s:.0f} (×{ratio:.2f})\n")
    else:
        report.append("Нет объёмных сигналов.")
    report.append("")

    if combo_signals:
        report.append("КОМБО (цена+объём):")
        for name, details in combo_signals:
            report.append(f"{name}\n  {details}\n")
    else:
        report.append("Комбо-сигналов нет.")
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

    # сохраняем .txt на раннере (на случай upload-artifact)
    fname = f"cs2_austin_report_{now.strftime('%Y%m%d_%H%M%S')}Z.txt"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(full_report)
    except Exception as e:
        print("Cannot write report file:", e)

    # Короткое резюме (влезает в лимит Телеги)
    header = "📊 Austin 2025 — сигналы"
    ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [header,
             f"Цена (к 7д мед.): {len(price_signals)}",
             f"Объёмные: {len(vol_signals)}",
             f"Комбо: {len(combo_signals)}"]
    if enable_change:
        lines.append(f"Δ за интервал: {len(changed_entries)}")
    lines.append(f"<i>{ts}</i>")
    send_telegram("\n".join(lines))

    ok = send_document(full_report, filename=fname, caption="Полный отчёт (txt)")
    if not ok:
        # фолбэк: порубим на куски в <code>
        limit = 3500
        for i in range(0, len(full_report), limit):
