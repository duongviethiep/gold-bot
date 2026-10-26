import os
import sys
import json
import copy
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
        atr = safe_float(max((bb_upper - bb_lower) / 4.0, h - l, 8.0), default=12.0, ndigits=2)
    else:
        atr = safe_float(max(h - l, 12.0), default=12.0, ndigits=2)

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
    if price > ema20 and ema20 > ema50:
        trend = f"Tăng rõ rệt (Trên EMA20 & EMA50 - TradingView: {tv_signal})"
        bias = "BUY"
    elif price < ema20 and ema20 < ema50:
        trend = f"Giảm rõ rệt (Dưới EMA20 & EMA50 - TradingView: {tv_signal})"
        bias = "SELL"
    elif price > ema20:
        trend = f"Hồi phục ngắn hạn (TradingView: {tv_signal})"
        bias = "BUY"
    else:
        trend = f"Điều chỉnh ngắn hạn (TradingView: {tv_signal})"
        bias = "SELL"

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

    base_signal = {
        "status": "PENDING",
        "pending_hours": 0,
        "time": data["time_str"],
        "created_ts": data.get("ts"),
        "atr": atr,
    }

    if is_choppy:
        action = "ĐỨNG NGOÀI (Né sideway biên hẹp, không vội mở vị thế)"
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
    }


