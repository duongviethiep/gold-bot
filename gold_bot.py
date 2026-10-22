from datetime import datetime
import html
import json
import os
import random
import re
import string
import sys
import requests
from tradingview_ta import Interval, TA_Handler
import websocket

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TRADINGVIEW_SESSION_ID = os.environ.get("TRADINGVIEW_SESSION_ID", "")
TRADES_FILE = "daily_trades.json"

SYMBOL_FULL = "PEPPERSTONE:XAUUSD"


# ==================== KẾT NỐI WEBSOCKET CHÍNH CHỦ TRADINGVIEW ====================
def generate_tv_session():
  return "cs_" + "".join(
      random.choice(string.ascii_lowercase) for _ in range(12)
  )


def create_tv_message(func, param_list):
  st = json.dumps({"m": func, "p": param_list})
  return f"~m~{len(st)}~m~{st}"


def fetch_tradingview_direct_bars(timeframe="60", n_bars=5):
  """Kết nối vào wss://data.tradingview.com bằng sessionid chính chủ của anh."""
  if not TRADINGVIEW_SESSION_ID:
    print("Cảnh báo: Chưa tìm thấy TRADINGVIEW_SESSION_ID trong Secrets!")
    return None

  try:
    headers = {
        "Origin": "https://www.tradingview.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Cookie": f"sessionid={TRADINGVIEW_SESSION_ID}",
    }
    ws = websocket.create_connection(
        "wss://data.tradingview.com/socket.io/websocket",
        headers=headers,
        timeout=10,
    )

    chart_session = generate_tv_session()
    # Xác thực bằng mã tài khoản chính chủ để không bị chặn IP
    ws.send(create_tv_message("set_auth_token", [TRADINGVIEW_SESSION_ID]))
    ws.send(create_tv_message("chart_create_session", [chart_session, ""]))

    resolve_param = json.dumps({"symbol": SYMBOL_FULL, "adjustment": "splits"})
    ws.send(
        create_tv_message(
            "resolve_symbol", [chart_session, "sds_sym_1", resolve_param]
        )
    )
    ws.send(
        create_tv_message(
            "create_series",
            [chart_session, "sds_1", "s1", "sds_sym_1", str(timeframe), n_bars],
        )
    )

    pattern = r"~m~\d+~m~"
    for _ in range(25):
      raw = ws.recv()
      msgs = [m for m in re.split(pattern, raw) if m.strip()]
      for msg in msgs:
        try:
          data = json.loads(msg)
          if data.get("m") == "timescale_update":
            ws.close()
            return data["p"][1]["s1"]["s"]
        except Exception:
          pass
    ws.close()
  except Exception as e:
    print(f"Lỗi kết nối TradingView WebSocket: {e}")
  return None


def fetch_completed_candle_tradingview(timeframe_str):
  """Lấy chính xác cây nến vừa đóng hoàn toàn (n-1) từ máy chủ TradingView."""
  tf_code = "60" if timeframe_str == "1H" else "240"
  series = fetch_tradingview_direct_bars(tf_code, n_bars=5)
  if series and len(series) >= 2:
    # series[-2] là cây nến vừa đóng xong 100% trên biểu đồ
    c = series[-2]["v"]
    o, h, l, close_p, vol = (
        float(c[1]),
        float(c[2]),
        float(c[3]),
        float(c[4]),
        float(c[5]),
    )
    chg = round((close_p - o) / o * 100, 2)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": close_p,
        "volume": vol,
        "change": chg,
    }
  return None


def fetch_1m_bars_tradingview():
  """Lấy 60 nến 1 phút trực tiếp từ TradingView để dò TP/SL chuẩn xác từng phút."""
  series = fetch_tradingview_direct_bars("1", n_bars=65)
  if series and len(series) >= 10:
    bars = []
    for item in series[-62:-1]:
      b = item["v"]
      t_str = datetime.fromtimestamp(b[0]).strftime("%H:%M")
      bars.append((t_str, float(b[1]), float(b[2]), float(b[3]), float(b[4])))
    return bars
  return None


