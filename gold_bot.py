import os
import sys
import json
import copy
import time
import requests
from datetime import datetime, timezone, timedelta

# Cấu hình môi trường (Chỉ cần Telegram, lấy giá trực tiếp từ TradingView)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TV_SYMBOL = os.getenv("TV_SYMBOL", "XAUUSD")
TV_EXCHANGE = os.getenv("TV_EXCHANGE", "PEPPERSTONE")
TV_SCREENER = os.getenv("TV_SCREENER", "cfd")
BOT_MODE = os.getenv("BOT_MODE", "auto")  # "auto" (định kỳ), "check" (báo cáo lệnh ngay), "poll" (quét lệnh Telegram)

LOT_SIZE = 0.01
LOT_VALUE_PER_DOLLAR = 1.0  # 0.01 lot XAU/USD: $1.00 biên độ = $1.00 P&L
MAX_PENDING_HOURS = 4       # Lệnh Limit ở Multi Mode chờ quá 4 tiếng không khớp sẽ tự hủy
TRADES_FILE = "daily_trades.json"

def safe_float(val, default=0.0, ndigits=2):
    """Ép kiểu an toàn, chống lỗi NoneType khi TradingView trả về null."""
    if val is None:
        return default
    try:
        return round(float(val), ndigits)
    except (ValueError, TypeError):
        return default

def format_vn_time(utc_str):
    """Chuyển chuỗi giờ UTC (VD: '2026-10-26 04:01:00' hoặc ISO) sang giờ Việt Nam (UTC+7) '11:01'."""
    if not utc_str:
        return "N/A"
    try:
        clean_str = str(utc_str).replace("Z", "").replace("T", " ").split(".")[0]
        dt = datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
        dt_vn = dt + timedelta(hours=7)
        return dt_vn.strftime("%H:%M")
    except Exception:
        return str(utc_str)

def is_weekend_market_closed(now_utc):
    """
    Kiểm tra giờ đóng cửa cuối tuần của thị trường Vàng (XAU/USD) theo giờ UTC:
    - Thứ Sáu (weekday == 4): Sau 22:00 UTC (tức từ 23:00 UTC = 06:00 sáng Thứ Bảy VN) sàn nghỉ.
    - Thứ Bảy (weekday == 5): Nghỉ toàn bộ cả ngày.
    - Chủ Nhật (weekday == 6): Nghỉ từ 00:00 đến 21:59 UTC (trước 05:00 sáng Thứ Hai VN).
    """
    wd = now_utc.weekday()  # 0=Mon, ..., 4=Fri, 5=Sat, 6=Sun
    hr = now_utc.hour
    if wd == 4 and hr >= 23:
        return True
    if wd == 5:
        return True
    if wd == 6 and hr < 22:
        return True
    return False

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
        "Recommend.All",
        "RSI",
        "ADX",
        "EMA10",
        "EMA20",
        "EMA50",
        "open",
        "high",
        "low",
        "close",
        "Pivot.M.Classic.S1",
        "Pivot.M.Classic.R1",
        "BB.upper",
        "BB.lower"
    ]
    cols = [f"{c}{suffix}" for c in base_cols]
    url = f"https://scanner.tradingview.com/{screener}/scan"
    payload = {
        "symbols": {
            "tickers": [f"{exchange}:{symbol}"],
            "query": {"types": []}
        },
        "columns": cols
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json"
    }
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

