import os
import sys
import json
import requests
from datetime import datetime, timezone

# Configurations & Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY")
CAPITAL_IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER")
CAPITAL_PASSWORD = os.getenv("CAPITAL_PASSWORD")
CAPITAL_BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://demo-api-capital.backend-capital.com")

LOT_SIZE = 0.01
LOT_VALUE_PER_DOLLAR = 1.0  # 0.01 lot XAU/USD: $1.00 biên độ = $1.00 P&L
TRADES_FILE = "daily_trades.json"

def get_capital_session():
    """Xác thực với Capital.com để lấy CST và X-SECURITY-TOKEN."""
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "identifier": CAPITAL_IDENTIFIER,
        "password": CAPITAL_PASSWORD,
        "encryptedPassword": False
    }
    
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Đăng nhập Capital.com thất bại [{resp.status_code}]: {resp.text}")
        
    cst = resp.headers.get("CST")
    security_token = resp.headers.get("X-SECURITY-TOKEN")
    
    if not cst or not security_token:
        raise Exception("Không tìm thấy CST hoặc X-SECURITY-TOKEN trong phản hồi của Capital.com")
        
    return cst, security_token

def fetch_candles(cst, security_token, epic="GOLD", resolution="HOUR", max_bars=40):
    """Lấy danh sách nến lịch sử từ Capital.com."""
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
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
    """
    Tính ATR(14), EMA20, EMA50, RSI(14), ADX(14).
    Chỉ dùng các cây nến đã đóng hoàn chỉnh (loại bỏ nến [-1] đang chạy).
    """
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
    """Tạo đúng 4 gạch đầu dòng cô đọng."""
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
        momentum = f"RSI {rsi} | Sideway tích lũy (ADX {adx} < 20 - Khuyến nghị đứng ngoài)"
        is_choppy = True
    else:
        status = "Quá mua" if rsi > 70 else ("Quá bán" if rsi < 30 else "Trung tính")
        momentum = f"RSI {rsi} ({status}) | Xu hướng có lực rõ (ADX {adx})"
        is_choppy = False
        
    key_sr = f"Kháng cự: ${res} \vert{} Hỗ trợ: ${sup}"
    
    sl_dist = round(1.5 * atr, 2)
    tp_dist = round(2.0 * atr, 2)
    
    if is_choppy:
        action = "ĐỨNG NGOÀI (Né sideway, không vào lệnh vội)"
        trade_signal = None
    elif bias == "BUY":
        entry = round(max(sup, close - round(0.5 * atr, 2)), 2)
        sl = round(entry - sl_dist, 2)
        tp = round(entry + tp_dist, 2)
        action = f"Chờ Buy Limit quanh ${entry} (Không mua đuổi) | SL: ${sl} (-${sl_dist}) | TP: ${tp} (+${tp_dist})"
        trade_signal = {"direction": "BUY", "entry": entry, "sl": sl, "tp": tp, "time": c["time_utc"]}
    else:
        entry = round(min(res, close + round(0.5 * atr, 2)), 2)
        sl = round(entry + sl_dist, 2)
        tp = round(entry - tp_dist, 2)
        action = f"Chờ Sell Limit quanh ${entry} (Không bán đuổi) | SL: ${sl} (-${sl_dist}) | TP: ${tp} (+${tp_dist})"
        trade_signal = {"direction": "SELL", "entry": entry, "sl": sl, "tp": tp, "time": c["time_utc"]}
        
    return {
        "trend": trend,
        "momentum": momentum,
        "key_sr": key_sr,
        "action": action,
        "trade_signal": trade_signal
    }

def format_telegram_message(tf, data, analysis):
    c = data["candle"]
    msg = (
        f"📊 *XAU/USD ({tf}) - CÂY NẾN ĐÃ ĐÓNG*\n"
        f"⏰ *Nến kết thúc:* `{c['time_utc']}` (UTC)\n"
        f"💰 *O:* `{c['open']}` | *H:* `{c['high']}` | *L:* `{c['low']}` | *C:* `{c['close']}`\n\n"
        f"• *Xu hướng:* {analysis['trend']}\n"
        f"• *Động lực:* {analysis['momentum']}\n"
        f"• *Vùng cản:* {analysis['key_sr']}\n"
        f"• *Kế hoạch:* {analysis['action']}\n"
        f"*(ATR: ${data['atr14']} | Khối lượng: 0.01 lot = $1/giá)*"
    )
    return msg

def load_trades():
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "date": today_str,
        "single_mode": {"current_trade": None, "closed_trades": []},
        "multi_mode": {"open_trades": [], "closed_trades": []}
    }

def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

