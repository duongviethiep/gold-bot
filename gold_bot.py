import os
import sys
import json
from datetime import datetime, timezone
import requests

# Cấu hình môi trường
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TV_SYMBOL = os.getenv("TV_SYMBOL", "XAUUSD")
TV_EXCHANGE = os.getenv("TV_EXCHANGE", "PEPPERSTONE")
TV_SCREENER = os.getenv("TV_SCREENER", "cfd")

LOT_SIZE = 0.01
LOT_VALUE_PER_DOLLAR = 1.0  # 0.01 lot XAU/USD: $1.00 biên độ = $1.00 P&L
TRADES_FILE = "daily_trades.json"

def get_tv_analysis(symbol=TV_SYMBOL, exchange=TV_EXCHANGE, screener=TV_SCREENER, interval_str="1h"):
    """
    Lấy dữ liệu kỹ thuật và giá trực tiếp từ TradingView.
    Hỗ trợ tự động dự phòng nếu sàn chính nghẽn tạm thời.
    """
    from tradingview_ta import TA_Handler, Interval
    
    interval_map = {
        "1h": Interval.INTERVAL_1_HOUR,
        "4h": Interval.INTERVAL_4_HOURS
    }
    tv_interval = interval_map.get(interval_str, Interval.INTERVAL_1_HOUR)
    
    exchanges_to_try = [exchange]
    for fallback in ["PEPPERSTONE", "OANDA", "TVC", "FOREXCOM"]:
        if fallback not in exchanges_to_try:
            exchanges_to_try.append(fallback)
            
    last_err = None
    for ex in exchanges_to_try:
        try:
            handler = TA_Handler(
                symbol=symbol,
                exchange=ex,
                screener=screener,
                interval=tv_interval
            )
            analysis = handler.get_analysis()
            if analysis and analysis.indicators:
                return ex, analysis
        except Exception as e:
            last_err = e
            continue
            
    raise Exception(f"Không thể kết nối TradingView cho {symbol}: {last_err}")

def extract_market_data(exchange_used, analysis):
    """Trích xuất giá và chỉ báo kỹ thuật từ TradingView."""
    ind = analysis.indicators
    
    price = round(ind.get("close", 0.0), 2)
    o = round(ind.get("open", price), 2)
    h = round(ind.get("high", price), 2)
    l = round(ind.get("low", price), 2)
    
    ema20 = round(ind.get("EMA20", ind.get("EMA10", price)), 2)
    ema50 = round(ind.get("EMA50", ind.get("EMA20", price)), 2)
    rsi = round(ind.get("RSI", 50.0), 1)
    adx = round(ind.get("ADX", 25.0), 1)
    
    atr = ind.get("ATR", None)
    if atr is None or atr <= 0:
        atr = round(max(h - l, 15.0), 2)
    else:
        atr = round(atr, 2)
        
    sup = ind.get("Pivot.M.Classic.S1", None)
    res = ind.get("Pivot.M.Classic.R1", None)
    sup = round(l - 5.0, 2) if sup is None or sup <= 0 else round(sup, 2)
    res = round(h + 5.0, 2) if res is None or res <= 0 else round(res, 2)
        
    recommendation = analysis.summary.get("RECOMMENDATION", "NEUTRAL")
    
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
    """Tạo 4 gạch đầu dòng nhận định kỹ thuật và tín hiệu hành động."""
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
        trade_signal = {"direction": "BUY", "entry": entry, "sl": sl, "tp": tp, "time": data["time_str"]}
    else:
        entry = round(min(res, price + round(0.4 * atr, 2)), 2)
        sl = round(entry + sl_dist, 2)
        tp = round(entry - tp_dist, 2)
        action = f"Canh Sell Limit quanh ${entry} (Không bán đuổi) - SL: ${sl} (-${sl_dist}) - TP: ${tp} (+${tp_dist})"
        trade_signal = {"direction": "SELL", "entry": entry, "sl": sl, "tp": tp, "time": data["time_str"]}
        
    return {
        "trend": trend,
        "momentum": momentum,
        "key_sr": key_sr,
        "action": action,
        "trade_signal": trade_signal
    }