def get_tv_analysis_with_retry(symbol=TV_SYMBOL, exchange=TV_EXCHANGE, screener=TV_SCREENER, interval_str="1h", max_retries=3):
    """
    Lấy dữ liệu kỹ thuật và giá trực tiếp từ TradingView.
    Tự động thử lại 3 lần và dự phòng các sàn lớn (PEPPERSTONE -> OANDA -> TVC -> FOREXCOM -> FX_IDC).
    """
    exchanges_to_try = [exchange]
    for fallback in ["PEPPERSTONE", "OANDA", "TVC", "FOREXCOM", "FX_IDC"]:
        if fallback not in exchanges_to_try:
            exchanges_to_try.append(fallback)
            
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
        "time_str": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    }

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
    
    if is_choppy:
        action = "ĐỨNG NGOÀI (Né sideway biên hẹp, không vội mở vị thế)"
        trade_signal = None
    elif bias == "BUY":
        entry = round(max(sup, price - round(0.4 * atr, 2)), 2)
        sl = round(entry - sl_dist, 2)
        tp = round(entry + tp_dist, 2)
        action = f"Canh Buy Limit quanh ${entry} (Không mua đuổi) - SL: ${sl} (-${sl_dist}) - TP: ${tp} (+${tp_dist})"
        trade_signal = {
            "direction": "BUY",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "status": "PENDING",
            "pending_hours": 0,
            "time": data["time_str"]
        }
    else:
        entry = round(min(res, price + round(0.4 * atr, 2)), 2)
        sl = round(entry + sl_dist, 2)
        tp = round(entry - tp_dist, 2)
        action = f"Canh Sell Limit quanh ${entry} (Không bán đuổi) - SL: ${sl} (-${sl_dist}) - TP: ${tp} (+${tp_dist})"
        trade_signal = {
            "direction": "SELL",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "status": "PENDING",
            "pending_hours": 0,
            "time": data["time_str"]
        }
        
    return {
        "trend": trend,
        "momentum": momentum,
        "key_sr": key_sr,
        "action": action,
        "trade_signal": trade_signal
    }

def load_trades():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default_data = {
        "date": today_str,
        "single_mode": {"current_trade": None, "closed_trades": []},
        "multi_mode": {"open_trades": [], "closed_trades": []}
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

def process_single_order(trade, high, low, current_time_str=None, is_hourly_step=True):
    """Xử lý vòng đời chuẩn xác của 1 lệnh Limit qua biên độ (high, low)."""
    direction = trade["direction"]
    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    status = trade.get("status", "PENDING")

    if status == "PENDING":
        if direction == "BUY":
            if low <= entry:
                trade["status"] = "ACTIVE"
                trade["filled_time"] = current_time_str
                if low <= sl:
                    pnl = round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2)
                    trade["closed_time"] = current_time_str
                    return "CLOSED", pnl, "HIT_SL"
                return "ACTIVE", 0.0, "FILLED"
            elif high >= tp:
                return "CANCELLED", 0.0, "MISSED_TP"
        elif direction == "SELL":
            if high >= entry:
                trade["status"] = "ACTIVE"
                trade["filled_time"] = current_time_str
                if high >= sl:
                    pnl = round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2)
                    trade["closed_time"] = current_time_str
                    return "CLOSED", pnl, "HIT_SL"
                return "ACTIVE", 0.0, "FILLED"
            elif low <= tp:
                return "CANCELLED", 0.0, "MISSED_TP"
                
        if is_hourly_step:
            trade["pending_hours"] = trade.get("pending_hours", 0) + 1
        return "PENDING", 0.0, ""

    elif status == "ACTIVE":
        if direction == "BUY":
            if low <= sl:
                pnl = round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2)
                trade["closed_time"] = current_time_str
                return "CLOSED", pnl, "HIT_SL"
            elif high >= tp:
                pnl = round((tp - entry) * LOT_VALUE_PER_DOLLAR, 2)
                trade["closed_time"] = current_time_str
                return "CLOSED", pnl, "HIT_TP"
        elif direction == "SELL":
            if high >= sl:
                pnl = round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2)
                trade["closed_time"] = current_time_str
                return "CLOSED", pnl, "HIT_SL"
            elif low <= tp:
                pnl = round((entry - tp) * LOT_VALUE_PER_DOLLAR, 2)
                trade["closed_time"] = current_time_str
                return "CLOSED", pnl, "HIT_TP"
        return "ACTIVE", 0.0, ""

    return "CANCELLED", 0.0, ""