def fetch_gold_data(timeframe_str):
  interval_map = {
      "1H": Interval.INTERVAL_1_HOUR,
      "4H": Interval.INTERVAL_4_HOURS,
  }
  selected_interval = interval_map.get(timeframe_str, Interval.INTERVAL_1_HOUR)

  handler = TA_Handler(
      symbol="XAUUSD",
      exchange="PEPPERSTONE",
      screener="cfd",
      interval=selected_interval,
  )
  handler.add_indicators(["ATR", "VWAP"])
  analysis = handler.get_analysis()
  ind = analysis.indicators

  atr_val = ind.get("ATR")
  if not atr_val or atr_val == 0.0:
    atr_val = abs(ind.get("high", 0.0) - ind.get("low", 0.0))

  vwap_val = ind.get("VWAP") or ind.get("VWMA") or ind.get("close", 0.0)

  base_data = {
      "timeframe": timeframe_str,
      "close": ind.get("close", 0.0),
      "open": ind.get("open", 0.0),
      "high": ind.get("high", 0.0),
      "low": ind.get("low", 0.0),
      "change": ind.get("change", 0.0),
      "volume": ind.get("volume", 0),
      "rsi": ind.get("RSI", 50.0),
      "macd": ind.get("MACD.macd", 0.0),
      "macd_signal": ind.get("MACD.signal", 0.0),
      "adx": ind.get("ADX", 0.0),
      "atr": atr_val,
      "vwap": vwap_val,
      "ema20": ind.get("EMA20", 0.0),
      "ema50": ind.get("EMA50", 0.0),
      "ema200": ind.get("EMA200", 0.0),
      "bb_upper": ind.get("BB.upper", 0.0),
      "bb_lower": ind.get("BB.lower", 0.0),
      "summary": analysis.summary.get("RECOMMENDATION", "NEUTRAL"),
  }

  # Lấy đúng cây nến vừa đóng trực tiếp từ biểu đồ TradingView
  completed = fetch_completed_candle_tradingview(timeframe_str)
  if completed:
    base_data["open"] = completed["open"]
    base_data["high"] = completed["high"]
    base_data["low"] = completed["low"]
    base_data["close"] = completed["close"]
    base_data["change"] = completed["change"]
    base_data["volume"] = completed["volume"]

  return base_data


