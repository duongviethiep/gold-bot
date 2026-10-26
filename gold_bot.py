import os
import sys
import csv
import json
import copy
import math
import time
import requests
from datetime import datetime, timezone, timedelta

# ==============================================================================
# CẤU HÌNH
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TV_SYMBOL = os.getenv("TV_SYMBOL", "XAUUSD")
TV_EXCHANGE = os.getenv("TV_EXCHANGE", "PEPPERSTONE")
TV_SCREENER = os.getenv("TV_SCREENER", "cfd")

# "auto"  : chạy định kỳ mỗi giờ (phân tích + tạo tín hiệu + báo cáo)
# "poll"  : chạy mỗi phút (cập nhật trạng thái lệnh + trả lời /check)
# "check" : báo cáo trạng thái lệnh ngay
BOT_MODE = os.getenv("BOT_MODE", "auto")

LOT_SIZE = 0.01
LOT_VALUE_PER_DOLLAR = 1.0      # 0.01 lot XAU/USD: $1.00 biên độ = $1.00 P&L
MAX_PENDING_HOURS = 4           # Lệnh Limit ở Multi Mode chờ quá 4 giờ không khớp sẽ tự hủy
TRADES_FILE = "daily_trades.json"

# --- Tham số khớp lệnh (bản vá) ---
CANDLE_TFS = (1, 5, 15, 60)     # Các khung nến (phút) dùng để dò giá chạm Entry/SL/TP
MIN_CANDLE_AGE = 8              # Bỏ qua nến vừa mở dưới 8 giây (dữ liệu có thể chưa cập nhật)
MULTI_MIN_GAP_ATR = 0.5         # Multi Mode: không mở thêm lệnh cùng hướng nếu Entry cách lệnh đang mở < 0.5 ATR
NOTIFY_EVENTS = os.getenv("NOTIFY_EVENTS", "1") != "0"   # Gửi thông báo khi lệnh khớp / chạm TP / chạm SL / bị hủy

# --- Bộ lọc rủi ro ---
TREND_FILTER = os.getenv("TREND_FILTER", "1") != "0"   # Chỉ vào lệnh THUẬN xu hướng EMA20/EMA50 (bỏ 2 nhánh đánh ngược xu hướng)
NO_HEDGE = os.getenv("NO_HEDGE", "1") != "0"           # Multi Mode: không giữ BUY và SELL cùng lúc

# --- Đo lường (nhật ký lệnh dài hạn, KHÔNG reset theo ngày) ---
JOURNAL_FILE = "trade_journal.csv"


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# Chi phí giả định mỗi lệnh (spread + phí, đơn vị USD/oz). CHỈNH theo sàn thực tế của bạn.
ASSUMED_COST_USD = _env_float("ASSUMED_COST_USD", 0.30)
ATR_MODE = os.getenv("ATR_MODE", "real").lower()   # "real": ATR(14) của TradingView | "proxy": công thức cũ
MIN_SAMPLE = 30                                    # Dưới ngưỡng này thống kê chưa đủ tin cậy

_JOURNAL_BUFFER = []     # Dòng nhật ký chờ ghi (chỉ ghi khi lưu trạng thái)
_TG_COMMANDS = set()     # Lệnh Telegram đọc được trong lần chạy này

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


# ==============================================================================
# TIỆN ÍCH
# ==============================================================================
def safe_float(val, default=0.0, ndigits=2):
    """Ép kiểu an toàn, chống lỗi NoneType khi TradingView trả về null."""
    if val is None:
        return default
    try:
        return round(float(val), ndigits)
    except (ValueError, TypeError):
        return default


def _parse_utc(utc_str):
    clean_str = str(utc_str).replace("Z", "").replace("T", " ").split(".")[0]
    return datetime.strptime(clean_str, TS_FORMAT)


def utc_str_to_ts(utc_str):
    """Chuỗi giờ UTC -> epoch (giây). Trả về None nếu không đọc được."""
    if not utc_str:
        return None
    try:
        return int(_parse_utc(utc_str).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def ts_to_utc_str(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(TS_FORMAT)


def format_vn_time(utc_str):
    """Chuyển chuỗi giờ UTC sang giờ Việt Nam (UTC+7) dạng '11:01'."""
    if not utc_str:
        return "N/A"
    try:
        dt_vn = _parse_utc(utc_str) + timedelta(hours=7)
        return dt_vn.strftime("%H:%M")
    except Exception:
        return str(utc_str)


def format_vn_dt(utc_str):
    """Chuyển chuỗi giờ UTC sang giờ VN kèm ngày, dạng '26/10 11:01'."""
    if not utc_str:
        return "N/A"
    try:
        dt_vn = _parse_utc(utc_str) + timedelta(hours=7)
        return dt_vn.strftime("%d/%m %H:%M")
    except Exception:
        return str(utc_str)


def session_of(hour_utc):
    """Phân loại phiên giao dịch theo giờ UTC (xấp xỉ, không điều chỉnh giờ mùa hè)."""
    if 0 <= hour_utc < 7:
        return "ASIA"
    if 7 <= hour_utc < 12:
        return "LONDON"
    if 12 <= hour_utc < 16:
        return "OVERLAP"
    if 16 <= hour_utc < 21:
        return "NY"
    return "LATE"


def is_weekend_market_closed(now_utc):
    """
    Kiểm tra giờ đóng cửa cuối tuần của thị trường Vàng (XAU/USD) theo giờ UTC:
    - Thứ Sáu (weekday == 4): từ 23:00 UTC trở đi sàn nghỉ.
    - Thứ Bảy (weekday == 5): nghỉ toàn bộ cả ngày.
    - Chủ Nhật (weekday == 6): nghỉ từ 00:00 đến 21:59 UTC.
    """
    wd = now_utc.weekday()
    hr = now_utc.hour
    if wd == 4 and hr >= 23:
        return True
    if wd == 5:
        return True
    if wd == 6 and hr < 22:
        return True
    return False


# ==============================================================================
# DỮ LIỆU TRADINGVIEW
# ==============================================================================
def compute_tv_recommendation(val):
    if val is None:
        return "NEUTRAL"
    if val > 0.5:
        return "STRONG_BUY"
    if val > 0.1:
        return "BUY"
    if val >= -0.1:
        return "NEUTRAL"
    if val >= -0.5:
        return "SELL"
    return "STRONG_SELL"


def fetch_tv_direct_scan(symbol, exchange, screener, interval_str):
    """Gọi trực tiếp API Scanner của TradingView bằng thư viện requests."""
    suffix = "|60" if interval_str == "1h" else ("|240" if interval_str == "4h" else "|60")

    base_cols = [
        "Recommend.All", "RSI", "ADX", "EMA10", "EMA20", "EMA50",
        "open", "high", "low", "close",
        "Pivot.M.Classic.S1", "Pivot.M.Classic.R1",
        "BB.upper", "BB.lower",
    ]
    cols = [f"{c}{suffix}" for c in base_cols]

    url = f"https://scanner.tradingview.com/{screener}/scan"
    payload = {
        "symbols": {"tickers": [f"{exchange}:{symbol}"], "query": {"types": []}},
        "columns": cols,
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"TradingView Scanner HTTP {resp.status_code}: {resp.text}")

    res_json = resp.json()
    data_list = res_json.get("data", [])
    if not data_list or not data_list[0].get("d"):
        raise Exception(f"TradingView không trả về dữ liệu cho {exchange}:{symbol}")

    vals = data_list[0]["d"]
    indicators = {}
    for idx, col_name in enumerate(base_cols):
        indicators[col_name] = vals[idx] if idx < len(vals) else None

    rec_str = compute_tv_recommendation(indicators.get("Recommend.All"))
    return indicators, {"RECOMMENDATION": rec_str}


def fetch_tv_real_atr(symbol, exchange, screener, interval_str):
    """ATR(14) thật từ TradingView. Không khả dụng -> None (bot dùng ATR ước lượng cũ)."""
    if ATR_MODE != "real":
        return None
    suffix = "|240" if interval_str == "4h" else "|60"
    try:
        url = f"https://scanner.tradingview.com/{screener}/scan"
        payload = {
            "symbols": {"tickers": [f"{exchange}:{symbol}"], "query": {"types": []}},
            "columns": [f"ATR{suffix}"],
        }
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"ATR thật không khả dụng (HTTP {resp.status_code}), dùng ATR ước lượng.")
            return None
        data_list = resp.json().get("data", [])
        if not data_list or not data_list[0].get("d"):
            return None
        val = safe_float(data_list[0]["d"][0], default=None)
        if val is None or val < 1.0:
            return None
        return val
    except Exception as e:
        print(f"Lỗi lấy ATR thật, dùng ATR ước lượng: {e}")
        return None


def _exchange_candidates(exchange):
    exchanges_to_try = [exchange]
    for fallback in ["PEPPERSTONE", "OANDA", "TVC", "FOREXCOM", "FX_IDC"]:
        if fallback not in exchanges_to_try:
            exchanges_to_try.append(fallback)
    return exchanges_to_try


def get_tv_analysis_with_retry(symbol=TV_SYMBOL, exchange=TV_EXCHANGE, screener=TV_SCREENER,
                               interval_str="1h", max_retries=3):
    """Lấy dữ liệu kỹ thuật và giá từ TradingView. Thử lại 3 lần và dự phòng các sàn lớn."""
    exchanges_to_try = _exchange_candidates(exchange)
    last_err = None
    for attempt in range(1, max_retries + 1):
        for ex in exchanges_to_try:
            try:
                print(f"[{attempt}/{max_retries}] Đang lấy dữ liệu TradingView ({symbol} - Sàn: {ex} - Khung: {interval_str})...")
                indicators, summary = fetch_tv_direct_scan(symbol, ex, screener, interval_str)
                if indicators and indicators.get("close") is not None:
                    print(f"-> Thành công lấy dữ liệu từ TradingView sàn {ex}")
                    indicators["ATR_REAL"] = fetch_tv_real_atr(symbol, ex, screener, interval_str)
                    return ex, indicators, summary
            except Exception as e:
                last_err = e
                continue
        print(f"-> Đợi 5 giây rồi thử lại do lỗi: {last_err}")
        time.sleep(5)
    raise Exception(f"Không thể kết nối TradingView cho {symbol} sau {max_retries} lần thử: {last_err}")


def fetch_tv_candles_once(symbol, exchange, screener):
    """
    Lấy High/Low/Close của nến ĐANG CHẠY ở nhiều khung (1m, 5m, 15m, 1h) trong 1 request.
    Trả về {"exchange", "price", "tf": {phút: (high, low)}}.
    """
    cols = [f"{f}|{tf}" for tf in CANDLE_TFS for f in ("high", "low", "close")]
    url = f"https://scanner.tradingview.com/{screener}/scan"
    payload = {
        "symbols": {"tickers": [f"{exchange}:{symbol}"], "query": {"types": []}},
        "columns": cols,
    }
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"TradingView Scanner HTTP {resp.status_code}: {resp.text}")

    data_list = resp.json().get("data", [])
    if not data_list or not data_list[0].get("d"):
        raise Exception(f"TradingView không trả về dữ liệu nến cho {exchange}:{symbol}")

    vals = data_list[0]["d"]

    def pick(i):
        return vals[i] if i < len(vals) else None

    tf_ranges = {}
    price = None
    for i, tf in enumerate(CANDLE_TFS):
        h = safe_float(pick(i * 3), default=None)
        l = safe_float(pick(i * 3 + 1), default=None)
        c = safe_float(pick(i * 3 + 2), default=None)
        if h is not None and l is not None and h >= l:
            tf_ranges[tf] = (h, l)
        if price is None and c is not None:
            price = c

    if price is None:
        raise Exception(f"TradingView không trả về giá cho {exchange}:{symbol}")
    return {"exchange": exchange, "price": price, "tf": tf_ranges}