def evaluate_and_update_trades(trades, current_market, new_signal, is_hourly_step=True):
    high, low = current_market["high"], current_market["low"]
    now_str = current_market.get("time_str")
    
    # 1. Chế độ Đơn lệnh (Single Mode - Tối đa 1 vị thế)
    curr = trades.get("single_mode", {}).get("current_trade")
    if isinstance(curr, dict) and "direction" in curr and "entry" in curr and "sl" in curr and "tp" in curr:
        new_status, pnl, outcome = process_single_order(curr, high, low, now_str, is_hourly_step=is_hourly_step)
        if new_status == "CLOSED":
            curr["status"] = "CLOSED"
            curr["outcome"] = outcome
            curr["pnl"] = pnl
            trades["single_mode"]["closed_trades"].append(curr)
            trades["single_mode"]["current_trade"] = None
        elif new_status == "CANCELLED":
            trades["single_mode"]["current_trade"] = None
        elif new_status == "PENDING":
            if is_hourly_step:
                trades["single_mode"]["current_trade"] = None
            else:
                trades["single_mode"]["current_trade"] = curr
        elif new_status == "ACTIVE":
            trades["single_mode"]["current_trade"] = curr
    elif curr is not None:
        trades["single_mode"]["current_trade"] = None

    if is_hourly_step and trades["single_mode"]["current_trade"] is None and new_signal:
        trades["single_mode"]["current_trade"] = copy.deepcopy(new_signal)
        
    # 2. Chế độ Đa lệnh (Multi Mode - Độc lập từng tín hiệu)
    remaining_open = []
    for t in trades.get("multi_mode", {}).get("open_trades", []):
        if not (isinstance(t, dict) and "direction" in t and "entry" in t and "sl" in t and "tp" in t):
            continue
        new_status, pnl, outcome = process_single_order(t, high, low, now_str, is_hourly_step=is_hourly_step)
        if new_status == "CLOSED":
            t["status"] = "CLOSED"
            t["outcome"] = outcome
            t["pnl"] = pnl
            trades["multi_mode"]["closed_trades"].append(t)
        elif new_status == "ACTIVE":
            remaining_open.append(t)
        elif new_status == "PENDING":
            if t.get("pending_hours", 0) < MAX_PENDING_HOURS:
                remaining_open.append(t)
                
    trades["multi_mode"]["open_trades"] = remaining_open
    if is_hourly_step and new_signal:
        trades["multi_mode"]["open_trades"].append(copy.deepcopy(new_signal))

def calc_floating_pnl(trade, current_price):
    """Tính P&L tạm tính (Floating P&L) cho lệnh đã khớp (ACTIVE) theo giá hiện tại."""
    if not trade or trade.get("status") != "ACTIVE":
        return 0.0
    entry = trade.get("entry", current_price)
    if trade.get("direction") == "BUY":
        return round((current_price - entry) * LOT_VALUE_PER_DOLLAR, 2)
    else:
        return round((entry - current_price) * LOT_VALUE_PER_DOLLAR, 2)

def format_closed_trade_item(idx, t):
    """Định dạng chi tiết 1 lệnh đã chốt."""
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    pnl = t.get("pnl", 0.0)
    outcome = t.get("outcome", "")
    sig_time = format_vn_time(t.get("time"))
    close_time = format_vn_time(t.get("closed_time"))
    
    if pnl > 0 or outcome == "HIT_TP":
        res_str = f"✅ TP (+${abs(pnl):.2f})"
    else:
        res_str = f"❌ SL (-${abs(pnl):.2f})"
        
    close_note = f" - Chốt {close_time}" if close_time != "N/A" else ""
    return f"  {idx}. `{direction}` ({sig_time} VN) - Entry: `${entry}` - SL: `${sl}` - TP: `${tp}` -> *{res_str}*{close_note}"

def format_active_trade_item(idx, t, current_price):
    """Định dạng chi tiết 1 lệnh ĐÃ VÀO (ACTIVE - Đang gồng) kèm P&L tạm tính."""
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    sig_time = format_vn_time(t.get("time"))
    fill_time = format_vn_time(t.get("filled_time"))
    fpnl = calc_floating_pnl(t, current_price)
    fpnl_str = f"+${abs(fpnl):.2f}" if fpnl >= 0 else f"-${abs(fpnl):.2f}"
    icon = "🟢" if fpnl >= 0 else "🔴"
    fill_note = f" - Khớp {fill_time}" if fill_time != "N/A" else ""
    return (
        f"  {idx}. {icon} `{direction}` (Tín hiệu {sig_time}{fill_note}) - "
        f"Entry: `${entry}` - SL: `${sl}` - TP: `${tp}` -> *P&L tạm tính: `{fpnl_str}`*"
    )