# ==================== PHÂN TÍCH VÀ LẬP KẾ HOẠCH ====================
def analyze_and_plan(d):
  close, ema20, ema50 = d["close"], d["ema20"], d["ema50"]
  rsi, macd = d["rsi"], d["macd"]
  adx, atr, vwap = d.get("adx", 0.0), d.get("atr", 0.0), d.get("vwap", 0.0)
  low, high = d["low"], d["high"]
  atr = atr if atr > 0 else 5.0

  if close > ema20 and ema20 > ema50:
    trend = "Tăng (Giá vận động trên EMA20/50)"
    action = "BUY"
    support = (
        round(ema20, 2)
        if (ema20 > 0 and ema20 < close)
        else round(min(low, close), 2)
    )
    resistance = (
        round(max(high, close + 1.5 * atr), 2)
        if high <= close
        else round(high, 2)
    )
  elif close < ema20 and ema20 < ema50:
    trend = "Giảm (Giá chịu áp lực dưới EMA20/50)"
    action = "SELL"
    resistance = (
        round(ema20, 2)
        if (ema20 > 0 and ema20 > close)
        else round(max(high, close), 2)
    )
    support = (
        round(min(low, close - 1.5 * atr), 2)
        if low >= close
        else round(low, 2)
    )
  else:
    trend = "Sideway (Tích lũy giằng co quanh EMA20/50)"
    action = "WAIT"
    support = round(min(low, ema20) if ema20 > 0 else low, 2)
    resistance = round(max(high, ema50) if ema50 > 0 else high, 2)

  mom_rsi = "Quá mua" if rsi > 70 else ("Quá bán" if rsi < 30 else "Ổn định")
  adx_desc = (
      "Trend mạnh" if adx >= 25 else ("Sideway/Yếu" if adx < 20 else "Trung bình")
  )
  momentum = (
      f"RSI {rsi:.1f} ({mom_rsi}) | MACD {macd:.2f} | ADX {adx:.1f}"
      f" ({adx_desc})"
  )
  levels = (
      f"Hỗ trợ: {support:.2f} | Kháng cự: {resistance:.2f} | VWAP: {vwap:.2f}"
  )

  trade_plan = None
  hour_utc = datetime.utcnow().strftime("%H")
  is_near_close = hour_utc in ["20", "21", "22"]

  if action == "BUY" and (adx >= 20 or rsi >= 50) and not is_near_close:
    entry = support
    sl = round(entry - 1.5 * atr, 2)
    tp = round(entry + 2.0 * atr, 2)
    plan = (
        f"Canh Mua tại hỗ trợ <b>{entry:.2f}</b> (SL: <code>{sl:.2f}</code>, TP:"
        f" <code>{tp:.2f}</code>)"
    )
    trade_plan = {"type": "BUY", "entry": entry, "sl": sl, "tp": tp}
  elif action == "SELL" and (adx >= 20 or rsi < 50) and not is_near_close:
    entry = resistance
    sl = round(entry + 1.5 * atr, 2)
    tp = round(entry - 2.0 * atr, 2)
    plan = (
        f"Canh Bán tại kháng cự <b>{entry:.2f}</b> (SL: <code>{sl:.2f}</code>,"
        f" TP: <code>{tp:.2f}</code>)"
    )
    trade_plan = {"type": "SELL", "entry": entry, "sl": sl, "tp": tp}
  else:
    plan = "Đứng ngoài quan sát, thị trường đang tích lũy thiếu lực bứt phá."

  text = (
      f"• <b>Xu hướng:</b> {trend}\n"
      f"• <b>Động lượng:</b> {momentum}\n"
      f"• <b>Vùng cản:</b> {levels}\n"
      f"• <b>Kế hoạch:</b> {plan}"
  )
  return text, trade_plan


def generate_ai_analysis(data):
  text_plan, trade_plan = analyze_and_plan(data)
  if not GEMINI_API_KEY:
    return text_plan, trade_plan

  prompt = f"""
Bạn là chuyên gia phân tích kỹ thuật vàng (XAUUSD). Hãy viết nhận định CỰC KỲ NGẮN GỌN (đúng 4 gạch đầu dòng, không giải thích dài dòng):
• Xu hướng: [Ngắn gọn 1 câu về trend và EMA20/50]
• Động lượng: [RSI {data['rsi']:.1f}, MACD {data['macd']:.2f}, ADX {data['adx']:.1f} nói lên xung lực gì]
• Vùng cản: [Hỗ trợ: X | Kháng cự: Y | VWAP: {data['vwap']:.2f}]
• Kế hoạch: [Canh mua tại hỗ trợ (SL, TP) HOẶC Canh bán tại kháng cự (SL, TP) HOẶC Đứng ngoài quan sát nếu sideway. Không mua đuổi đỉnh / bán đuổi đáy].
"""
  headers = {
      "Content-Type": "application/json",
      "x-goog-api-key": GEMINI_API_KEY,
  }
  payload = {"contents": [{"parts": [{"text": prompt}]}]}

  try:
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    response = requests.post(url, json=payload, headers=headers, timeout=10)
    res_json = response.json()
    if "candidates" in res_json and len(res_json["candidates"]) > 0:
      return (
          res_json["candidates"][0]["content"]["parts"][0]["text"].strip(),
          trade_plan,
      )
  except Exception:
    pass

  return text_plan, trade_plan


# ==================== BỘ QUẢN LÝ LỆNH DÒ NẾN 1 PHÚT ====================
def load_trades():
  today_str = datetime.now().strftime("%Y-%m-%d")
  if os.path.exists(TRADES_FILE):
    try:
      with open(TRADES_FILE, "r") as f:
        data = json.load(f)
        if data.get("date") == today_str:
          return data
    except Exception:
      pass
  return {
      "date": today_str,
      "single_mode": {"current_trade": None, "closed_trades": []},
      "multi_mode": {"open_trades": [], "closed_trades": []},
  }


