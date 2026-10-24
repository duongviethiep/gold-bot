import os
import sys
import json
import copy
import requests
from datetime import datetime, timezone, timedelta

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

def format_vn_time(utc_str):
    """Chuyển chuỗi giờ UTC (VD: '2026-10-23T07:00:00') sang giờ Việt Nam (UTC+7) '14:00'."""
    if not utc_str:
        return "N/A"
    try:
        clean_str = str(utc_str).replace("Z", "").split(".")[0]
        dt = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        dt_vn = dt + timedelta(hours=7)
        return dt_vn.strftime("%H:%M")
    except Exception:
        return str(utc_str)

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
        