def format_pending_trade_item(idx, t, current_price):
    """Định dạng chi tiết 1 lệnh ĐANG CHỜ (PENDING Limit) kèm khoảng cách tới Entry."""
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    sig_time = format_vn_time(t.get("time"))
    dist = round(abs(current_price - entry), 2)
    return (
        f"  {idx}. 🕒 `{direction} Limit` (Tín hiệu {sig_time} VN) - "
        f"Chờ Entry: `${entry}` (Cách giá hiện tại `${dist}`) - SL: `${sl}` - TP: `${tp}`"
    )

def generate_live_status_report(trades, data):
    """
    Báo cáo chi tiết toàn bộ tình trạng lệnh (Đã vào đang gồng, Đang chờ khớp, Đã chốt)
    và TP - SL - P&L của cả chế độ Đơn lệnh và Đa lệnh.
    """
    current_price = data["price"]
    vn_now = format_vn_time(data["time_str"])
    
    # --- 1. CHẾ ĐỘ ĐƠN LỆNH (SINGLE MODE) ---
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

    # --- 2. CHẾ ĐỘ ĐA LỆNH (MULTI MODE) ---
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

def format_telegram_message(tf, data, assessment, trades=None):
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
    return msg

def format_open_trade_item(t):
    direction = t.get("direction", "N/A")
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp = t.get("tp", 0.0)
    status = t.get("status", "PENDING")
    sig_time = format_vn_time(t.get("time"))
    
    if status == "ACTIVE":
        return f"  - ⏳ *Đang gồng:* `{direction} @ ${entry}` (SL: `${sl}` - TP: `${tp}` - Tín hiệu {sig_time} VN)"
    else:
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

def check_telegram_command_requests():
    """
    Đọc hàng đợi 20 tin nhắn mới nhất trên máy chủ Telegram và xác nhận đã đọc (acknowledge offset)
    trực tiếp trên máy chủ Telegram để tránh xung đột git push mỗi phút.
    """
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "offset": -20,
        "timeout": 2,
        "allowed_updates": json.dumps(["message", "channel_post", "edited_message"])
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 409:
            print("Phát hiện Webhook cũ đang chặn getUpdates, đang tự động xóa Webhook...")
            requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=5)
            resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram getUpdates lỗi [{resp.status_code}]: {resp.text}")
            return False
        updates = resp.json().get("result", [])
        print(f"Số tin nhắn mới trong hàng đợi Telegram: {len(updates)}")
        if not updates:
            return False
        found_command = False
        max_id = 0
        for u in updates:
            uid = u.get("update_id", 0)
            if uid > max_id:
                max_id = uid
            msg_obj = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
            text = str(msg_obj.get("text", "")).strip().lower()
            print(f" -> Đọc được tin nhắn [{uid}]: '{text}'")
            if any(cmd in text for cmd in ["/check", "/status", "/lenh", "check"]):
                found_command = True
        if max_id > 0:
            # Xác nhận ngay với máy chủ Telegram là đã đọc hết đến max_id (Telegram tự xóa các tin <= max_id)
            try:
                requests.get(url, params={"offset": max_id + 1, "timeout": 1}, timeout=5)
            except Exception:
                pass
        return found_command
    except Exception as e:
        print(f"Lỗi khi kiểm tra lệnh Telegram: {e}")
        return False

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram chưa cấu hình Token/Chat ID. Nội dung tin nhắn:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        print("Telegram Status (Markdown):", resp.status_code)
        if resp.status_code != 200:
            print(f"Telegram từ chối Markdown ({resp.text}). Đang gửi lại dạng Plain Text...")
            payload_plain = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text
            }
            resp_fallback = requests.post(url, json=payload_plain, timeout=10)
            print("Telegram Fallback Status:", resp_fallback.status_code)
    except Exception as e:
        print(f"Lỗi kết nối Telegram: {e}")