def format_telegram_message(tf, data, assessment):
    msg = (
        f"📊 *XAU/USD ({tf}) - TRADINGVIEW LIVE*\n"
        f"🏦 *Sàn:* `{data['exchange']}` | ⏰ *Cập nhật:* `{data['time_str']}` UTC\n"
        f"💰 *Giá hiện tại:* `{data['price']}`\n"
        f"📈 *Biên độ:* O: `{data['open']}` - H: `{data['high']}` - L: `{data['low']}`\n\n"
        f"• *Xu hướng:* {assessment['trend']}\n"
        f"• *Động lực:* {assessment['momentum']}\n"
        f"• *Vùng cản:* {assessment['key_sr']}\n"
        f"• *Kế hoạch:* {assessment['action']}\n"
        f"*(ATR: ${data['atr']} - Chuẩn hóa: 0.01 lot = $1/giá)*"
    )
    return msg

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
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

def evaluate_and_update_trades(trades, current_market, new_signal):
    high, low = current_market["high"], current_market["low"]
    
    # 1. Single Mode
    curr = trades.get("single_mode", {}).get("current_trade")
    if isinstance(curr, dict) and "direction" in curr and "entry" in curr and "sl" in curr and "tp" in curr:
        direction = curr["direction"]
        entry = curr["entry"]
        sl = curr["sl"]
        tp = curr["tp"]
        closed, pnl, outcome = False, 0.0, ""
        
        if direction == "BUY":
            if low <= sl:
                closed, pnl, outcome = True, round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif high >= tp:
                closed, pnl, outcome = True, round((tp - entry) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
        elif direction == "SELL":
            if high >= sl:
                closed, pnl, outcome = True, round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif low <= tp:
                closed, pnl, outcome = True, round((entry - tp) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
                
        if closed:
            curr["outcome"] = outcome
            curr["pnl"] = pnl
            trades["single_mode"]["closed_trades"].append(curr)
            trades["single_mode"]["current_trade"] = None
    elif curr is not None:
        trades["single_mode"]["current_trade"] = None

    if trades["single_mode"]["current_trade"] is None and new_signal:
        trades["single_mode"]["current_trade"] = new_signal
        
    # 2. Multi Mode
    remaining_open = []
    for t in trades.get("multi_mode", {}).get("open_trades", []):
        if not (isinstance(t, dict) and "direction" in t and "entry" in t and "sl" in t and "tp" in t):
            continue
        direction = t["direction"]
        entry = t["entry"]
        sl = t["sl"]
        tp = t["tp"]
        closed, pnl, outcome = False, 0.0, ""
        
        if direction == "BUY":
            if low <= sl:
                closed, pnl, outcome = True, round((sl - entry) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif high >= tp:
                closed, pnl, outcome = True, round((tp - entry) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
        elif direction == "SELL":
            if high >= sl:
                closed, pnl, outcome = True, round((entry - sl) * LOT_VALUE_PER_DOLLAR, 2), "HIT_SL"
            elif low <= tp:
                closed, pnl, outcome = True, round((entry - tp) * LOT_VALUE_PER_DOLLAR, 2), "HIT_TP"
                
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
    
    msg = (
        f"📋 *TỔNG KẾT PHIÊN GIAO DỊCH VÀNG (05:00 VN / 22:00 UTC)*\n"
        f"📅 Ngày: `{trades.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))}`\n"
        f"📊 Khối lượng: `0.01 lot` ($1 giá = $1.00)\n\n"
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
    # 1. Quét khung 1H trực tiếp từ TradingView
    ex_1h, tv_1h = get_tv_analysis(interval_str="1h")
    data_1h = extract_market_data(ex_1h, tv_1h)
    assessment_1h = generate_technical_assessment(data_1h)
    msg_1h = format_telegram_message("1H", data_1h, assessment_1h)
    send_telegram(msg_1h)
    
    # Cập nhật nhật ký theo dõi lệnh
    trades = load_trades()
    evaluate_and_update_trades(trades, data_1h, assessment_1h["trade_signal"])
    save_trades(trades)
    
    # 2. Quét khung 4H khi đóng nến (01, 05, 09, 13, 17, 21 UTC)
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour in [1, 5, 9, 13, 17, 21]:
        ex_4h, tv_4h = get_tv_analysis(interval_str="4h")
        data_4h = extract_market_data(ex_4h, tv_4h)
        assessment_4h = generate_technical_assessment(data_4h)
        msg_4h = format_telegram_message("4H", data_4h, assessment_4h)
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