def get_candles_with_retry(symbol=TV_SYMBOL, exchange=TV_EXCHANGE, screener=TV_SCREENER, max_retries=2):
    exchanges_to_try = _exchange_candidates(exchange)
    last_err = None
    for attempt in range(1, max_retries + 1):
        for ex in exchanges_to_try:
            try:
                return fetch_tv_candles_once(symbol, ex, screener)
            except Exception as e:
                last_err = e
                continue
        if attempt < max_retries:
            time.sleep(3)
    raise Exception(f"Không lấy được nến TradingView cho {symbol}: {last_err}")


def extract_market_data(exchange_used, ind, summary):
    """Trích xuất giá hiện tại và các thông số kỹ thuật từ TradingView."""
    ind = ind or {}
    summary = summary or {}

    price = safe_float(ind.get("close"), default=2700.0, ndigits=2)
    o = safe_float(ind.get("open"), default=price, ndigits=2)
    h = safe_float(ind.get("high"), default=price, ndigits=2)
    l = safe_float(ind.get("low"), default=price, ndigits=2)

    ema20 = safe_float(ind.get("EMA20"), default=safe_float(ind.get("EMA10"), default=price, ndigits=2), ndigits=2)
    ema50 = safe_float(ind.get("EMA50"), default=safe_float(ind.get("EMA20"), default=price, ndigits=2), ndigits=2)
    rsi = safe_float(ind.get("RSI"), default=50.0, ndigits=1)
    adx = safe_float(ind.get("ADX"), default=25.0, ndigits=1)

    bb_upper = ind.get("BB.upper")
    bb_lower = ind.get("BB.lower")
    if bb_upper is not None and bb_lower is not None and bb_upper > bb_lower:
        atr_proxy = safe_float(max((bb_upper - bb_lower) / 4.0, h - l, 8.0), default=12.0, ndigits=2)
    else:
        atr_proxy = safe_float(max(h - l, 12.0), default=12.0, ndigits=2)

    atr_real = safe_float(ind.get("ATR_REAL"), default=None)
    if atr_real is not None and atr_real >= 1.0:
        atr, atr_source = atr_real, "ATR14"
    else:
        atr, atr_source = atr_proxy, "proxy"

    sup = safe_float(ind.get("Pivot.M.Classic.S1"), default=safe_float(l - 5.0, default=price - 10.0, ndigits=2), ndigits=2)
    res = safe_float(ind.get("Pivot.M.Classic.R1"), default=safe_float(h + 5.0, default=price + 10.0, ndigits=2), ndigits=2)

    recommendation = summary.get("RECOMMENDATION", "NEUTRAL")
    now = datetime.now(timezone.utc)

    return {
        "exchange": exchange_used,
        "price": price,
        "open": o,
        "high": h,
        "low": l,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "adx": adx,
        "atr": atr,
        "atr_source": atr_source,
        "atr_proxy": atr_proxy,
        "support": sup,
        "resistance": res,
        "tv_signal": recommendation,
        "time_str": now.strftime(TS_FORMAT),
        "ts": int(now.timestamp()),
    }


# ==============================================================================
# PHÂN TÍCH KỸ THUẬT & TẠO TÍN HIỆU
# ==============================================================================
def generate_technical_assessment(data):
    """Tạo 4 gạch đầu dòng nhận định kỹ thuật và tín hiệu lệnh chờ Limit."""
    price = data["price"]
    ema20 = data["ema20"]
    ema50 = data["ema50"]
    rsi = data["rsi"]
    adx = data["adx"]
    sup = data["support"]
    res = data["resistance"]
    atr = data["atr"]
    tv_signal = data["tv_signal"]

    # 1. Xu hướng
    # regime: TREND_* = thuận xu hướng EMA20/EMA50; *_COUNTER = lệnh ngược xu hướng EMA
    if price > ema20 and ema20 > ema50:
        trend = f"Tăng rõ rệt (Trên EMA20 & EMA50 - TradingView: {tv_signal})"
        bias = "BUY"
        regime = "TREND_UP"
    elif price < ema20 and ema20 < ema50:
        trend = f"Giảm rõ rệt (Dưới EMA20 & EMA50 - TradingView: {tv_signal})"
        bias = "SELL"
        regime = "TREND_DOWN"
    elif price > ema20:
        trend = f"Hồi phục ngắn hạn (TradingView: {tv_signal})"
        bias = "BUY"
        regime = "BOUNCE_COUNTER"
    else:
        trend = f"Điều chỉnh ngắn hạn (TradingView: {tv_signal})"
        bias = "SELL"
        regime = "DIP_COUNTER"
    with_trend = regime in ("TREND_UP", "TREND_DOWN")

    # 2. Động lực
    if adx < 20:
        momentum = f"RSI {rsi} - Thị trường Sideway tích lũy (ADX {adx} < 20 - Khuyến nghị đứng ngoài)"
        is_choppy = True
    else:
        status = "Quá mua" if rsi > 70 else ("Quá bán" if rsi < 30 else "Trung tính")
        momentum = f"RSI {rsi} ({status}) - Xu hướng có lực đẩy tốt (ADX {adx})"
        is_choppy = False

    # 3. Vùng cản
    key_sr = f"Kháng cự: ${res} - Hỗ trợ: ${sup}"

    # 4. Kế hoạch & Tín hiệu (SL = 1.5 ATR, TP = 2.0 ATR)
    sl_dist = round(1.5 * atr, 2)
    tp_dist = round(2.0 * atr, 2)

    created_ts = data.get("ts")
    if created_ts:
        hour_utc = datetime.fromtimestamp(created_ts, tz=timezone.utc).hour
    else:
        hour_utc = datetime.now(timezone.utc).hour

    base_signal = {
        "status": "PENDING",
        "pending_hours": 0,
        "time": data["time_str"],
        "created_ts": created_ts,
        "atr": atr,
        "atr_source": data.get("atr_source", "proxy"),
        "atr_proxy": data.get("atr_proxy", atr),
        "adx": adx,
        "rsi": rsi,
        "regime": regime,
        "with_trend": with_trend,
        "session": session_of(hour_utc),
    }

    if is_choppy:
        action = "ĐỨNG NGOÀI (Né sideway biên hẹp, không vội mở vị thế)"
        trade_signal = None
    elif TREND_FILTER and not with_trend:
        action = "ĐỨNG NGOÀI (Bộ lọc xu hướng: không đánh ngược EMA20/EMA50)"
        trade_signal = None
    elif bias == "BUY":
        entry = round(max(sup, price - round(0.4 * atr, 2)), 2)
        sl = round(entry - sl_dist, 2)
        tp = round(entry + tp_dist, 2)
        action = f"Canh Buy Limit quanh ${entry} (Không mua đuổi) - SL: ${sl} (-${sl_dist}) - TP: ${tp} (+${tp_dist})"
        trade_signal = dict(base_signal, direction="BUY", entry=entry, sl=sl, tp=tp)
    else:
        entry = round(min(res, price + round(0.4 * atr, 2)), 2)
        sl = round(entry + sl_dist, 2)
        tp = round(entry - tp_dist, 2)
        action = f"Canh Sell Limit quanh ${entry} (Không bán đuổi) - SL: ${sl} (-${sl_dist}) - TP: ${tp} (+${tp_dist})"
        trade_signal = dict(base_signal, direction="SELL", entry=entry, sl=sl, tp=tp)

    return {
        "trend": trend,
        "momentum": momentum,
        "key_sr": key_sr,
        "action": action,
        "trade_signal": trade_signal,
        "skip_reason": "CHOPPY" if is_choppy else ("COUNTER_TREND" if (TREND_FILTER and not with_trend) else None),
    }