# ==============================================================================
# LƯU / TẢI NHẬT KÝ LỆNH
# ==============================================================================
def load_trades():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default_data = {
        "date": today_str,
        "last_signal_slot": None,
        "last_summary_date": None,
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
                return data
        except Exception:
            pass
    return default_data


def save_trades(trades):
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


def new_hour_step(trades, new_signal, slot_key):
    """
    Bước chạy MỖI GIỜ (chỉ 1 lần cho mỗi khung giờ nhờ slot_key):
    - Single: lệnh chờ hết hạn sau 1 giờ; nếu trống thì nhận tín hiệu mới.
    - Multi : tăng bộ đếm giờ chờ, hủy lệnh chờ quá hạn, thêm tín hiệu mới (nếu không bị trùng vùng).
    Trả về True nếu đây là lần đầu xử lý khung giờ này, False nếu đã xử lý rồi.
    """
    if trades.get("last_signal_slot") == slot_key:
        return False
    trades["last_signal_slot"] = slot_key

    single = trades["single_mode"]
    curr = single.get("current_trade")
    if isinstance(curr, dict) and curr.get("status") == "PENDING":
        single["current_trade"] = None
    if single.get("current_trade") is None and new_signal:
        single["current_trade"] = copy.deepcopy(new_signal)

    multi = trades["multi_mode"]
    kept = []
    for t in multi.get("open_trades", []):
        if t.get("status") == "PENDING":
            t["pending_hours"] = t.get("pending_hours", 0) + 1
            if t["pending_hours"] >= MAX_PENDING_HOURS:
                continue
        kept.append(t)
    multi["open_trades"] = kept

    if new_signal and not _too_close_to_existing(multi["open_trades"], new_signal):
        multi["open_trades"].append(copy.deepcopy(new_signal))

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


def format_closed_trade_item(idx, t):
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    pnl = t.get("pnl", 0.0)
    outcome = t.get("outcome", "")
    sig_time = format_vn_dt(t.get("time"))
    close_time = format_vn_dt(t.get("closed_time"))

    if pnl > 0 or outcome == "HIT_TP":
        res_str = f"✅ TP (+${abs(pnl):.2f})"
    else:
        res_str = f"❌ SL (-${abs(pnl):.2f})"

    close_note = f" - Chốt {close_time}" if close_time != "N/A" else ""
    return f"  {idx}. `{direction}` ({sig_time} VN) - Entry: `${entry}` - SL: `${sl}` - TP: `${tp}` -> *{res_str}*{close_note}"


def format_active_trade_item(idx, t, current_price):
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    sig_time = format_vn_dt(t.get("time"))
    fill_time = format_vn_dt(t.get("filled_time"))

    fpnl = calc_floating_pnl(t, current_price)
    fpnl_str = f"+${abs(fpnl):.2f}" if fpnl >= 0 else f"-${abs(fpnl):.2f}"
    icon = "🟢" if fpnl >= 0 else "🔴"
    fill_note = f" - Khớp {fill_time}" if fill_time != "N/A" else ""

    return (
        f"  {idx}. {icon} `{direction}` (Tín hiệu {sig_time}{fill_note}) - "
        f"Entry: `${entry}` - SL: `${sl}` - TP: `${tp}` -> *P&L tạm tính: `{fpnl_str}`*"
    )


def format_pending_trade_item(idx, t, current_price):
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    sig_time = format_vn_dt(t.get("time"))
    dist = round(abs(current_price - entry), 2)

    return (
        f"  {idx}. 🕒 `{direction} Limit` (Tín hiệu {sig_time} VN) - "
        f"Chờ Entry: `${entry}` (Cách giá hiện tại `${dist}`) - SL: `${sl}` - TP: `${tp}`"
    )


def generate_live_status_report(trades, data):
    """Báo cáo chi tiết toàn bộ tình trạng lệnh của cả chế độ Đơn lệnh và Đa lệnh."""
    current_price = data["price"]
    vn_now = format_vn_time(data["time_str"])

    # --- 1. ĐƠN LỆNH ---
    s_curr = trades.get("single_mode", {}).get("current_trade")
    s_closed = trades.get("single_mode", {}).get("closed_trades", [])

    if s_curr and s_curr.get("status") == "ACTIVE":
        s_active_str = format_active_trade_item(1, s_curr, current_price)
        s_floating = calc_floating_pnl(s_curr, current_price)
    else:
        s_active_str = "  - Không có lệnh nào đang gồng"
        s_floating = 0.0

    if s_curr and s_curr.get("status") == "PENDING":
        s_pending_str = format_pending_trade_item(1, s_curr, current_price)
    else:
        s_pending_str = "  - Không có lệnh đang chờ"

    if s_closed:
        s_closed_str = "\n".join(format_closed_trade_item(i + 1, t) for i, t in enumerate(s_closed))
    else:
        s_closed_str = "  - Chưa có lệnh nào chốt trong ngày"

    s_realized = round(sum(t.get("pnl", 0.0) for t in s_closed), 2)
    s_total_pnl = round(s_realized + s_floating, 2)
    s_wins = len([t for t in s_closed if t.get("pnl", 0.0) > 0])
    s_losses = len(s_closed) - s_wins
    s_real_sign = "+$" if s_realized >= 0 else "-$"
    s_float_sign = "+$" if s_floating >= 0 else "-$"
    s_tot_sign = "+$" if s_total_pnl >= 0 else "-$"

    # --- 2. ĐA LỆNH ---
    m_open_list = trades.get("multi_mode", {}).get("open_trades", [])
    m_active_list = [t for t in m_open_list if t.get("status") == "ACTIVE"]
    m_pending_list = [t for t in m_open_list if t.get("status") == "PENDING"]
    m_closed = trades.get("multi_mode", {}).get("closed_trades", [])

    if m_active_list:
        m_active_str = "\n".join(format_active_trade_item(i + 1, t, current_price) for i, t in enumerate(m_active_list))
        m_floating = round(sum(calc_floating_pnl(t, current_price) for t in m_active_list), 2)
    else:
        m_active_str = "  - Không có lệnh nào đang gồng"
        m_floating = 0.0

    if m_pending_list:
        m_pending_str = "\n".join(format_pending_trade_item(i + 1, t, current_price) for i, t in enumerate(m_pending_list))
    else:
        m_pending_str = "  - Không có lệnh đang chờ"

    if m_closed:
        m_closed_str = "\n".join(format_closed_trade_item(i + 1, t) for i, t in enumerate(m_closed))
    else:
        m_closed_str = "  - Chưa có lệnh nào chốt trong ngày"

    m_realized = round(sum(t.get("pnl", 0.0) for t in m_closed), 2)
    m_total_pnl = round(m_realized + m_floating, 2)
    m_wins = len([t for t in m_closed if t.get("pnl", 0.0) > 0])
    m_losses = len(m_closed) - m_wins
    m_real_sign = "+$" if m_realized >= 0 else "-$"
    m_float_sign = "+$" if m_floating >= 0 else "-$"
    m_tot_sign = "+$" if m_total_pnl >= 0 else "-$"

    msg = (
        f"🔎 *KIỂM TRA TRẠNG THÁI LỆNH XAU/USD REALTIME*\n"
        f"⏰ *Giờ kiểm tra:* `{vn_now}` VN - 💰 *Giá hiện tại:* `${current_price}` (`{data['exchange']}`)\n"
        f"📊 *Khối lượng chuẩn:* `0.01 lot` ($1 giá = $1.00)\n\n"
        f"1️⃣ *CHẾ ĐỘ ĐƠN LỆNH (Single Mode - Tối đa 1 lệnh)*\n"
        f"• *Lệnh ĐÃ VÀO đang gồng ({1 if s_curr and s_curr.get('status') == 'ACTIVE' else 0} lệnh):*\n"
        f"{s_active_str}\n"
        f"• *Lệnh ĐANG CHỜ khớp ({1 if s_curr and s_curr.get('status') == 'PENDING' else 0} lệnh):*\n"
        f"{s_pending_str}\n"
        f"• *Lệnh ĐÃ CHỐT ({len(s_closed)} lệnh - Thắng {s_wins} / Thua {s_losses}):*\n"
        f"{s_closed_str}\n"
        f"👉 *P&L Đơn lệnh:* Đã chốt `{s_real_sign}{abs(s_realized):.2f}` - Đang gồng `{s_float_sign}{abs(s_floating):.2f}` -> *Tổng: `{s_tot_sign}{abs(s_total_pnl):.2f}`*\n\n"
        f"2️⃣ *CHẾ ĐỘ ĐA LỆNH (Multi Mode - Độc lập từng tín hiệu)*\n"
        f"• *Lệnh ĐÃ VÀO đang gồng ({len(m_active_list)} lệnh):*\n"
        f"{m_active_str}\n"
        f"• *Lệnh ĐANG CHỜ khớp ({len(m_pending_list)} lệnh):*\n"
        f"{m_pending_str}\n"
        f"• *Lệnh ĐÃ CHỐT ({len(m_closed)} lệnh - Thắng {m_wins} / Thua {m_losses}):*\n"
        f"{m_closed_str}\n"
        f"👉 *P&L Đa lệnh:* Đã chốt `{m_real_sign}{abs(m_realized):.2f}` - Đang gồng `{m_float_sign}{abs(m_floating):.2f}` -> *Tổng: `{m_tot_sign}{abs(m_total_pnl):.2f}`*"
    )
    return msg


def format_event_message(events, price, now_str):
    """Thông báo khi lệnh khớp / chạm TP / chạm SL / bị hủy."""
    lines = [f"🔔 *CẬP NHẬT LỆNH XAU/USD* - `{format_vn_time(now_str)}` VN - Giá: `${price}`"]
    for ev in events:
        t = ev["trade"]
        mode = ev["mode"]
        base = f"`{t.get('direction')}` Entry `${t.get('entry')}` (SL `${t.get('sl')}` - TP `${t.get('tp')}`)"
        kind = ev["kind"]
        pnl = t.get("pnl", 0.0)
        if kind == "FILLED":
            lines.append(f"🟢 [{mode}] KHỚP lệnh {base}")
        elif kind == "HIT_TP":
            lines.append(f"✅ [{mode}] CHẠM TP (+${abs(pnl):.2f}): {base}")
        elif kind == "HIT_SL":
            lines.append(f"❌ [{mode}] CHẠM SL (-${abs(pnl):.2f}): {base}")
        elif kind == "MISSED_TP":
            lines.append(f"⚪ [{mode}] HỦY lệnh chờ (giá chạy tới TP khi chưa khớp): {base}")
        else:
            lines.append(f"⚪ [{mode}] Lệnh bị hủy: {base}")
    lines.append("Giờ khớp/chốt là thời điểm bot phát hiện (quét mỗi phút).")
    return "\n".join(lines)


def format_telegram_message(tf, data, assessment, trades=None, new_slot=True):
    vn_now = format_vn_time(data["time_str"])
    msg = (
        f"📊 *XAU/USD ({tf}) - TRADINGVIEW LIVE*\n"
        f"🏦 *Sàn:* `{data['exchange']}` - ⏰ *Giờ VN:* `{vn_now}` (`{data['time_str']}` UTC)\n"
        f"💰 *Giá hiện tại:* `{data['price']}`\n"
        f"📈 *Biên độ:* O: `{data['open']}` - H: `{data['high']}` - L: `{data['low']}`\n\n"
        f"• *Xu hướng:* {assessment['trend']}\n"
        f"• *Động lực:* {assessment['momentum']}\n"
        f"• *Vùng cản:* {assessment['key_sr']}\n"
        f"• *Kế hoạch:* {assessment['action']}\n"
        f"*(ATR: ${data['atr']} - Chuẩn hóa: 0.01 lot = $1/giá)*"
    )

    if tf == "1H" and trades:
        curr = trades.get("single_mode", {}).get("current_trade")
        if curr:
            if curr.get("status") == "ACTIVE":
                fpnl = calc_floating_pnl(curr, data["price"])
                fpnl_str = f"+${abs(fpnl):.2f}" if fpnl >= 0 else f"-${abs(fpnl):.2f}"
                st_label = f"ĐANG GỒNG (P&L: {fpnl_str})"
            else:
                st_label = "ĐANG CHỜ KHỚP (Pending Limit)"
            msg += f"\n📌 *Trạng thái lệnh đơn:* {st_label} - `{curr['direction']} @ ${curr['entry']}` (SL: `${curr['sl']}` - TP: `${curr['tp']}`)"
        else:
            msg += "\n📌 *Trạng thái lệnh đơn:* Không có lệnh mở"
        if not new_slot:
            msg += "\nℹ️ Khung giờ này đã có tín hiệu, lần chạy này không tạo lệnh mới."
    return msg


def format_open_trade_item(t):
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    status = t.get("status", "PENDING")
    sig_time = format_vn_dt(t.get("time"))

    if status == "ACTIVE":
        return f"  - ⏳ *Đang gồng:* `{direction} @ ${entry}` (SL: `${sl}` - TP: `${tp}` - Tín hiệu {sig_time} VN)"
    return f"  - 🕒 *Chờ khớp:* `{direction} Limit @ ${entry}` (SL: `${sl}` - TP: `${tp}` - Tín hiệu {sig_time} VN)"


def generate_daily_summary(trades):
    s_closed = trades.get("single_mode", {}).get("closed_trades", [])
    m_closed = trades.get("multi_mode", {}).get("closed_trades", [])

    s_pnl = sum(t.get("pnl", 0.0) for t in s_closed)
    s_wins = len([t for t in s_closed if t.get("pnl", 0.0) > 0])
    s_losses = len(s_closed) - s_wins
    s_winrate = (s_wins / len(s_closed) * 100) if s_closed else 0.0
    s_sign = "+$" if s_pnl >= 0 else "-$"

    if s_closed:
        s_closed_lines = "\n".join(format_closed_trade_item(i + 1, t) for i, t in enumerate(s_closed))
    else:
        s_closed_lines = "  - Chưa có lệnh nào chốt trong phiên"

    s_curr = trades.get("single_mode", {}).get("current_trade")
    s_open_lines = format_open_trade_item(s_curr) if s_curr else "  - Không có lệnh mở"

    m_pnl = sum(t.get("pnl", 0.0) for t in m_closed)
    m_wins = len([t for t in m_closed if t.get("pnl", 0.0) > 0])
    m_losses = len(m_closed) - m_wins
    m_winrate = (m_wins / len(m_closed) * 100) if m_closed else 0.0
    m_sign = "+$" if m_pnl >= 0 else "-$"

    if m_closed:
        m_closed_lines = "\n".join(format_closed_trade_item(i + 1, t) for i, t in enumerate(m_closed))
    else:
        m_closed_lines = "  - Chưa có lệnh nào chốt trong phiên"

    m_open_list = trades.get("multi_mode", {}).get("open_trades", [])
    if m_open_list:
        m_open_lines = "\n".join(format_open_trade_item(t) for t in m_open_list)
    else:
        m_open_lines = "  - Không có lệnh mở"

    msg = (
        f"📋 *TỔNG KẾT CHI TIẾT PHIÊN GIAO DỊCH VÀNG (05:00 VN)*\n"
        f"📅 Ngày: `{trades.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))}` - Khối lượng: `0.01 lot` ($1 giá = $1.00)\n"
        f"✅ *Quy tắc:* Chỉ tính lệnh đã chạm Entry khớp thực tế\n\n"
        f"1️⃣ *CHẾ ĐỘ ĐƠN LỆNH (Single Mode - Tối đa 1 lệnh)*\n"
        f"• *Chi tiết lệnh đã chốt ({len(s_closed)} lệnh):*\n"
        f"{s_closed_lines}\n"
        f"• *Vị thế hiện tại:*\n"
        f"{s_open_lines}\n"
        f"• *Tổng kết Đơn lệnh:* Thắng `{s_wins}` - Thua `{s_losses}` (Winrate: `{s_winrate:.1f}%`) -> *P&L: `{s_sign}{abs(s_pnl):.2f}`*\n\n"
        f"2️⃣ *CHẾ ĐỘ ĐA LỆNH (Multi Mode - Độc lập từng tín hiệu)*\n"
        f"• *Chi tiết lệnh đã chốt ({len(m_closed)} lệnh):*\n"
        f"{m_closed_lines}\n"
        f"• *Vị thế hiện tại ({len(m_open_list)} lệnh):*\n"
        f"{m_open_lines}\n"
        f"• *Tổng kết Đa lệnh:* Thắng `{m_wins}` - Thua `{m_losses}` (Winrate: `{m_winrate:.1f}%`) -> *P&L: `{m_sign}{abs(m_pnl):.2f}`*\n"
    )
    return msg


# ==============================================================================
# TELEGRAM
# ==============================================================================
def check_telegram_command_requests():
    """
    Đọc tối đa 20 tin nhắn mới nhất. Trả về (có_lệnh, max_update_id).
    LƯU Ý: KHÔNG xác nhận (ack) ở đây. Hàm ack_telegram_updates() được gọi SAU khi đã xử lý xong,
    để nếu lấy dữ liệu lỗi thì lệnh không bị mất.
    """
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
            if any(cmd in text for cmd in ["/check", "/status", "/lenh", "check"]):
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
        is_new_slot = new_hour_step(trades, assessment_1h["trade_signal"], slot_key)
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
        error_msg = f"⚠️ *Cảnh báo Gold Bot:* Không thể xử lý lúc này: `{e}`"
        print(error_msg)
        send_telegram(error_msg)
        sys.exit(1)
    finally:
        ack_telegram_updates(max_id)


def main():
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