def save_trades(data):
  try:
    with open(TRADES_FILE, "w") as f:
      json.dump(data, f, indent=2)
  except Exception as e:
    print(f"Lỗi lưu file nhật ký: {e}")


def update_trade_engine(market_data, trade_plan):
  trades = load_trades()
  time_now_str = datetime.now().strftime("%H:%M")
  m1_bars = fetch_1m_bars_tradingview()

  def evaluate_trade(t):
    if m1_bars and len(m1_bars) >= 5:
      for t_time, o, h, l, c in m1_bars:
        if t["status"] == "Chờ đón":
          if t["type"] == "BUY":
            if l <= t["entry"]:
              t["status"] = "Đang chạy"
            elif h >= t["tp"]:
              t["status"] = "Hụt lệnh"
              t["pnl"] = 0.0
              return True
          elif t["type"] == "SELL":
            if h >= t["entry"]:
              t["status"] = "Đang chạy"
            elif l <= t["tp"]:
              t["status"] = "Hụt lệnh"
              t["pnl"] = 0.0
              return True

        if t["status"] == "Đang chạy":
          if t["type"] == "BUY":
            if h >= t["tp"]:
              t["status"] = f"Đạt TP lúc {t_time}"
              t["pnl"] = round(t["tp"] - t["entry"], 2)
              return True
            elif l <= t["sl"]:
              t["status"] = f"Chạm SL lúc {t_time}"
              t["pnl"] = round(-(t["entry"] - t["sl"]), 2)
              return True
          elif t["type"] == "SELL":
            if l <= t["tp"]:
              t["status"] = f"Đạt TP lúc {t_time}"
              t["pnl"] = round(t["entry"] - t["tp"], 2)
              return True
            elif h >= t["sl"]:
              t["status"] = f"Chạm SL lúc {t_time}"
              t["pnl"] = round(-(t["sl"] - t["entry"]), 2)
              return True
      return False

    high, low = market_data["high"], market_data["low"]
    if t["status"] == "Chờ đón":
      if t["type"] == "BUY":
        if low <= t["entry"]:
          t["status"] = "Đang chạy"
        elif high >= t["tp"]:
          t["status"] = "Hụt lệnh"
          t["pnl"] = 0.0
          return True
      elif t["type"] == "SELL":
        if high >= t["entry"]:
          t["status"] = "Đang chạy"
        elif low <= t["tp"]:
          t["status"] = "Hụt lệnh"
          t["pnl"] = 0.0
          return True

    if t["status"] == "Đang chạy":
      if t["type"] == "BUY":
        if high >= t["tp"] and low <= t["sl"]:
          t["status"] = "Chạm SL"
          t["pnl"] = round(-(t["entry"] - t["sl"]), 2)
          return True
        elif high >= t["tp"]:
          t["status"] = "Đạt TP"
          t["pnl"] = round(t["tp"] - t["entry"], 2)
          return True
        elif low <= t["sl"]:
          t["status"] = "Chạm SL"
          t["pnl"] = round(-(t["entry"] - t["sl"]), 2)
          return True
      elif t["type"] == "SELL":
        if low <= t["tp"] and high >= t["sl"]:
          t["status"] = "Chạm SL"
          t["pnl"] = round(-(t["sl"] - t["entry"]), 2)
          return True
        elif low <= t["tp"]:
          t["status"] = "Đạt TP"
          t["pnl"] = round(t["entry"] - t["tp"], 2)
          return True
        elif high >= t["sl"]:
          t["status"] = "Chạm SL"
          t["pnl"] = round(-(t["sl"] - t["entry"]), 2)
          return True
    return False

  curr_s = trades["single_mode"]["current_trade"]
  if curr_s and evaluate_trade(curr_s):
    trades["single_mode"]["closed_trades"].append(curr_s)
    trades["single_mode"]["current_trade"] = None

  still_open_m = []
  for t in trades["multi_mode"]["open_trades"]:
    if evaluate_trade(t):
      trades["multi_mode"]["closed_trades"].append(t)
    else:
      still_open_m.append(t)
  trades["multi_mode"]["open_trades"] = still_open_m

  if trade_plan:
    new_item = {
        "time": time_now_str,
        "type": trade_plan["type"],
        "entry": trade_plan["entry"],
        "sl": trade_plan["sl"],
        "tp": trade_plan["tp"],
        "status": "Chờ đón",
        "pnl": 0.0,
    }
    if trades["single_mode"]["current_trade"] is None:
      trades["single_mode"]["current_trade"] = dict(new_item)

    trades["multi_mode"]["open_trades"].append(dict(new_item))

  save_trades(trades)
  return trades