# ==============================================================================
# LƯU / TẢI NHẬT KÝ LỆNH
# ==============================================================================
def _default_counters():
    return {"hours_scanned": 0, "signals": 0, "choppy_skips": 0, "counter_trend_skips": 0,
            "hedge_skipped": 0, "superseded": 0, "gap_skipped": 0}


def load_trades():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default_data = {
        "date": today_str,
        "last_signal_slot": None,
        "last_summary_date": None,
        "counters": _default_counters(),
        "single_mode": {"current_trade": None, "closed_trades": []},
        "multi_mode": {"open_trades": [], "closed_trades": []},
    }
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if "single_mode" not in data or not isinstance(data["single_mode"], dict):
                    data["single_mode"] = {"current_trade": None, "closed_trades": []}
                if "multi_mode" not in data or not isinstance(data["multi_mode"], dict):
                    data["multi_mode"] = {"open_trades": [], "closed_trades": []}
                data.setdefault("last_signal_slot", None)
                data.setdefault("last_summary_date", None)
                data.setdefault("counters", _default_counters())
                return data
        except Exception:
            pass
    return default_data


def save_trades(trades):
    flush_journal()
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        print(f"Lỗi khi lưu trades: {e}")


# ==============================================================================
# LÕI KHỚP LỆNH (BẢN VÁ)
# Nguyên tắc: chỉ dùng dữ liệu giá QUAN SÁT ĐƯỢC SAU thời điểm tham chiếu của lệnh
#   - Lệnh chờ (PENDING)  : thời điểm tham chiếu = lúc tạo lệnh (created_ts)
#   - Lệnh đã vào (ACTIVE): thời điểm tham chiếu = lúc khớp lệnh (filled_ts)
# Một nến chỉ được dùng nếu nó MỞ SAU thời điểm tham chiếu, để không "nhìn về quá khứ".
# ==============================================================================
def trade_ref_ts(trade, kind, now_ts):
    """Lấy thời điểm tham chiếu (epoch). Với lệnh cũ chưa có timestamp thì suy ra từ chuỗi giờ."""
    if kind == "filled":
        ts = trade.get("filled_ts") or utc_str_to_ts(trade.get("filled_time"))
        if ts:
            trade["filled_ts"] = ts
            return ts
    ts = trade.get("created_ts") or utc_str_to_ts(trade.get("time"))
    if not ts:
        ts = now_ts  # Không rõ -> bắt đầu theo dõi từ bây giờ (an toàn, không khớp ảo)
    trade["created_ts"] = ts
    return ts


def observed_window(candles, now_ts, ref_ts):
    """
    Trả về (low, high) giá đã quan sát được SAU ref_ts, hoặc (None, None) nếu chưa có dữ liệu hợp lệ.
    - Điểm giá hiện tại luôn hợp lệ nếu now_ts > ref_ts.
    - Nến khung tf chỉ hợp lệ nếu thời điểm mở nến >= ref_ts (nến không chứa dữ liệu trước khi lệnh tồn tại).
    """
    lo = hi = None
    price = candles.get("price")
    if price is not None and now_ts > ref_ts:
        lo = hi = price

    for tf, (h, l) in candles.get("tf", {}).items():
        start = now_ts - (now_ts % (tf * 60))
        if start < ref_ts:
            continue
        if now_ts - start < MIN_CANDLE_AGE:
            continue
        hi = h if hi is None else max(hi, h)
        lo = l if lo is None else min(lo, l)
    return lo, hi


def _update_excursions(trade, lo, hi):
    """Ghi nhận biên độ thuận lợi tối đa (MFE) và bất lợi tối đa (MAE) kể từ lúc khớp, đơn vị USD."""
    entry = trade["entry"]
    if trade["direction"] == "BUY":
        fav, adv = hi - entry, entry - lo
    else:
        fav, adv = entry - lo, hi - entry
    trade["mfe"] = round(max(trade.get("mfe", 0.0), fav, 0.0), 2)
    trade["mae"] = round(max(trade.get("mae", 0.0), adv, 0.0), 2)


def _mark_filled(trade, now_ts):
    trade["status"] = "ACTIVE"
    trade["filled_ts"] = now_ts
    trade["filled_time"] = ts_to_utc_str(now_ts)


def _mark_closed(trade, now_ts, pnl, outcome):
    trade["closed_ts"] = now_ts
    trade["closed_time"] = ts_to_utc_str(now_ts)
    return "CLOSED", pnl, outcome


def process_single_order(trade, candles, now_ts):
    """
    Xử lý vòng đời 1 lệnh Limit. Trả về (trạng thái mới, pnl, kết quả).
    Kết quả: "FILLED" | "HIT_SL" | "HIT_TP" | "MISSED_TP" | "".
    Quy ước thận trọng: nếu cùng 1 lần quét chạm cả SL lẫn TP thì tính là SL.
    """
    direction = trade["direction"]
    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    status = trade.get("status", "PENDING")

    if status == "PENDING":
        ref = trade_ref_ts(trade, "created", now_ts)
        lo, hi = observed_window(candles, now_ts, ref)
        if lo is None:
            return "PENDING", 0.0, ""

        if direction == "BUY":
            if lo <= entry:
                _mark_filled(trade, now_ts)
                if lo <= sl:
                    pnl = round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2)
                    return _mark_closed(trade, now_ts, pnl, "HIT_SL")
                return "ACTIVE", 0.0, "FILLED"
            if hi >= tp:
                return "CANCELLED", 0.0, "MISSED_TP"
        else:
            if hi >= entry:
                _mark_filled(trade, now_ts)
                if hi >= sl:
                    pnl = round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2)
                    return _mark_closed(trade, now_ts, pnl, "HIT_SL")
                return "ACTIVE", 0.0, "FILLED"
            if lo <= tp:
                return "CANCELLED", 0.0, "MISSED_TP"
        return "PENDING", 0.0, ""

    if status == "ACTIVE":
        ref = trade_ref_ts(trade, "filled", now_ts)
        lo, hi = observed_window(candles, now_ts, ref)
        if lo is None:
            return "ACTIVE", 0.0, ""
        _update_excursions(trade, lo, hi)

        if direction == "BUY":
            if lo <= sl:
                pnl = round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2)
                return _mark_closed(trade, now_ts, pnl, "HIT_SL")
            if hi >= tp:
                pnl = round((tp - entry) * LOT_VALUE_PER_DOLLAR, 2)
                return _mark_closed(trade, now_ts, pnl, "HIT_TP")
        else:
            if hi >= sl:
                pnl = round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2)
                return _mark_closed(trade, now_ts, pnl, "HIT_SL")
            if lo <= tp:
                pnl = round((entry - tp) * LOT_VALUE_PER_DOLLAR, 2)
                return _mark_closed(trade, now_ts, pnl, "HIT_TP")
        return "ACTIVE", 0.0, ""

    return "CANCELLED", 0.0, ""


def _is_valid_trade(t):
    return isinstance(t, dict) and all(k in t for k in ("direction", "entry", "sl", "tp"))