def main():
    now_utc = datetime.now(timezone.utc)
    trades = load_trades()
    
    # Kiểm tra yêu cầu /check, /status, /lenh từ Telegram
    has_tg_cmd = check_telegram_command_requests()
    is_manual_check = (BOT_MODE.lower() == "check") or ("--check" in sys.argv)
    is_poll_only = (BOT_MODE.lower() == "poll") or ("--poll" in sys.argv)

    if is_poll_only and not has_tg_cmd:
        print("Không có lệnh /check mới từ Telegram.")
        return

    if is_manual_check or (is_poll_only and has_tg_cmd):
        ex_1h, ind_1h, sum_1h = get_tv_analysis_with_retry(interval_str="1h")
        data_1h = extract_market_data(ex_1h, ind_1h, sum_1h)
        evaluate_and_update_trades(trades, data_1h, new_signal=None, is_hourly_step=False)
        save_trades(trades)
        status_msg = generate_live_status_report(trades, data_1h)
        send_telegram(status_msg)
        return

    # Kiểm tra giờ đóng cửa cuối tuần (Thứ Bảy & Chủ Nhật)
    if is_weekend_market_closed(now_utc):
        if has_tg_cmd:
            ex_1h, ind_1h, sum_1h = get_tv_analysis_with_retry(interval_str="1h")
            data_1h = extract_market_data(ex_1h, ind_1h, sum_1h)
            save_trades(trades)
            send_telegram(generate_live_status_report(trades, data_1h))
        else:
            print(f"Thị trường Vàng đang đóng cửa nghỉ cuối tuần ({now_utc.strftime('%Y-%m-%d %H:%M UTC')}). Bỏ qua gửi báo cáo.")
        return

    try:
        # 1. Quét khung 1H trực tiếp từ TradingView
        ex_1h, ind_1h, sum_1h = get_tv_analysis_with_retry(interval_str="1h")
        data_1h = extract_market_data(ex_1h, ind_1h, sum_1h)
        assessment_1h = generate_technical_assessment(data_1h)
        
        # Cập nhật nhật ký theo dõi lệnh (Pending -> Active -> SL/TP)
        evaluate_and_update_trades(trades, data_1h, assessment_1h["trade_signal"], is_hourly_step=True)
        save_trades(trades)
        
        msg_1h = format_telegram_message("1H", data_1h, assessment_1h, trades)
        send_telegram(msg_1h)
        
        # Nếu người dùng có nhắn /check trên Telegram thì gửi kèm luôn bảng chi tiết trạng thái lệnh
        if has_tg_cmd:
            status_msg = generate_live_status_report(trades, data_1h)
            send_telegram(status_msg)
        
        # 2. Quét khung 4H khi đóng nến (01, 05, 09, 13, 17, 21 UTC)
        if now_utc.hour in [1, 5, 9, 13, 17, 21]:
            try:
                ex_4h, ind_4h, sum_4h = get_tv_analysis_with_retry(interval_str="4h")
                data_4h = extract_market_data(ex_4h, ind_4h, sum_4h)
                assessment_4h = generate_technical_assessment(data_4h)
                msg_4h = format_telegram_message("4H", data_4h, assessment_4h)
                send_telegram(msg_4h)
            except Exception as e:
                print(f"Bỏ qua khung 4H do lỗi: {e}")
                
        # 3. Báo cáo tổng kết ngày lúc 22:00 UTC (05:00 sáng VN, từ Thứ Ba đến Thứ Bảy theo giờ VN)
        if now_utc.hour == 22 and now_utc.weekday() in [0, 1, 2, 3, 4]:
            summary_msg = generate_daily_summary(trades)
            send_telegram(summary_msg)
            new_trades = {
                "date": now_utc.strftime("%Y-%m-%d"),
                "single_mode": {"current_trade": trades["single_mode"]["current_trade"], "closed_trades": []},
                "multi_mode": {"open_trades": trades["multi_mode"]["open_trades"], "closed_trades": []}
            }
            save_trades(new_trades)
            
    except Exception as e:
        error_msg = f"⚠️ *Cảnh báo Gold Bot:* Không thể lấy dữ liệu TradingView lúc này: `{e}`"
        print(error_msg)
        send_telegram(error_msg)
        sys.exit(1)

if __name__ == "__main__":
    main()