def build_daily_summary_message(trades, final_close):
  day_str = datetime.now().strftime("%d/%m/%Y")

  def finalize_list(current, closed):
    res = [t for t in closed if t["status"] != "Hụt lệnh"]
    if current:
      c = dict(current)
      if c["status"] == "Đang chạy":
        c["status"] = "Chốt phiên"
        c["pnl"] = (
            round(final_close - c["entry"], 2)
            if c["type"] == "BUY"
            else round(c["entry"] - final_close, 2)
        )
        res.append(c)
    return res

  s_list = finalize_list(
      trades["single_mode"]["current_trade"],
      trades["single_mode"]["closed_trades"],
  )

  m_list = [
      t for t in trades["multi_mode"]["closed_trades"] if t["status"] != "Hụt lệnh"
  ]
  for t in trades["multi_mode"]["open_trades"]:
    if t["status"] == "Đang chạy":
      c = dict(t)
      c["status"] = "Chốt phiên"
      c["pnl"] = (
          round(final_close - c["entry"], 2)
          if c["type"] == "BUY"
          else round(c["entry"] - final_close, 2)
      )
      m_list.append(c)

  def stats(t_list):
    tot = len(t_list)
    wins = sum(1 for t in t_list if t["pnl"] > 0)
    losses = sum(1 for t in t_list if t["pnl"] < 0)
    pnl = sum(t["pnl"] for t in t_list)
    wr = (wins / tot * 100) if tot > 0 else 0.0
    return tot, wins, losses, pnl, wr

  s_tot, s_win, s_loss, s_pnl, s_wr = stats(s_list)
  m_tot, m_win, m_loss, m_pnl, m_wr = stats(m_list)

  def render_detail(t_list):
    if not t_list:
      return "<i>• Không có lệnh khớp trong ngày (Thị trường sideway).</i>"
    lines = []
    for i, t in enumerate(t_list, 1):
      icon = "✅" if t["pnl"] > 0 else ("❌" if t["pnl"] < 0 else "⚪")
      sign = "+" if t["pnl"] > 0 else ""
      lines.append(
          f"{i}. [{t['time']}] <b>{t['type']}</b> {t['entry']:.2f} | TP:"
          f" {t['tp']:.2f} | SL: {t['sl']:.2f} ➔ {t['status']} ({sign}{t['pnl']:.2f}"
          f" USD) {icon}"
      )
    return "\n".join(lines)

  s_sign = "+" if s_pnl > 0 else ""
  m_sign = "+" if m_pnl > 0 else ""

  return f"""📋 <b>TỔNG KẾT GIAO DỊCH VÀNG CUỐI NGÀY (XAU/USD)</b>
⏱ <b>Ngày kết thúc:</b> {day_str} (Khối lượng: <code>0.01 lot</code>)
🎯 <b>Quy tắc:</b> Đón tại hỗ trợ/kháng cự | Dò nến 1m TradingView | SL = 1.5 x ATR | TP = 2.0 x ATR

━━━━━━━━━━━━━━━━━━━━
<b>1️⃣ CHẾ ĐỘ 1 LỆNH DUY NHẤT (Single Position):</b>
• <b>Tổng lệnh:</b> {s_tot} | <b>Thắng:</b> {s_win} ✅ | <b>Thua:</b> {s_loss} ❌ | <b>Tỷ lệ thắng:</b> {s_wr:.1f}%
• <b>Tổng P&L:</b> <code>{s_sign}{s_pnl:.2f} USD</code> ({s_sign}{s_pnl*10:.0f} pips)

<b>Chi tiết lệnh:</b>
{render_detail(s_list)}

━━━━━━━━━━━━━━━━━━━━
<b>2️⃣ CHẾ ĐỘ ĐA LỆNH ĐỘC LẬP (Multi Positions):</b>
• <b>Tổng lệnh:</b> {m_tot} | <b>Thắng:</b> {m_win} ✅ | <b>Thua:</b> {m_loss} ❌ | <b>Tỷ lệ thắng:</b> {m_wr:.1f}%
• <b>Tổng P&L:</b> <code>{m_sign}{m_pnl:.2f} USD</code> ({m_sign}{m_pnl*10:.0f} pips)

<b>Chi tiết lệnh:</b>
{render_detail(m_list)}
"""