def evaluate_trades(trades, candles, now_ts):
    """
    Cập nhật trạng thái mọi lệnh theo dữ liệu nến. Gọi được MỖI PHÚT, không đổi bộ đếm giờ chờ.
    Trả về danh sách sự kiện [{"mode", "kind", "trade"}] để thông báo.
    """
    events = []

    # 1. Chế độ Đơn lệnh
    single = trades["single_mode"]
    curr = single.get("current_trade")
    if _is_valid_trade(curr):
        new_status, pnl, outcome = process_single_order(curr, candles, now_ts)
        if new_status == "CLOSED":
            curr["status"] = "CLOSED"
            curr["outcome"] = outcome
            curr["pnl"] = pnl
            single["closed_trades"].append(curr)
            single["current_trade"] = None
            events.append({"mode": "Đơn lệnh", "kind": outcome, "trade": curr})
        elif new_status == "CANCELLED":
            single["current_trade"] = None
            events.append({"mode": "Đơn lệnh", "kind": outcome or "CANCELLED", "trade": curr})
        elif new_status == "ACTIVE" and outcome == "FILLED":
            events.append({"mode": "Đơn lệnh", "kind": "FILLED", "trade": curr})
    elif curr is not None:
        single["current_trade"] = None

    # 2. Chế độ Đa lệnh
    multi = trades["multi_mode"]
    remaining = []
    for t in multi.get("open_trades", []):
        if not _is_valid_trade(t):
            continue
        new_status, pnl, outcome = process_single_order(t, candles, now_ts)
        if new_status == "CLOSED":
            t["status"] = "CLOSED"
            t["outcome"] = outcome
            t["pnl"] = pnl
            multi["closed_trades"].append(t)
            events.append({"mode": "Đa lệnh", "kind": outcome, "trade": t})
        elif new_status == "CANCELLED":
            events.append({"mode": "Đa lệnh", "kind": outcome or "CANCELLED", "trade": t})
        else:
            remaining.append(t)
            if new_status == "ACTIVE" and outcome == "FILLED":
                events.append({"mode": "Đa lệnh", "kind": "FILLED", "trade": t})
    multi["open_trades"] = remaining

    for ev in events:
        if ev["kind"] in ("HIT_TP", "HIT_SL", "MISSED_TP", "CANCELLED"):
            journal_record(ev["mode"], ev["trade"], ev["kind"])

    return events


def _too_close_to_existing(open_trades, signal):
    """Multi Mode: có lệnh cùng hướng đang mở với Entry quá gần tín hiệu mới không?"""
    atr = signal.get("atr") or 0.0
    min_gap = MULTI_MIN_GAP_ATR * atr
    if min_gap <= 0:
        return False
    for t in open_trades:
        if t.get("direction") == signal.get("direction") and abs(t.get("entry", 0.0) - signal["entry"]) < min_gap:
            return True
    return False


def new_hour_step(trades, new_signal, slot_key, skip_reason=None):
    """
    Bước chạy MỖI GIỜ (chỉ 1 lần cho mỗi khung giờ nhờ slot_key):
    - Single: lệnh chờ hết hạn sau 1 giờ; nếu trống thì nhận tín hiệu mới.
    - Multi : tăng bộ đếm giờ chờ, hủy lệnh chờ quá hạn, thêm tín hiệu mới (nếu không bị trùng vùng).
    Trả về True nếu đây là lần đầu xử lý khung giờ này, False nếu đã xử lý rồi.
    """
    if trades.get("last_signal_slot") == slot_key:
        return False
    trades["last_signal_slot"] = slot_key

    counters = trades.setdefault("counters", _default_counters())
    counters["hours_scanned"] = counters.get("hours_scanned", 0) + 1
    if new_signal:
        counters["signals"] = counters.get("signals", 0) + 1
    elif skip_reason == "CHOPPY":
        counters["choppy_skips"] = counters.get("choppy_skips", 0) + 1
    elif skip_reason == "COUNTER_TREND":
        counters["counter_trend_skips"] = counters.get("counter_trend_skips", 0) + 1

    single = trades["single_mode"]
    curr = single.get("current_trade")
    if isinstance(curr, dict) and curr.get("status") == "PENDING":
        journal_record("single", curr, "EXPIRED")
        single["current_trade"] = None
    if single.get("current_trade") is None and new_signal:
        single["current_trade"] = copy.deepcopy(new_signal)

    multi = trades["multi_mode"]
    kept = []
    for t in multi.get("open_trades", []):
        if t.get("status") == "PENDING":
            t["pending_hours"] = t.get("pending_hours", 0) + 1
            if t["pending_hours"] >= MAX_PENDING_HOURS:
                journal_record("multi", t, "EXPIRED")
                continue
        kept.append(t)
    multi["open_trades"] = kept

    # Chống hedge: không giữ BUY và SELL cùng lúc
    blocked = False
    if new_signal and NO_HEDGE:
        opposite = [t for t in multi["open_trades"] if t.get("direction") != new_signal["direction"]]
        if any(t.get("status") == "ACTIVE" for t in opposite):
            counters["hedge_skipped"] = counters.get("hedge_skipped", 0) + 1
            blocked = True      # Đang gồng lệnh ngược chiều -> bỏ tín hiệu mới
        elif opposite:
            for t in opposite:  # Chỉ còn lệnh chờ ngược chiều -> tín hiệu mới thay thế
                journal_record("multi", t, "SUPERSEDED")
            counters["superseded"] = counters.get("superseded", 0) + len(opposite)
            multi["open_trades"] = [t for t in multi["open_trades"] if t not in opposite]

    if new_signal and not blocked:
        if not _too_close_to_existing(multi["open_trades"], new_signal):
            multi["open_trades"].append(copy.deepcopy(new_signal))
        else:
            counters["gap_skipped"] = counters.get("gap_skipped", 0) + 1

    return True


# ==============================================================================
# ĐỊNH DẠNG BÁO CÁO
# ==============================================================================
def calc_floating_pnl(trade, current_price):
    """Tính P&L tạm tính cho lệnh đã khớp (ACTIVE) theo giá hiện tại."""
    if not trade or trade.get("status") != "ACTIVE":
        return 0.0
    entry = trade.get("entry", current_price)
    if trade.get("direction") == "BUY":
        return round((current_price - entry) * LOT_VALUE_PER_DOLLAR, 2)
    return round((entry - current_price) * LOT_VALUE_PER_DOLLAR, 2)


SEP = "━━━━━━━━━━━━━━━━━━"


def fmt_price(x):
    return f"{x:.2f}"


def fmt_usd(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):.2f}"


def fmt_r(x):
    return f"{x:+.2f}R"


def _vn_dt(utc_str):
    try:
        return _parse_utc(utc_str) + timedelta(hours=7)
    except Exception:
        return None


def fmt_when(utc_str, now_str=None):
    """Giờ VN: 'HH:MM' nếu cùng ngày với now_str, ngược lại 'dd/mm HH:MM'."""
    if not utc_str:
        return "N/A"
    dt = _vn_dt(utc_str)
    if dt is None:
        return str(utc_str)
    ref = _vn_dt(now_str) if now_str else None
    if ref is not None and dt.date() == ref.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d/%m %H:%M")


def fmt_duration(minutes):
    m = max(0, int(round(minutes)))
    if m < 60:
        return f"{m}p"
    h, mm = divmod(m, 60)
    return f"{h}h{mm:02d}p"


def _minutes_since(utc_str, now_str):
    a, b = utc_str_to_ts(utc_str), utc_str_to_ts(now_str)
    if a is None or b is None:
        return None
    return (b - a) / 60.0


def _risk_usd(t):
    return abs(t.get("entry", 0.0) - t.get("sl", 0.0)) * LOT_VALUE_PER_DOLLAR


def _r_of(pnl, t):
    risk = _risk_usd(t)
    return pnl / risk if risk else 0.0


def _levels(t):
    return f"Entry {fmt_price(t.get('entry', 0.0))} · SL {fmt_price(t.get('sl', 0.0))} · TP {fmt_price(t.get('tp', 0.0))}"


def format_closed_trade_item(idx, t, now_str=None):
    pnl = t.get("pnl", 0.0)
    win = pnl > 0 or t.get("outcome") == "HIT_TP"
    icon, tag = ("✅", "TP") if win else ("❌", "SL")
    parts = [f"Tín hiệu {fmt_when(t.get('time'), now_str)}"]
    if t.get("filled_time"):
        parts.append(f"Khớp {fmt_when(t.get('filled_time'), now_str)}")
    if t.get("closed_time"):
        parts.append(f"Chốt {fmt_when(t.get('closed_time'), now_str)}")
    return (
        f"{idx}. {icon} {t.get('direction', 'N/A')} · {_levels(t)}\n"
        f"   ↳ {' · '.join(parts)} · {tag} {fmt_usd(pnl)} ({fmt_r(_r_of(pnl, t))})"
    )


