import os, re, time, json, requests, yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

STEAM_APPID = 730  # CS2
PRICE_URL = "https://steamcommunity.com/market/priceoverview/"
STATE_DIR = Path(".state")
STATE_FILE = STATE_DIR / "state.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def rub_str_to_float(s: str) -> float:
    if not isinstance(s, str):
        return 0.0
    cleaned = re.sub(r"[^\d,\.]", "", s).replace(",", ".")
    try:
        return float(cleaned) if cleaned else 0.0
    except:
        return 0.0

def get_priceoverview(market_hash_name: str, currency: int) -> dict:
    resp = requests.get(
        PRICE_URL,
        params={"appid": STEAM_APPID, "market_hash_name": market_hash_name, "currency": currency},
        timeout=20,
        headers={"User-Agent":"Mozilla/5.0 (compatible; CS2-Monitor/2.0)"}
    )
    resp.raise_for_status()
    return resp.json()

def roi_net(buy_price, sell_price, fee_pct):
    if buy_price <= 0 or sell_price <= 0:
        return None
    return (sell_price * (1 - fee_pct/100.0) - buy_price) / buy_price

def target_price_for_roi(buy_price, roi_min_pct, fee_pct):
    if buy_price <= 0: return None
    roi = roi_min_pct/100.0
    return buy_price * (1.0 + roi) / (1.0 - fee_pct/100.0)

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode":"HTML"}, timeout=20)