# ==================== GỬI THÔNG BÁO TELEGRAM ====================
def send_telegram_raw(text):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  chat_ids = [cid.strip() for cid in TELEGRAM_CHAT_ID.split(",") if cid.strip()]
  for cid in chat_ids:
    try:
      res = requests.post(
          url,
          json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
          timeout=15,
      )
      if res.status_code != 200:
        requests.post(
            url, json={"chat_id": cid, "text": html.unescape(text)}, timeout=15
        )
    except Exception as e:
      print(f"Lỗi gửi tới ID {cid}: {e}")


def send_telegram(data, ai_opinion):
  time_now = datetime.now().strftime("%d/%m/%Y %H:%M")

  message_html = f"""📊 <b>BÁO CÁO GIÁ VÀNG XAU/USD ({data['timeframe']})</b>
⏱ <b>Thời điểm đóng nến:</b> {time_now}

💰 <b>Chỉ số giá:</b>
• Giá đóng cửa: <code>{data['close']:.2f} USD</code> ({data['change']:+.2f}%)
• Biên độ nến: <code>{data['low']:.2f}</code> – <code>{data['high']:.2f}</code>

📈 <b>Chỉ báo kỹ thuật:</b>
• RSI (14): <code>{data['rsi']:.2f}</code> | ADX (14): <code>{data['adx']:.2f}</code>
• MACD: <code>{data['macd']:.2f}</code> | Signal: <code>{data['macd_signal']:.2f}</code>
• EMA: EMA20: <code>{data['ema20']:.2f}</code> | EMA50: <code>{data['ema50']:.2f}</code> | EMA200: <code>{data['ema200']:.2f}</code>
• ATR: <code>{data['atr']:.2f} USD</code> | VWAP: <code>{data['vwap']:.2f} USD</code>
• Tín hiệu kỹ thuật: <b>{data['summary']}</b>

🧠 <b>Nhận định & Phân tích chuyên sâu:</b>
{ai_opinion}
"""
  send_telegram_raw(message_html)


if __name__ == "__main__":
  tf = sys.argv[1] if len(sys.argv) > 1 else "1H"
  market_data = fetch_gold_data(tf)

  if tf == "1H":
    analysis_text, trade_plan = generate_ai_analysis(market_data)
    trade_state = update_trade_engine(market_data, trade_plan)
    send_telegram(market_data, analysis_text)

    hour_utc = datetime.utcnow().strftime("%H")
    if hour_utc == "22" or (len(sys.argv) > 2 and sys.argv[2] == "summary"):
      summary_card = build_daily_summary_message(
          trade_state, market_data["close"]
      )
      send_telegram_raw(summary_card)
      if os.path.exists(TRADES_FILE):
        try:
          os.remove(TRADES_FILE)
        except Exception:
          pass
  else:
    analysis_text, _ = generate_ai_analysis(market_data)
    send_telegram(market_data, analysis_text)