def format_active_trade_item(idx, t, current_price, now_str=None):
    direction = t.get("direction", "N/A")
    fpnl = calc_floating_pnl(t, current_price)
    icon = "🟢" if fpnl >= 0 else "🔴"
    if direction == "BUY":
        to_sl, to_tp = current_price - t.get("sl", 0.0), t.get("tp", 0.0) - current_price
    else:
        to_sl, to_tp = t.get("sl", 0.0) - current_price, current_price - t.get("tp", 0.0)

    parts = [f"Tín hiệu {fmt_when(t.get('time'), now_str)}"]
    if t.get("filled_time"):
        parts.append(f"Khớp {fmt_when(t.get('filled_time'), now_str)}")
        held = _minutes_since(t.get("filled_time"), now_str) if now_str else None
        if held is not None:
            parts.append(f"Giữ {fmt_duration(held)}")
    return (
        f"{idx}. {icon} {direction} · {_levels(t)}\n"
        f"   ↳ {' · '.join(parts)}\n"
        f"   ↳ P&L {fmt_usd(fpnl)} ({fmt_r(_r_of(fpnl, t))})"
    )


def format_pending_trade_item(idx, t, current_price, now_str=None):
    dist = abs(current_price - t.get("entry", current_price))
    parts = [f"Tín hiệu {fmt_when(t.get('time'), now_str)}"]
    waited = _minutes_since(t.get("time"), now_str) if now_str else None
    if waited is not None:
        parts.append(f"Chờ {fmt_duration(waited)}")
    parts.append(f"Cách giá {dist:.2f}")
    return (
        f"{idx}. 🕒 {t.get('direction', 'N/A')} Limit · {_levels(t)}\n"
        f"   ↳ {' · '.join(parts)}"
    )


def _render_section(icon, title, active, pending, closed, price, now_str, show_exposure=False):
    floating = round(sum(calc_floating_pnl(t, price) for t in active), 2)
    realized = round(sum(t.get("pnl", 0.0) for t in closed), 2)
    wins = len([t for t in closed if t.get("pnl", 0.0) > 0])
    losses = len(closed) - wins

    lines = [f"{icon} *{title}*"]
    if show_exposure:
        opened = active + pending
        buys = len([t for t in opened if t.get("direction") == "BUY"])
        sells = len([t for t in opened if t.get("direction") == "SELL"])
        lines.append(f"Đang mở: BUY {buys} · SELL {sells}")
        if (len([t for t in active if t.get("direction") == "BUY"]) > 0
                and len([t for t in active if t.get("direction") == "SELL"]) > 0):
            lines.append("⚠️ Đang giữ đồng thời BUY và SELL (hedge)")

    lines.append(f"▸ Đang gồng ({len(active)})")
    lines.extend([format_active_trade_item(i + 1, t, price, now_str) for i, t in enumerate(active)] or ["   —"])
    lines.append(f"▸ Chờ khớp ({len(pending)})")
    lines.extend([format_pending_trade_item(i + 1, t, price, now_str) for i, t in enumerate(pending)] or ["   —"])
    lines.append(f"▸ Đã chốt ({len(closed)}) · Thắng {wins} · Thua {losses}")
    lines.extend([format_closed_trade_item(i + 1, t, now_str) for i, t in enumerate(closed)] or ["   —"])
    lines.append(f"💰 Đã chốt {fmt_usd(realized)} · Đang gồng {fmt_usd(floating)} · Tổng {fmt_usd(realized + floating)}")
    return "\n".join(lines)


def generate_live_status_report(trades, data):
    """Báo cáo trạng thái lệnh của cả chế độ Đơn lệnh và Đa lệnh."""
    price = data["price"]
    now_str = data["time_str"]

    s_curr = trades.get("single_mode", {}).get("current_trade")
    s_active = [s_curr] if s_curr and s_curr.get("status") == "ACTIVE" else []
    s_pending = [s_curr] if s_curr and s_curr.get("status") == "PENDING" else []
    s_closed = trades.get("single_mode", {}).get("closed_trades", [])

    m_open = trades.get("multi_mode", {}).get("open_trades", [])
    m_active = [t for t in m_open if t.get("status") == "ACTIVE"]
    m_pending = [t for t in m_open if t.get("status") == "PENDING"]
    m_closed = trades.get("multi_mode", {}).get("closed_trades", [])

    header = "\n".join([
        "📊 *TRẠNG THÁI LỆNH XAU/USD*",
        f"🕐 {format_vn_time(now_str)} VN · Giá {fmt_price(price)} · {data['exchange']}",
        "📐 0.01 lot · $1 giá = $1.00 · 1R = rủi ro |Entry - SL|",
    ])
    parts = [
        header,
        _render_section("1️⃣", "ĐƠN LỆNH (tối đa 1 lệnh)", s_active, s_pending, s_closed, price, now_str),
        _render_section("2️⃣", "ĐA LỆNH (độc lập từng tín hiệu)", m_active, m_pending, m_closed, price, now_str, show_exposure=True),
        "Gõ /stats để xem thống kê lũy kế.",
    ]
    return ("\n" + SEP + "\n").join(parts)


def format_event_message(events, price, now_str):
    """Thông báo khi lệnh khớp / chạm TP / chạm SL / bị hủy."""
    lines = [
        "🔔 *CẬP NHẬT LỆNH XAU/USD*",
        f"🕐 {format_vn_time(now_str)} VN · Giá {fmt_price(price)}",
        SEP,
    ]
    for ev in events:
        t = ev["trade"]
        mode = ev["mode"]
        kind = ev["kind"]
        pnl = t.get("pnl", 0.0)
        head = f"[{mode}] {t.get('direction')} · {_levels(t)}"
        if kind == "FILLED":
            lines.append(f"🟢 KHỚP {head}")
        elif kind == "HIT_TP":
            lines.append(f"✅ CHẠM TP {fmt_usd(pnl)} ({fmt_r(_r_of(pnl, t))}) {head}")
        elif kind == "HIT_SL":
            lines.append(f"❌ CHẠM SL {fmt_usd(pnl)} ({fmt_r(_r_of(pnl, t))}) {head}")
        elif kind == "MISSED_TP":
            lines.append(f"⚪ HỦY (giá chạy tới TP khi chưa khớp) {head}")
        else:
            lines.append(f"⚪ HỦY {head}")
    lines.append("Giờ khớp/chốt là lúc bot phát hiện (quét mỗi phút).")
    return "\n".join(lines)


def format_telegram_message(tf, data, assessment, trades=None, new_slot=True):
    lines = [
        f"📊 *XAU/USD {tf} · TradingView*",
        f"🕐 {format_vn_time(data['time_str'])} VN ({data['time_str']} UTC) · {data['exchange']}",
        f"💰 Giá {fmt_price(data['price'])} · O {fmt_price(data['open'])} · H {fmt_price(data['high'])} · L {fmt_price(data['low'])}",
        SEP,
        f"Xu hướng: {assessment['trend']}",
        f"Động lực: {assessment['momentum']}",
        f"Vùng cản: {assessment['key_sr']}",
        f"Kế hoạch: {assessment['action']}",
        f"ATR {data['atr']} [{data.get('atr_source', 'proxy')}] · 0.01 lot = $1/giá",
    ]

    if tf == "1H" and trades:
        curr = trades.get("single_mode", {}).get("current_trade")
        if curr:
            if curr.get("status") == "ACTIVE":
                fpnl = calc_floating_pnl(curr, data["price"])
                st_label = f"ĐANG GỒNG ({fmt_usd(fpnl)})"
            else:
                st_label = "ĐANG CHỜ KHỚP"
            lines.append(f"📌 Đơn lệnh: {st_label} · {curr['direction']} @ {fmt_price(curr['entry'])} (SL {fmt_price(curr['sl'])} · TP {fmt_price(curr['tp'])})")
        else:
            lines.append("📌 Đơn lệnh: không có lệnh mở")
        m_open = trades.get("multi_mode", {}).get("open_trades", [])
        buys = len([t for t in m_open if t.get("direction") == "BUY"])
        sells = len([t for t in m_open if t.get("direction") == "SELL"])
        lines.append(f"📌 Đa lệnh: BUY {buys} · SELL {sells} (đang mở)")
        if not new_slot:
            lines.append("ℹ️ Khung giờ này đã có tín hiệu, lần chạy này không tạo lệnh mới.")
    return "\n".join(lines)


def format_open_trade_item(t):
    icon = "⏳" if t.get("status") == "ACTIVE" else "🕒"
    kind = "" if t.get("status") == "ACTIVE" else " Limit"
    return f"   {icon} {t.get('direction', 'N/A')}{kind} · {_levels(t)} · Tín hiệu {fmt_when(t.get('time'))}"


