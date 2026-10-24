import os
import sys
import json
import copy
import requests
from datetime import datetime, timezone

# Configurations & Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY")
CAPITAL_IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER")
CAPITAL_PASSWORD = os.getenv("CAPITAL_PASSWORD")

# Tự động thử cả 2 máy chủ Live và Demo của Capital.com
URL_CANDIDATES = [
    "https://api-capital.backend-capital.com",
    "https://demo-api-capital.backend-capital.com"
]
custom_url = os.getenv("CAPITAL_BASE_URL")
if custom_url:
    URL_CANDIDATES.insert(0, custom_url.rstrip("/"))

LOT_SIZE = 0.01
LOT_VALUE_PER_DOLLAR = 1.0  # 0.01 lot XAU/USD: $1.00 biên độ = $1.00 P&L
MAX_PENDING_HOURS = 4       # Lệnh Limit ở Multi Mode chờ quá 4 tiếng không khớp sẽ tự hủy
TRADES_FILE = "daily_trades.json"

def is_weekend_market_closed(now_utc):
    """
    Kiểm tra giờ đóng cửa cuối tuần của thị trường Vàng (XAU/USD) theo giờ UTC:
    - Thứ Sáu (weekday == 4): Sau 22:00 UTC (tức từ 23:00 UTC = 06:00 sáng Thứ Bảy VN) sàn đã nghỉ.
      (Mốc 22:00 UTC Thứ Sáu = 05:00 sáng Thứ Bảy VN vẫn chạy để gửi Báo cáo tổng kết phiên Thứ Sáu).
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

def get_capital_session():
    """Xác thực với Capital.com."""
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "identifier": CAPITAL_IDENTIFIER,
        "password": CAPITAL_PASSWORD,
        "encryptedPassword": False
    }
    
    last_error = ""
    for base_url in URL_CANDIDATES:
        try:
            url = f"{base_url}/api/v1/session"
            print(f"Đang thử kết nối máy chủ Capital.com: {base_url}...")
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                cst = resp.headers.get("CST")
                security_token = resp.headers.get("X-SECURITY-TOKEN")
                if cst and security_token:
                    print(f"-> Đăng nhập thành công qua máy chủ: {base_url}")
                    return base_url, cst, security_token
            last_error = f"[{resp.status_code}] {resp.text}"
            print(f"-> Máy chủ {base_url} trả về lỗi: {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"-> Không thể kết nối tới {base_url}: {last_error}")
            
    raise Exception(f"Đăng nhập Capital.com thất bại trên cả Live và Demo. Chi tiết lỗi: {last_error}")

def fetch_candles(base_url, cst, security_token, epic="GOLD", resolution="HOUR", max_bars=40):
    """Lấy danh sách nến lịch sử từ Capital.com."""
    url = f"{base_url}/api/v1/prices/{epic}"
    headers = {
        "CST": cst,
        "X-SECURITY-TOKEN": security_token
    }
    params = {
        "resolution": resolution,
        "max": max_bars
    }
    
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Lỗi lấy giá [{resp.status_code}]: {resp.text}")
        
    data = resp.json()
    prices = data.get("prices", [])
    if len(prices) < 20:
        raise Exception(f"Dữ liệu nến không đủ: {len(prices)} nến")
        
    candles = []
    for p in prices:
        o = (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2.0
        h = (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2.0
        l = (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2.0
        c = (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2.0
        candles.append({
            "time_utc": p.get("snapshotTimeUTC", p.get("snapshotTime")),
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": p.get("lastTradedVolume", 0)
        })
    return candles

def calculate_technical_indicators(candles):
    """Tính toán chỉ báo dựa trên các nến đã đóng hoàn chỉnh (loại bỏ nến [-1] đang chạy)."""
    closed_candles = candles[:-1]
    
    closes = [c["close"] for c in closed_candles]
    highs = [c["high"] for c in closed_candles]
    lows = [c["low"] for c in closed_candles]
    
    def calc_ema(values, period):
        k = 2 / (period + 1)
        ema = [values[0]]
        for val in values[1:]:
            ema.append(val * k + ema[-1] * (1 - k))
        return ema
        
    ema20 = calc_ema(closes, 20)[-1]
    ema50 = calc_ema(closes, min(len(closes), 50))[-1] if len(closes) >= 50 else calc_ema(closes, 20)[-1]
    
    tr_list = []
    for i in range(1, len(closed_candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    atr14 = sum(tr_list[-14:]) / 14.0 if len(tr_list) >= 14 else sum(tr_list) / len(tr_list)
    
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    avg_gain = sum(gains[-14:]) / 14.0
    avg_loss = sum(losses[-14:]) / 14.0
    rs = (avg_gain / avg_loss) if avg_loss != 0 else 100.0
    rsi14 = 100.0 - (100.0 / (1.0 + rs))
    
    dm_plus, dm_minus = [], []
    for i in range(1, len(closed_candles)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        dm_plus.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        dm_minus.append(down_move if down_move > up_move and down_move > 0 else 0.0)
            
    di_plus = (sum(dm_plus[-14:]) / (sum(tr_list[-14:]) or 1.0)) * 100.0
    di_minus = (sum(dm_minus[-14:]) / (sum(tr_list[-14:]) or 1.0)) * 100.0
    dx_den = di_plus + di_minus
    adx14 = (abs(di_plus - di_minus) / dx_den * 100.0) if dx_den != 0 else 0.0
    
    last_closed = closed_candles[-1]
    recent_highs = highs[-10:]
    recent_lows = lows[-10:]
    
    return {
        "candle": last_closed,
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "atr14": round(atr14, 2),
        "rsi14": round(rsi14, 1),
        "adx14": round(adx14, 1),
        "support": round(min(recent_lows), 2),
        "resistance": round(max(recent_highs), 2)
    }

def generate_analysis(data):
    """Tạo 4 gạch đầu dòng nhận định và tín hiệu lệnh chờ Limit."""
    c = data["candle"]
    close = c["close"]
    ema20 = data["ema20"]
    ema50 = data["ema50"]
    rsi = data["rsi14"]
    adx = data["adx14"]
    sup = data["support"]
    res = data["resistance"]
    atr = data["atr14"]
    
    if close > ema20 and ema20 > ema50:
        trend = "Tăng mạnh (Trên EMA20 & EMA50)"
        bias = "BUY"
    elif close < ema20 and ema20 < ema50:
        trend = "Giảm mạnh (Dưới EMA20 & EMA50)"
        bias = "SELL"
    elif close > ema20:
        trend = "Tăng nhẹ / Phục hồi ngắn hạn"
        bias = "BUY"
    else:
        trend = "Giảm nhẹ / Điều chỉnh ngắn hạn"
        bias = "SELL"
        
    if adx < 20:
        momentum = f"RSI {rsi} - Sideway tích lũy (ADX {adx} < 20 - Khuyến nghị đứng ngoài)"
        is_choppy = True
    else:
        status = "Quá mua" if rsi > 70 else ("Quá bán" if rsi < 30 else "Trung tính")
        momentum = f"RSI {rsi} ({status}) - Xu hướng rõ ràng (ADX {adx})"
        is_choppy = False
        
    key_sr = f"Kháng cự: ${res} - Hỗ trợ: ${sup}"
    
    sl_dist = round(1.5 * atr, 2)
    tp_dist = round(2.0 * atr, 2)
    
    if is_choppy:
        action = "ĐỨNG NGOÀI (Thị trường sideway biên hẹp, không vào lệnh vội)"
        trade_signal = None
    elif bias == "BUY":
        entry = round(max(sup, close - round(0.5 * atr, 2)), 2)
        sl = round(entry - sl_dist, 2)
        tp = round(entry + tp_dist, 2)
        action = f"Chờ Buy Limit quanh ${entry} (Không mua đuổi) - SL: ${sl} (-${sl_dist}) - TP: ${tp} (+${tp_dist})"
        trade_signal = {
            "direction": "BUY",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "status": "PENDING",
            "pending_hours": 0,
            "time": c["time_utc"]
        }
    else:
        entry = round(min(res, close + round(0.5 * atr, 2)), 2)
        sl = round(entry + sl_dist, 2)
        tp = round(entry - tp_dist, 2)
        action = f"Chờ Sell Limit quanh ${entry} (Không bán đuổi) - SL: ${sl} (-${sl_dist}) - TP: ${tp} (+${tp_dist})"
        trade_signal = {
            "direction": "SELL",
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "status": "PENDING",
            "pending_hours": 0,
            "time": c["time_utc"]
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
        "last_1h_candle_time": None,
        "last_4h_candle_time": None,
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
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

def process_single_order(trade, high, low):
    """
    Xử lý vòng đời chuẩn xác của 1 lệnh Limit qua cây nến mới (high, low):
    1. Nếu đang PENDING (Chờ khớp):
       - Phải chạm giá Entry (low <= entry với BUY, high >= entry với SELL) mới chuyển sang ACTIVE (Đã khớp).
       - Nếu trong chính cây nến vừa khớp mà giá quét lủng luôn SL -> tính HIT_SL.
       - Nếu chưa chạm Entry mà giá đã chạy thẳng tới TP -> tính CANCELLED (hụt lệnh / lỡ sóng).
    2. Nếu đã ACTIVE (Đã khớp từ nến trước):
       - Kiểm tra chạm SL (HIT_SL) hoặc chạm TP (HIT_TP).
    """
    direction = trade["direction"]
    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    status = trade.get("status", "PENDING")

    if status == "PENDING":
        if direction == "BUY":
            if low <= entry:
                trade["status"] = "ACTIVE"
                if low <= sl:
                    pnl = round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2)
                    return "CLOSED", pnl, "HIT_SL"
                return "ACTIVE", 0.0, "FILLED"
            elif high >= tp:
                return "CANCELLED", 0.0, "MISSED_TP"
        elif direction == "SELL":
            if high >= entry:
                trade["status"] = "ACTIVE"
                if high >= sl:
                    pnl = round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2)
                    return "CLOSED", pnl, "HIT_SL"
                return "ACTIVE", 0.0, "FILLED"
            elif low <= tp:
                return "CANCELLED", 0.0, "MISSED_TP"
                
        trade["pending_hours"] = trade.get("pending_hours", 0) + 1
        return "PENDING", 0.0, ""

    elif status == "ACTIVE":
        if direction == "BUY":
            if low <= sl:
                pnl = round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2)
                return "CLOSED", pnl, "HIT_SL"
            elif high >= tp:
                pnl = round((tp - entry) * LOT_VALUE_PER_DOLLAR, 2)
                return "CLOSED", pnl, "HIT_TP"
        elif direction == "SELL":
            if high >= sl:
                pnl = round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2)
                return "CLOSED", pnl, "HIT_SL"
            elif low <= tp:
                pnl = round((entry - tp) * LOT_VALUE_PER_DOLLAR, 2)
                return "CLOSED", pnl, "HIT_TP"
        return "ACTIVE", 0.0, ""

    return "CANCELLED", 0.0, ""

def evaluate_and_update_trades(trades, latest_candle, new_signal):
    high, low = latest_candle["high"], latest_candle["low"]
    
    # 1. Chế độ Đơn lệnh (Single Mode - Tối đa 1 vị thế)
    curr = trades.get("single_mode", {}).get("current_trade")
    if isinstance(curr, dict) and "direction" in curr and "entry" in curr and "sl" in curr and "tp" in curr:
        new_status, pnl, outcome = process_single_order(curr, high, low)
        if new_status == "CLOSED":
            curr["status"] = "CLOSED"
            curr["outcome"] = outcome
            curr["pnl"] = pnl
            trades["single_mode"]["closed_trades"].append(curr)
            trades["single_mode"]["current_trade"] = None
        elif new_status == "CANCELLED":
            trades["single_mode"]["current_trade"] = None
        elif new_status == "PENDING":
            # Sau 1 giờ mà lệnh Limit cũ vẫn chưa chạm Entry -> Thay bằng lệnh chờ mới sát giá hơn
            trades["single_mode"]["current_trade"] = None
        elif new_status == "ACTIVE":
            # Đã chạm Entry khớp lệnh -> Giữ nguyên vị thế đang gồng, không nhận đè lệnh mới
            trades["single_mode"]["current_trade"] = curr
    elif curr is not None:
        trades["single_mode"]["current_trade"] = None

    if trades["single_mode"]["current_trade"] is None and new_signal:
        trades["single_mode"]["current_trade"] = copy.deepcopy(new_signal)
        
    # 2. Chế độ Đa lệnh (Multi Mode - Độc lập từng tín hiệu)
    remaining_open = []
    for t in trades.get("multi_mode", {}).get("open_trades", []):
        if not (isinstance(t, dict) and "direction" in t and "entry" in t and "sl" in t and "tp" in t):
            continue
        new_status, pnl, outcome = process_single_order(t, high, low)
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
    if new_signal:
        trades["multi_mode"]["open_trades"].append(copy.deepcopy(new_signal))

def format_telegram_message(tf, data, analysis, trades=None):
    c = data["candle"]
    msg = (
        f"📊 *XAU/USD ({tf}) - CÂY NẾN ĐÃ ĐÓNG*\n"
        f"⏰ *Nến kết thúc:* `{c['time_utc']}` (UTC)\n"
        f"💰 *O:* `{c['open']}` - *H:* `{c['high']}` - *L:* `{c['low']}` - *C:* `{c['close']}`\n\n"
        f"• *Xu hướng:* {analysis['trend']}\n"
        f"• *Động lực:* {analysis['momentum']}\n"
        f"• *Vùng cản:* {analysis['key_sr']}\n"
        f"• *Kế hoạch:* {analysis['action']}\n"
        f"*(ATR: ${data['atr14']} - Khối lượng: 0.01 lot = $1/giá)*"
    )
    if tf == "1H" and trades:
        curr = trades.get("single_mode", {}).get("current_trade")
        if curr:
            st_label = "ĐANG GỒNG (Đã khớp Entry)" if curr.get("status") == "ACTIVE" else "ĐANG CHỜ KHỚP (Pending Limit)"
            msg += f"\n📌 *Trạng thái lệnh đơn:* {st_label} - `{curr['direction']} @ ${curr['entry']}` (SL: `${curr['sl']}` - TP: `${curr['tp']}`)"
        else:
            msg += "\n📌 *Trạng thái lệnh đơn:* Không có lệnh mở"
    return msg

def generate_daily_summary(trades):
    s_closed = trades.get("single_mode", {}).get("closed_trades", [])
    m_closed = trades.get("multi_mode", {}).get("closed_trades", [])
    
    s_pnl = sum(t.get("pnl", 0.0) for t in s_closed)
    s_wins = len([t for t in s_closed if t.get("pnl", 0.0) > 0])
    s_winrate = (s_wins / len(s_closed) * 100) if s_closed else 0.0
    
    m_pnl = sum(t.get("pnl", 0.0) for t in m_closed)
    m_wins = len([t for t in m_closed if t.get("pnl", 0.0) > 0])
    m_winrate = (m_wins / len(m_closed) * 100) if m_closed else 0.0
    
    s_sign = "+$" if s_pnl >= 0 else "-$"
    m_sign = "+$" if m_pnl >= 0 else "-$"
    
    s_curr = trades.get("single_mode", {}).get("current_trade")
    if s_curr:
        s_open_str = f"1 lệnh ({'Đã khớp đang gồng' if s_curr.get('status') == 'ACTIVE' else 'Đang chờ chạm Entry'})"
    else:
        s_open_str = "0"
        
    m_open_list = trades.get("multi_mode", {}).get("open_trades", [])
    m_active_count = len([t for t in m_open_list if t.get("status") == "ACTIVE"])
    m_pending_count = len([t for t in m_open_list if t.get("status") == "PENDING"])
    
    msg = (
        f"📋 *TỔNG KẾT PHIÊN GIAO DỊCH VÀNG (05:00 VN / 22:00 UTC)*\n"
        f"📅 Ngày: `{trades.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))}`\n"
        f"📊 Khối lượng chuẩn hóa: `0.01 lot` ($1 giá = $1.00)\n"
        f"✅ *Quy tắc:* Chỉ tính lệnh đã chạm Entry khớp lệnh thực tế\n\n"
        f"1️⃣ *Chế độ Đơn lệnh (Single Position - Tối đa 1 lệnh)*:\n"
        f"• Lệnh đã khớp & chốt: `{len(s_closed)}` (Thắng: `{s_wins}` - Tỉ lệ: `{s_winrate:.1f}%`)\n"
        f"• Tổng P&L đã chốt: `{s_sign}{abs(s_pnl):.2f}`\n"
        f"• Vị thế hiện tại: `{s_open_str}`\n\n"
        f"2️⃣ *Chế độ Đa lệnh (Multi Positions - Độc lập từng tín hiệu)*:\n"
        f"• Lệnh đã khớp & chốt: `{len(m_closed)}` (Thắng: `{m_wins}` - Tỉ lệ: `{m_winrate:.1f}%`)\n"
        f"• Tổng P&L đã chốt: `{m_sign}{abs(m_pnl):.2f}`\n"
        f"• Đang gồng (Active): `{m_active_count}` - Đang chờ khớp (Pending): `{m_pending_count}`\n"
    )
    return msg

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
        # Nếu lỗi định dạng Markdown (HTTP 400), tự động gửi lại dạng văn bản thường để không mất tin nhắn
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
    
    # Lớp bảo vệ 1: Kiểm tra giờ đóng cửa cuối tuần (Thứ Bảy & Chủ Nhật)
    if is_weekend_market_closed(now_utc):
        print(f"Thị trường Vàng đang đóng cửa nghỉ cuối tuần ({now_utc.strftime('%Y-%m-%d %H:%M UTC')}). Bỏ qua gửi báo cáo.")
        return

    if not CAPITAL_API_KEY or not CAPITAL_IDENTIFIER or not CAPITAL_PASSWORD:
        print("Lỗi: Thiếu CAPITAL_API_KEY, CAPITAL_IDENTIFIER hoặc CAPITAL_PASSWORD")
        sys.exit(1)
        
    base_url, cst, sec_token = get_capital_session()
    trades = load_trades()
    
    # 1. Quét khung 1H & Kiểm tra xem có nến mới thực sự hay sàn đang nghỉ (Lớp bảo vệ 2)
    candles_1h = fetch_candles(base_url, cst, sec_token, epic="GOLD", resolution="HOUR", max_bars=40)
    data_1h = calculate_technical_indicators(candles_1h)
    candle_1h_time = data_1h["candle"]["time_utc"]
    
    if trades.get("last_1h_candle_time") == candle_1h_time:
        print(f"Không có nến 1H mới (Nến cuối: {candle_1h_time} đã được báo cáo). Sàn đang tạm nghỉ.")
    else:
        analysis_1h = generate_analysis(data_1h)
        evaluate_and_update_trades(trades, data_1h["candle"], analysis_1h["trade_signal"])
        trades["last_1h_candle_time"] = candle_1h_time
        save_trades(trades)
        
        msg_1h = format_telegram_message("1H", data_1h, analysis_1h, trades)
        send_telegram(msg_1h)
    
    # 2. Quét khung 4H khi đóng nến (01, 05, 09, 13, 17, 21 UTC)
    if now_utc.hour in [1, 5, 9, 13, 17, 21]:
        candles_4h = fetch_candles(base_url, cst, sec_token, epic="GOLD", resolution="HOUR_4", max_bars=40)
        data_4h = calculate_technical_indicators(candles_4h)
        candle_4h_time = data_4h["candle"]["time_utc"]
        
        if trades.get("last_4h_candle_time") != candle_4h_time:
            analysis_4h = generate_analysis(data_4h)
            trades["last_4h_candle_time"] = candle_4h_time
            save_trades(trades)
            msg_4h = format_telegram_message("4H", data_4h, analysis_4h)
            send_telegram(msg_4h)
        
    # 3. Báo cáo tổng kết ngày lúc 22:00 UTC (05:00 sáng VN, từ Thứ Ba đến Thứ Bảy theo giờ VN)
    if now_utc.hour == 22 and now_utc.weekday() in [0, 1, 2, 3, 4]:
        summary_msg = generate_daily_summary(trades)
        send_telegram(summary_msg)
        new_trades = {
            "date": now_utc.strftime("%Y-%m-%d"),
            "last_1h_candle_time": trades.get("last_1h_candle_time"),
            "last_4h_candle_time": trades.get("last_4h_candle_time"),
            "single_mode": {"current_trade": trades["single_mode"]["current_trade"], "closed_trades": []},
            "multi_mode": {"open_trades": trades["multi_mode"]["open_trades"], "closed_trades": []}
        }
        save_trades(new_trades)

if __name__ == "__main__":
    main()