def load_state():
    STATE_DIR.mkdir(exist_ok=True, parents=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state):
    STATE_DIR.mkdir(exist_ok=True, parents=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def append_point(state, key, ts_iso, median_price, sales24h, keep_days=45):
    rec = state.setdefault(key, {
        "history": [],     # list of {ts, median, volume}
        "alerts": {
            "last_high_7d": {"value": 0.0, "ts": None},
            "last_high_30d": {"value": 0.0, "ts": None},
            "last_volume_spike": {"factor": 0.0, "ts": None}
        }
    })
    rec["history"].append({"ts": ts_iso, "median": median_price, "volume": sales24h})
    # prune old
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    rec["history"] = [p for p in rec["history"] if datetime.fromisoformat(p["ts"].replace("Z","+00:00")) >= cutoff]
    state[key] = rec

def within_cooldown(last_ts_iso, cooldown_hours):
    if not last_ts_iso: return False
    try:
        last_ts = datetime.fromisoformat(last_ts_iso.replace("Z","+00:00"))
        return datetime.now(timezone.utc) - last_ts < timedelta(hours=cooldown_hours)
    except Exception:
        return False

def detect_new_highs_and_spikes(state, key, cfg_alerts, current_median, current_volume):
    """Return list of alert strings and update state alerts"""
    alerts_msgs = []

    windows_days = cfg_alerts.get("price_new_high_days", [7, 30])
    vol_cfg = cfg_alerts.get("volume_spike", {"lookback_days": 7, "factor": 2.0})
    cooldown = float(cfg_alerts.get("cooldown_hours", 12))

    rec = state.get(key, {})
    hist = rec.get("history", [])

    # parse history in time window
    now = datetime.now(timezone.utc)
    def window_vals(days, field):
        cutoff = now - timedelta(days=days)
        vals = [p.get(field, 0.0) for p in hist if datetime.fromisoformat(p["ts"].replace("Z","+00:00")) >= cutoff]
        return vals

    # Price new highs
    for d in windows_days:
        vals = window_vals(d, "median")
        prev_max = max(vals) if vals else 0.0
        tag = f"{d}d"
        alert_key = "last_high_7d" if d==7 else ("last_high_30d" if d==30 else f"last_high_{d}d")

        last_alert = rec.get("alerts", {}).get(alert_key, {"value":0.0,"ts":None})
        # new strict high and not within cooldown
        if current_median > prev_max and not within_cooldown(last_alert.get("ts"), cooldown):
            alerts_msgs.append(f"📈 <b>NEW HIGH {tag}</b>: median {current_median:.2f} ₽ (старый максимум {prev_max:.2f} ₽)")
            # update
            rec["alerts"][alert_key] = {"value": current_median, "ts": datetime.now(timezone.utc).isoformat()}

    # Volume spike
    lb_days = float(vol_cfg.get("lookback_days", 7))
    factor = float(vol_cfg.get("factor", 2.0))
    vol_vals = window_vals(lb_days, "volume")
    avg_vol = sum(vol_vals)/len(vol_vals) if vol_vals else 0.0
    last_vol_alert = rec.get("alerts", {}).get("last_volume_spike", {"factor":0.0,"ts":None})
    if avg_vol > 0 and current_volume >= factor * avg_vol and not within_cooldown(last_vol_alert.get("ts"), cooldown):
        spike_factor = current_volume / avg_vol
        alerts_msgs.append(f"🔥 <b>VOLUME SPIKE</b>: {current_volume} за 24ч (≈×{spike_factor:.1f} от {lb_days}д ср.)")
        rec["alerts"]["last_volume_spike"] = {"factor": spike_factor, "ts": datetime.now(timezone.utc).isoformat()}

    state[key] = rec
    return alerts_msgs

def main():
    cfg = yaml.safe_load(open("config.yaml","r",encoding="utf-8"))
    fee = float(cfg.get("steam_fee_pct", 15))
    ccy = int(cfg.get("currency_code", 5))
    min_sales = int(cfg.get("min_daily_sales", 100))
    thr_short = float(cfg["thresholds"]["short_roi_net_min_pct"])
    thr_long  = float(cfg["thresholds"]["long_roi_net_min_pct"])
    alerts_cfg = cfg.get("alerts", {})

    state = load_state()

    ok_lines, note_lines = [], []

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    for it in cfg.get("items", []):
        name = it.get("name") or it.get("market_hash_name")
        mhn  = it.get("market_hash_name")
        entry_limit = float(it.get("entry_limit_rub", 0) or 0)

        try:
            d = get_priceoverview(mhn, ccy)
            if not d.get("success"):
                note_lines.append(f"⚠️ {name}: нет данных (success=false)")
                continue

            lowest = rub_str_to_float(d.get("lowest_price","0"))
            median = rub_str_to_float(d.get("median_price","0"))
            volume_str = (d.get("volume","0") or "0").replace(",", "")
            m = re.findall(r"\d+", volume_str)
            daily_sales = int(m[0]) if m else 0

            # записываем точку в историю (до фильтра ликвидности — чтобы помнить даже «тихий» день)
            key = mhn
            append_point(state, key, now_iso, median, daily_sales)

            if daily_sales < min_sales:
                note_lines.append(f"⏳ {name}: ликвидность низкая (sales24h={daily_sales} < {min_sales})")
                continue

            # основной блок сигнала
            roi_now = ( (median * (1 - fee/100.0) - lowest) / lowest ) if (lowest>0 and median>0) else None
            t_short = ( lowest * (1 + thr_short/100.0) / (1 - fee/100.0) ) if lowest>0 else None
            t_long  = ( lowest * (1 + thr_long/100.0)  / (1 - fee/100.0) ) if lowest>0 else None

            # детекции
            alerts_msgs = detect_new_highs_and_spikes(state, key, alerts_cfg, median, daily_sales)

            lines = [f"<b>{name}</b>",
                     f"ask: <b>{lowest:.2f} ₽</b> | median: <b>{median:.2f} ₽</b> | sales24h: <b>{daily_sales}</b>"]

            if entry_limit and lowest <= entry_limit:
                lines.append(f"✅ ENTRY: ask ≤ {entry_limit:.2f} ₽")

            if roi_now is not None:
                lines.append(f"ROI_now (median→нетто): <b>{roi_now*100:.1f}%</b>")

            if t_short:
                if median >= t_short:
                    lines.append(f"🎯 SHORT достигнут: median ≥ {t_short:.2f} ₽ (порог {thr_short}%)")
                else:
                    lines.append(f"short target: {t_short:.2f} ₽ (ROI_net ≥ {thr_short}%)")

            if t_long:
                if median >= t_long:
                    lines.append(f"🏁 LONG достигнут: median ≥ {t_long:.2f} ₽ (порог {thr_long}%)")
                else:
                    lines.append(f"long target: {t_long:.2f} ₽ (ROI_net ≥ {thr_long}%)")

            # добавим алерты (если есть)
            if alerts_msgs:
                lines.append("—")
                lines += alerts_msgs

            ok_lines.append("\n".join(lines))
            time.sleep(1.2)

        except Exception as e:
            note_lines.append(f"⚠️ {name}: ошибка {e}")

    # сохранить состояние после прохода
    save_state(state)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if ok_lines:
        send_telegram("📈 <b>CS2 мониторинг</b>\n" + "\n".join(ok_lines) + f"\n\n<i>{ts}</i>")
    if note_lines:
        send_telegram("📝 Заметки:\n" + "\n".join(note_lines) + f"\n\n<i>{ts}</i>")

if __name__ == "__main__":
    main()