def generate_daily_summary(trades):
    def block(icon, title, closed, open_items):
        pnl = round(sum(t.get("pnl", 0.0) for t in closed), 2)
        wins = len([t for t in closed if t.get("pnl", 0.0) > 0])
        losses = len(closed) - wins
        winrate = (wins / len(closed) * 100) if closed else 0.0
        lines = [f"{icon} *{title}*", f"▸ Đã chốt ({len(closed)})"]
        lines.extend([format_closed_trade_item(i + 1, t) for i, t in enumerate(closed)] or ["   —"])
        lines.append(f"▸ Vị thế đang mở ({len(open_items)})")
        lines.extend([format_open_trade_item(t) for t in open_items] or ["   —"])
        lines.append(f"📈 Thắng {wins} · Thua {losses} · Winrate {winrate:.1f}% · P&L {fmt_usd(pnl)}")
        return "\n".join(lines)

    s_curr = trades.get("single_mode", {}).get("current_trade")
    header = "\n".join([
        "📋 *TỔNG KẾT PHIÊN GIAO DỊCH VÀNG (05:00 VN)*",
        f"📅 Ngày {trades.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))} · 0.01 lot · $1 giá = $1.00",
        "Chỉ tính lệnh đã khớp thực tế",
    ])
    parts = [
        header,
        block("1️⃣", "ĐƠN LỆNH (tối đa 1 lệnh)",
              trades.get("single_mode", {}).get("closed_trades", []), [s_curr] if s_curr else []),
        block("2️⃣", "ĐA LỆNH (độc lập từng tín hiệu)",
              trades.get("multi_mode", {}).get("closed_trades", []), trades.get("multi_mode", {}).get("open_trades", [])),
    ]
    msg = ("\n" + SEP + "\n").join(parts)
    cum = cumulative_line()
    if cum:
        msg += "\n" + SEP + "\n" + cum.rstrip()
    return msg


# ==============================================================================
# NHẬT KÝ LỆNH DÀI HẠN & THỐNG KÊ (append-only, KHÔNG reset theo ngày)
# ==============================================================================
JOURNAL_FIELDS = [
    "trade_id", "mode", "direction", "outcome",
    "signal_time_utc", "filled_time_utc", "closed_time_utc",
    "entry", "sl", "tp", "exit_price", "risk_usd", "pnl_usd", "r_multiple",
    "minutes_to_fill", "minutes_in_trade", "mfe_usd", "mae_usd",
    "atr", "atr_source", "atr_proxy", "adx", "rsi", "regime", "with_trend",
    "session", "hour_utc", "weekday",
]

SESSION_LABELS = {
    "ASIA": "Á (00-07 UTC)",
    "LONDON": "London (07-12)",
    "OVERLAP": "London+NY (12-16)",
    "NY": "New York (16-21)",
    "LATE": "Cuối ngày (21-24)",
}

NUMERIC_FIELDS = (
    "entry", "sl", "tp", "exit_price", "risk_usd", "pnl_usd", "r_multiple",
    "minutes_to_fill", "minutes_in_trade", "mfe_usd", "mae_usd", "atr", "atr_proxy", "adx", "rsi",
)


def _to_float(val, default=None):
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def journal_record(mode, trade, outcome):
    """Xếp 1 dòng vào hàng chờ ghi nhật ký. outcome: HIT_TP | HIT_SL | MISSED_TP | EXPIRED | CANCELLED."""
    mode_key = "single" if mode in ("single", "Đơn lệnh") else "multi"
    direction = trade.get("direction", "")
    entry, sl, tp = trade.get("entry", 0.0), trade.get("sl", 0.0), trade.get("tp", 0.0)
    risk = round(abs(entry - sl) * LOT_VALUE_PER_DOLLAR, 2)
    created = trade.get("created_ts") or utc_str_to_ts(trade.get("time"))
    filled = trade.get("filled_ts") or utc_str_to_ts(trade.get("filled_time"))
    closed = trade.get("closed_ts") or utc_str_to_ts(trade.get("closed_time"))
    was_filled = outcome in ("HIT_TP", "HIT_SL")

    exit_price = tp if outcome == "HIT_TP" else (sl if outcome == "HIT_SL" else "")
    pnl = trade.get("pnl", 0.0) if was_filled else 0.0
    r_mult = round(pnl / risk, 3) if (was_filled and risk) else 0.0
    to_fill = round((filled - created) / 60.0, 1) if (was_filled and filled and created) else ""
    in_trade = round((closed - filled) / 60.0, 1) if (was_filled and closed and filled) else ""

    dt = datetime.fromtimestamp(created, tz=timezone.utc) if created else None
    with_trend = trade.get("with_trend")

    _JOURNAL_BUFFER.append({
        "trade_id": f"{created}_{direction}",
        "mode": mode_key,
        "direction": direction,
        "outcome": outcome,
        "signal_time_utc": trade.get("time", ""),
        "filled_time_utc": trade.get("filled_time", "") if was_filled else "",
        "closed_time_utc": trade.get("closed_time", "") if was_filled else "",
        "entry": entry, "sl": sl, "tp": tp,
        "exit_price": exit_price, "risk_usd": risk, "pnl_usd": pnl, "r_multiple": r_mult,
        "minutes_to_fill": to_fill, "minutes_in_trade": in_trade,
        "mfe_usd": trade.get("mfe", "") if was_filled else "",
        "mae_usd": trade.get("mae", "") if was_filled else "",
        "atr": trade.get("atr", ""),
        "atr_source": trade.get("atr_source", "legacy"),
        "atr_proxy": trade.get("atr_proxy", ""),
        "adx": trade.get("adx", ""),
        "rsi": trade.get("rsi", ""),
        "regime": trade.get("regime", ""),
        "with_trend": "" if with_trend is None else int(bool(with_trend)),
        "session": trade.get("session") or (session_of(dt.hour) if dt else ""),
        "hour_utc": dt.hour if dt else "",
        "weekday": dt.weekday() if dt else "",
    })


def flush_journal():
    """Ghi các dòng đang chờ vào CSV (bỏ qua dòng trùng). Được gọi cùng lúc với save_trades."""
    if not _JOURNAL_BUFFER:
        return
    try:
        existing = set()
        new_file = (not os.path.exists(JOURNAL_FILE)) or os.path.getsize(JOURNAL_FILE) == 0
        if not new_file:
            with open(JOURNAL_FILE, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    existing.add((r.get("trade_id"), r.get("mode"), r.get("outcome")))
        written = 0
        with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new_file:
                w.writeheader()
            for row in _JOURNAL_BUFFER:
                key = (row["trade_id"], row["mode"], row["outcome"])
                if key in existing:
                    continue
                w.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})
                existing.add(key)
                written += 1
        print(f"Đã ghi {written} dòng vào {JOURNAL_FILE}")
        _JOURNAL_BUFFER.clear()
    except Exception as e:
        print(f"Lỗi khi ghi nhật ký lệnh: {e}")


def load_journal(path=None):
    path = path or JOURNAL_FILE
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for k in NUMERIC_FIELDS:
                    r[k] = _to_float(r.get(k))
                rows.append(r)
    except Exception as e:
        print(f"Lỗi khi đọc nhật ký lệnh: {e}")
    return rows


def wilson_interval(wins, n, z=1.96):
    """Khoảng tin cậy 95% cho tỉ lệ thắng (Wilson)."""
    if n <= 0:
        return 0.0, 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def summarize_rows(rows, cost=None):
    """Tính chỉ số hiệu quả từ các dòng nhật ký. R = lãi/lỗ chia cho rủi ro ban đầu (|entry - SL|)."""
    cost = ASSUMED_COST_USD if cost is None else cost
    cost_usd = cost * LOT_VALUE_PER_DOLLAR
    ended = len(rows)
    filled = [r for r in rows if r.get("outcome") in ("HIT_TP", "HIT_SL")]
    s = {"ended": ended, "filled": len(filled), "fill_rate": (len(filled) / ended) if ended else 0.0}
    if not filled:
        return s

    def net_r(r):
        risk = r.get("risk_usd") or 0.0
        return ((r.get("pnl_usd") or 0.0) - cost_usd) / risk if risk else 0.0

    wins = [r for r in filled if r["outcome"] == "HIT_TP"]
    losses = [r for r in filled if r["outcome"] == "HIT_SL"]
    n = len(filled)
    nets = [net_r(r) for r in filled]
    pos = sum(x for x in nets if x > 0)
    neg = -sum(x for x in nets if x < 0)

    ordered = sorted(filled, key=lambda r: r.get("closed_time_utc") or "")
    cum = peak = max_dd = 0.0
    streak = max_streak = 0
    for r in ordered:
        cum += net_r(r)
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        if r["outcome"] == "HIT_SL":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    avg_win = _mean([net_r(r) for r in wins])
    avg_loss = _mean([net_r(r) for r in losses])
    breakeven = (1.0 / (1.0 + avg_win / abs(avg_loss))) if (wins and losses and avg_loss != 0) else None

    sl_with_mfe = [r for r in losses if r.get("mfe_usd") is not None and r.get("risk_usd")]
    reached_1r = [r for r in sl_with_mfe if r["mfe_usd"] >= r["risk_usd"]]
    lo, hi = wilson_interval(len(wins), n)
    minutes = [r["minutes_in_trade"] for r in filled if r.get("minutes_in_trade") is not None]

    s.update({
        "wins": len(wins), "losses": len(losses),
        "winrate": len(wins) / n, "ci_low": lo, "ci_high": hi,
        "avg_r_gross": _mean([r.get("r_multiple") or 0.0 for r in filled]),
        "avg_r_net": _mean(nets),
        "profit_factor": (pos / neg) if neg > 0 else None,
        "avg_win_r": avg_win, "avg_loss_r": avg_loss, "breakeven_wr": breakeven,
        "total_r_net": sum(nets), "total_pnl_usd": sum((r.get("pnl_usd") or 0.0) for r in filled),
        "max_dd_r": max_dd, "max_loss_streak": max_streak,
        "avg_minutes": _mean(minutes) if minutes else None,
        "sl_mfe_known": len(sl_with_mfe), "sl_reached_1r": len(reached_1r),
    })
    return s