def evaluate_and_update_trades(trades, latest_candle, new_signal):
    high, low = latest_candle["high"], latest_candle["low"]
    
    # Single Mode
    curr = trades["single_mode"]["current_trade"]
    if curr:
        closed, pnl, outcome = False, 0.0, ""
        if curr["direction"] == "BUY":
            if low <= curr["sl"]:
                closed, pnl, outcome = True, round((curr["sl"] - curr["entry"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif high >= curr["tp"]:
                closed, pnl, outcome = True, round((curr["tp"] - curr["entry"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
        elif curr["direction"] == "SELL":
            if high >= curr["sl"]:
                closed, pnl, outcome = True, round((curr["entry"] - curr["sl"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif low <= curr["tp"]:
                closed, pnl, outcome = True, round((curr["entry"] - curr["tp"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
                
        if closed:
            curr["outcome"] = outcome
            curr["pnl"] = pnl
            trades["single_mode"]["closed_trades"].append(curr)
            trades["single_mode"]["current_trade"] = None

    if trades["single_mode"]["current_trade"] is None and new_signal:
        trades["single_mode"]["current_trade"] = new_signal
        
    # Multi Mode
    remaining_open = []
    for t in trades["multi_mode"]["open_trades"]:
        closed, pnl, outcome = False, 0.0, ""
        if t["direction"] == "BUY":
            if low <= t["sl"]:
                closed, pnl, outcome = True, round((t["sl"] - t["entry"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif high >= t["tp"]:
                closed, pnl, outcome = True, round((t["tp"] - t["entry"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
        elif t["direction"] == "SELL":
            if high >= t["sl"]:
                closed, pnl, outcome = True, round((t["entry"] - t["sl"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif low <= t["tp"]:
                closed, pnl, outcome = True, round((t["entry"] - t["tp"]) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
                
        if closed:
            t["outcome"] = outcome
            t["pnl"] = pnl
            trades["multi_mode"]["closed_trades"].append(t)
        else:
            remaining_open.append(t)
            
    trades["multi_mode"]["open_trades"] = remaining_open
    if new_signal:
        trades["multi_mode"]["open_trades"].append(new_signal)

def generate_daily_summary(trades):
    s_closed = trades["single_mode"]["closed_trades"]
    m_closed = trades["multi_mode"]["closed_trades"]
    
    s_pnl = sum(t["pnl"] for t in s_closed)
    s_wins = len([t for t in s_closed if t["pnl"] > 0])
    s_winrate = (s_wins / len(s_closed) * 100) if s_closed else 0.0
    
    m_pnl = sum(t["pnl"] for t in m_closed)
    m_wins = len([t for t in m_closed if t["pnl"] > 0])
    m_winrate = (m_wins / len(m_closed) * 100) if m_closed else 0.0
    
    s_sign = "+$" if s_pnl >= 0 else "-$"
    m_sign = "+$" if m_pnl >= 0 else "-$"
    
    msg = (
        f"📋 *TỔNG KẾT PHIÊN GIAO DỊCH VÀNG (05:00 VN / 22:00 UTC)*\n"
        f"📅 Ngày: `{trades.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))}`\n"
        f"📊 Khối lượng chuẩn hóa: `0.01 lot` ($1 giá = $1.00)\n\n"
        f"1️⃣ *Chế độ Đơn lệnh (Single Position - Tối đa 1 lệnh)*:\n"
        f"• Tổng lệnh đã chốt: `{len(s_closed)}` (Thắng: `{s_wins}` - Tỉ lệ: `{s_winrate:.1f}%`)\n"
        f"• Tổng P&L: `{s_sign}{abs(s_pnl):.2f}`\n\n"
        f"2️⃣ *Chế độ Đa lệnh (Multi Positions - Độc lập từng tín hiệu)*:\n"
        f"• Tổng lệnh đã chốt: `{len(m_closed)}` (Thắng: `{m_wins}` - Tỉ lệ: `{m_winrate:.1f}%`)\n"
        f"• Tổng P&L: `{m_sign}{abs(m_pnl):.2f}`\n"
        f"• Lệnh đang còn mở: `{len(trades['multi_mode']['open_trades'])}`\n"
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
    resp = requests.post(url, json=payload, timeout=10)
    print("Telegram Status:", resp.status_code)

def main():
    if not CAPITAL_API_KEY or not CAPITAL_IDENTIFIER or not CAPITAL_PASSWORD:
        print("Lỗi: Thiếu CAPITAL_API_KEY, CAPITAL_IDENTIFIER hoặc CAPITAL_PASSWORD")
        sys.exit(1)
        
    cst, sec_token = get_capital_session()
    
    # 1. Quét khung 1H
    candles_1h = fetch_candles(cst, sec_token, epic="GOLD", resolution="HOUR", max_bars=40)
    data_1h = calculate_technical_indicators(candles_1h)
    analysis_1h = generate_analysis(data_1h)
    msg_1h = format_telegram_message("1H", data_1h, analysis_1h)
    send_telegram(msg_1h)
    
    # Cập nhật nhật ký lệnh
    trades = load_trades()
    evaluate_and_update_trades(trades, data_1h["candle"], analysis_1h["trade_signal"])
    save_trades(trades)
    
    # 2. Quét khung 4H khi đóng nến (01, 05, 09, 13, 17, 21 UTC)
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour in [1, 5, 9, 13, 17, 21]:
        candles_4h = fetch_candles(cst, sec_token, epic="GOLD", resolution="HOUR_4", max_bars=40)
        data_4h = calculate_technical_indicators(candles_4h)
        analysis_4h = generate_analysis(data_4h)
        msg_4h = format_telegram_message("4H", data_4h, analysis_4h)
        send_telegram(msg_4h)
        
    # 3. Báo cáo tổng kết ngày lúc 22:00 UTC (05:00 sáng VN)
    if now_utc.hour == 22:
        summary_msg = generate_daily_summary(trades)
        send_telegram(summary_msg)
        new_trades = {
            "date": now_utc.strftime("%Y-%m-%d"),
            "single_mode": {"current_trade": trades["single_mode"]["current_trade"], "closed_trades": []},
            "multi_mode": {"open_trades": trades["multi_mode"]["open_trades"], "closed_trades": []}
        }
        save_trades(new_trades)

if __name__ == "__main__":
    main()