def summarize_groups(rows, key_fn, cost=None):
    groups = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    return {k: summarize_rows(v, cost) for k, v in groups.items()}


def _fmt_group_line(label, s):
    if not s.get("filled"):
        return None
    flag = " (mẫu nhỏ)" if s["filled"] < 10 else ""
    return f"  - {label}: {s['filled']} lệnh, winrate {s['winrate'] * 100:.0f}%, kỳ vọng {s['avg_r_net']:+.2f}R{flag}"


def _format_mode_stats(title, rows):
    s = summarize_rows(rows)
    lines = [f"*{title}*"]
    if not s["ended"]:
        lines.append("  Chưa có lệnh nào kết thúc.")
        return lines
    unfilled = s["ended"] - s["filled"]
    lines.append(f"- Lệnh kết thúc: {s['ended']} (khớp {s['filled']} = {s['fill_rate'] * 100:.0f}%, hủy/hết hạn {unfilled})")
    if not s["filled"]:
        return lines

    pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "n/a"
    lines.append(f"- Thắng/Thua: {s['wins']}/{s['losses']} - Winrate {s['winrate'] * 100:.1f}% (KTC 95%: {s['ci_low'] * 100:.0f}-{s['ci_high'] * 100:.0f}%)")
    if s["breakeven_wr"] is not None:
        lines.append(f"- Winrate hòa vốn (theo R:R thực tế): {s['breakeven_wr'] * 100:.1f}%")
    lines.append(f"- Kỳ vọng mỗi lệnh: gộp {s['avg_r_gross']:+.2f}R | sau chi phí {s['avg_r_net']:+.2f}R")
    lines.append(f"- Profit factor (sau chi phí): {pf}")
    lines.append(f"- Tổng: {s['total_pnl_usd']:+.2f}$ gộp | {s['total_r_net']:+.2f}R ròng - Sụt giảm tối đa: {s['max_dd_r']:.2f}R - Chuỗi thua dài nhất: {s['max_loss_streak']}")
    if s["avg_minutes"] is not None:
        lines.append(f"- Thời gian giữ lệnh trung bình: {s['avg_minutes']:.0f} phút")
    if s["sl_mfe_known"]:
        lines.append(f"- Lệnh thua SL từng chạy được >= 1R: {s['sl_reached_1r']}/{s['sl_mfe_known']}")
    if s["filled"] < MIN_SAMPLE:
        lines.append(f"⚠️ Mới {s['filled']} lệnh khớp (< {MIN_SAMPLE}): chưa đủ để kết luận, nên chờ vài trăm lệnh.")

    by_session = summarize_groups(rows, lambda r: r.get("session") or "?")
    sess_lines = [_fmt_group_line(SESSION_LABELS.get(k, k), v) for k, v in by_session.items()]
    sess_lines = [x for x in sess_lines if x]
    if sess_lines:
        lines.append("- Theo phiên (giờ UTC):")
        lines.extend(sess_lines)

    trend_label = {"1": "Thuận xu hướng EMA", "0": "Ngược xu hướng EMA"}
    by_trend = summarize_groups(rows, lambda r: str(r.get("with_trend") or "?"))
    trend_lines = [_fmt_group_line(trend_label.get(k, "Không rõ (lệnh cũ)"), v) for k, v in by_trend.items()]
    trend_lines = [x for x in trend_lines if x]
    if trend_lines:
        lines.append("- Theo xu hướng:")
        lines.extend(trend_lines)

    by_dir = summarize_groups(rows, lambda r: r.get("direction") or "?")
    dir_lines = [_fmt_group_line(k, v) for k, v in by_dir.items()]
    dir_lines = [x for x in dir_lines if x]
    if dir_lines:
        lines.append("- Theo hướng:")
        lines.extend(dir_lines)
    return lines


def generate_stats_message(plain=False):
    """Báo cáo thống kê lũy kế từ nhật ký lệnh (lệnh /stats hoặc --stats)."""
    rows = load_journal()
    try:
        counters = load_trades().get("counters", {})
    except Exception:
        counters = {}

    lines = ["📈 *THỐNG KÊ NHẬT KÝ LỆNH XAU/USD* (lũy kế, không reset theo ngày)"]
    if rows:
        first = min((r.get("signal_time_utc") or "9999") for r in rows)
        last = max((r.get("closed_time_utc") or r.get("signal_time_utc") or "") for r in rows)
        lines.append(f"Từ {format_vn_dt(first)} đến {format_vn_dt(last)} (giờ VN)")
    lines.append(f"Chi phí giả định: {ASSUMED_COST_USD:.2f}$/lệnh (biến ASSUMED_COST_USD), 1R = rủi ro ban đầu |Entry - SL|")
    if counters.get("hours_scanned"):
        lines.append(
            f"Số giờ quét: {counters.get('hours_scanned', 0)} · Tín hiệu: {counters.get('signals', 0)}\n"
            f"Bỏ vì sideway: {counters.get('choppy_skips', 0)} · Bỏ vì ngược xu hướng: {counters.get('counter_trend_skips', 0)}\n"
            f"Đa lệnh - bỏ vì đang giữ lệnh ngược chiều: {counters.get('hedge_skipped', 0)} · "
            f"lệnh chờ bị thay thế: {counters.get('superseded', 0)} · bỏ vì trùng vùng: {counters.get('gap_skipped', 0)}"
        )
    lines.append("")

    if not rows:
        lines.append("Chưa có lệnh nào kết thúc trong nhật ký. Thống kê sẽ xuất hiện sau khi có lệnh chạm TP/SL, bị hủy hoặc hết hạn.")
    else:
        lines.extend(_format_mode_stats("1) ĐƠN LỆNH", [r for r in rows if r.get("mode") == "single"]))
        lines.append("")
        lines.extend(_format_mode_stats("2) ĐA LỆNH", [r for r in rows if r.get("mode") == "multi"]))
        lines.append("")
        lines.append("Lưu ý: Đơn lệnh và Đa lệnh dùng chung tín hiệu nên KHÔNG cộng gộp. MFE/giờ khớp/giờ chốt là ước lượng từ dữ liệu quét (có điểm mù).")

    msg = "\n".join(lines)
    if len(msg) > 3900:
        msg = msg[:3900] + "\n..."
    if plain:
        msg = msg.replace("*", "").replace("`", "")
    return msg


def cumulative_line():
    """Dòng tóm tắt lũy kế gắn vào báo cáo tổng kết ngày."""
    try:
        rows = load_journal()
        parts = []
        for mode_key, label in (("single", "Đơn lệnh"), ("multi", "Đa lệnh")):
            s = summarize_rows([r for r in rows if r.get("mode") == mode_key])
            if s.get("filled"):
                parts.append(f"{label}: {s['filled']} lệnh khớp, winrate {s['winrate'] * 100:.1f}%, kỳ vọng {s['avg_r_net']:+.2f}R")
        if not parts:
            return ""
        return "📚 *Lũy kế toàn nhật ký (sau chi phí giả định):* " + " | ".join(parts) + " (gõ /stats để xem chi tiết)\n"
    except Exception:
        return ""


# ==============================================================================
# TELEGRAM
# ==============================================================================
def check_telegram_command_requests():
    """
    Đọc tối đa 20 tin nhắn mới nhất. Trả về (có_lệnh, max_update_id).
    LƯU Ý: KHÔNG xác nhận (ack) ở đây. Hàm ack_telegram_updates() được gọi SAU khi đã xử lý xong,
    để nếu lấy dữ liệu lỗi thì lệnh không bị mất.
    """
    _TG_COMMANDS.clear()
    if not TELEGRAM_BOT_TOKEN:
        return False, 0

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "offset": -20,
        "timeout": 2,
        "allowed_updates": json.dumps(["message", "channel_post", "edited_message"]),
    }

    try:
        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code == 409:
            print("Phát hiện Webhook cũ đang chặn getUpdates, đang tự động xóa Webhook...")
            requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=5)
            resp = requests.get(url, params=params, timeout=10)

        if resp.status_code != 200:
            print(f"Telegram getUpdates lỗi [{resp.status_code}]: {resp.text}")
            return False, 0

        updates = resp.json().get("result", [])
        print(f"Số tin nhắn mới trong hàng đợi Telegram: {len(updates)}")
        if not updates:
            return False, 0

        found_command = False
        max_id = 0
        for u in updates:
            uid = u.get("update_id", 0)
            if uid > max_id:
                max_id = uid
            msg_obj = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
            text = str(msg_obj.get("text", "")).strip().lower()
            print(f"  -> Đọc được tin nhắn [{uid}]: '{text}'")
            if any(cmd in text for cmd in ["/stats", "/thongke"]):
                _TG_COMMANDS.add("stats")
            elif any(cmd in text for cmd in ["/check", "/status", "/lenh", "check"]):
                found_command = True

        return found_command, max_id
    except Exception as e:
        print(f"Lỗi khi kiểm tra lệnh Telegram: {e}")
        return False, 0


def ack_telegram_updates(max_id):
    """Xác nhận với Telegram là đã đọc hết đến max_id."""
    if not TELEGRAM_BOT_TOKEN or not max_id:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        requests.get(url, params={"offset": max_id + 1, "timeout": 0}, timeout=5)
    except Exception:
        pass


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram chưa cấu hình Token/Chat ID. Nội dung tin nhắn:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chat_id = TELEGRAM_CHAT_ID

    def post(cid, markdown):
        payload = {"chat_id": cid, "text": text}
        if markdown:
            payload["parse_mode"] = "Markdown"
        return requests.post(url, json=payload, timeout=10)

    try:
        resp = post(chat_id, True)
        print("Telegram Status (Markdown):", resp.status_code)

        # Nhóm được nâng cấp lên supergroup -> Telegram trả về chat id mới
        if resp.status_code == 400:
            try:
                new_id = resp.json().get("parameters", {}).get("migrate_to_chat_id")
            except Exception:
                new_id = None
            if new_id:
                print(f"⚠️ Nhóm đã nâng cấp lên supergroup. Hãy cập nhật secret TELEGRAM_CHAT_ID = {new_id}")
                chat_id = str(new_id)
                resp = post(chat_id, True)
                print("Telegram Status (chat id mới):", resp.status_code)

        if resp.status_code != 200:
            print(f"Telegram từ chối Markdown ({resp.text}). Đang gửi lại dạng Plain Text...")
            resp_fallback = post(chat_id, False)
            print("Telegram Fallback Status:", resp_fallback.status_code)
    except Exception as e:
        print(f"Lỗi kết nối Telegram: {e}")


# ==============================================================================
# LUỒNG CHẠY
# ==============================================================================
def run_poll(force_report=False):
    """
    Chạy MỖI PHÚT: cập nhật trạng thái lệnh bằng dữ liệu nến mới nhất,
    thông báo khi có sự kiện, và trả lời /check nếu có.
    """
    now_utc = datetime.now(timezone.utc)
    trades = load_trades()

    has_cmd, max_id = check_telegram_command_requests()
    if "stats" in _TG_COMMANDS:
        send_telegram(generate_stats_message())
    want_report = force_report or has_cmd
    weekend = is_weekend_market_closed(now_utc)

    if weekend and not want_report:
        print(f"Thị trường Vàng đang đóng cửa ({now_utc.strftime('%Y-%m-%d %H:%M UTC')}). Bỏ qua.")
        ack_telegram_updates(max_id)
        return

    try:
        candles = get_candles_with_retry()
        now_ts = int(time.time())
        now_str = ts_to_utc_str(now_ts)

        events = []
        if not weekend:
            events = evaluate_trades(trades, candles, now_ts)
            if events:
                save_trades(trades)
                print(f"Có {len(events)} sự kiện lệnh, đã lưu nhật ký.")

        if events and NOTIFY_EVENTS:
            send_telegram(format_event_message(events, candles["price"], now_str))

        if want_report:
            data = {"price": candles["price"], "time_str": now_str, "exchange": candles["exchange"]}
            send_telegram(generate_live_status_report(trades, data))
        elif not events:
            print(f"Không có thay đổi. Giá hiện tại: {candles['price']}")
    except Exception as e:
        err = f"⚠️ Gold Bot lỗi khi cập nhật lệnh: {e}"
        print(err)
        if want_report:
            send_telegram(err)
    finally:
        ack_telegram_updates(max_id)


def run_auto():
    """Chạy MỖI GIỜ: phân tích 1H/4H, cập nhật lệnh, tạo tín hiệu mới (1 lần/giờ), tổng kết ngày."""
    now_utc = datetime.now(timezone.utc)
    trades = load_trades()
    has_cmd, max_id = check_telegram_command_requests()
    if "stats" in _TG_COMMANDS:
        send_telegram(generate_stats_message())

    try:
        # Cuối tuần: chỉ trả lời /check nếu có
        if is_weekend_market_closed(now_utc):
            if has_cmd:
                candles = get_candles_with_retry()
                data = {
                    "price": candles["price"],
                    "time_str": ts_to_utc_str(int(time.time())),
                    "exchange": candles["exchange"],
                }
                send_telegram(generate_live_status_report(trades, data))
            else:
                print(f"Thị trường Vàng đang đóng cửa nghỉ cuối tuần ({now_utc.strftime('%Y-%m-%d %H:%M UTC')}). Bỏ qua gửi báo cáo.")
            return

        # 1. Quét khung 1H
        ex_1h, ind_1h, sum_1h = get_tv_analysis_with_retry(interval_str="1h")
        data_1h = extract_market_data(ex_1h, ind_1h, sum_1h)
        assessment_1h = generate_technical_assessment(data_1h)

        # 2. Cập nhật lệnh theo nến (dự phòng bằng nến 1H nếu không lấy được nến đa khung)
        try:
            candles = get_candles_with_retry()
        except Exception as e:
            print(f"Không lấy được nến đa khung, dùng nến 1H dự phòng: {e}")
            candles = {
                "exchange": ex_1h,
                "price": data_1h["price"],
                "tf": {60: (data_1h["high"], data_1h["low"])},
            }
        now_ts = int(time.time())
        events = evaluate_trades(trades, candles, now_ts)

        # 3. Tạo tín hiệu mới - chỉ 1 lần cho mỗi khung giờ (chạy tay nhiều lần không tạo lệnh trùng)
        slot_key = now_utc.strftime("%Y-%m-%d %H")
        is_new_slot = new_hour_step(trades, assessment_1h["trade_signal"], slot_key,
                                    skip_reason=assessment_1h.get("skip_reason"))
        save_trades(trades)

        if events and NOTIFY_EVENTS:
            send_telegram(format_event_message(events, candles["price"], ts_to_utc_str(now_ts)))

        send_telegram(format_telegram_message("1H", data_1h, assessment_1h, trades, new_slot=is_new_slot))

        if has_cmd:
            data_status = {
                "price": candles["price"],
                "time_str": ts_to_utc_str(now_ts),
                "exchange": candles["exchange"],
            }
            send_telegram(generate_live_status_report(trades, data_status))

        # 4. Khung 4H khi đóng nến (01, 05, 09, 13, 17, 21 UTC)
        if now_utc.hour in [1, 5, 9, 13, 17, 21]:
            try:
                ex_4h, ind_4h, sum_4h = get_tv_analysis_with_retry(interval_str="4h")
                data_4h = extract_market_data(ex_4h, ind_4h, sum_4h)
                assessment_4h = generate_technical_assessment(data_4h)
                send_telegram(format_telegram_message("4H", data_4h, assessment_4h))
            except Exception as e:
                print(f"Bỏ qua khung 4H do lỗi: {e}")

        # 5. Tổng kết ngày lúc 22:00 UTC (05:00 sáng VN) - chỉ 1 lần/ngày
        today_str = now_utc.strftime("%Y-%m-%d")
        if now_utc.hour == 22 and now_utc.weekday() in [0, 1, 2, 3, 4] and trades.get("last_summary_date") != today_str:
            send_telegram(generate_daily_summary(trades))

            new_trades = copy.deepcopy(trades)
            new_trades["date"] = today_str
            new_trades["last_summary_date"] = today_str
            new_trades["single_mode"]["closed_trades"] = []
            new_trades["multi_mode"]["closed_trades"] = []
            save_trades(new_trades)

    except Exception as e:
        error_msg = f"⚠️ Cảnh báo Gold Bot: không thể xử lý lúc này: {e}"
        print(error_msg)
        send_telegram(error_msg)
        sys.exit(1)
    finally:
        ack_telegram_updates(max_id)


def main():
    if "--stats" in sys.argv:
        print(generate_stats_message(plain=True))
        return

    mode = BOT_MODE.lower()
    if "--check" in sys.argv:
        mode = "check"
    if "--poll" in sys.argv:
        mode = "poll"

    if mode == "poll":
        run_poll(force_report=False)
    elif mode == "check":
        run_poll(force_report=True)
    else:
        run_auto()


if __name__ == "__main__":
    main()
