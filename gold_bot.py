from datetime import datetime
import html
import json
import os
import sys
import requests
from tradingview_ta import Interval, TA_Handler

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TRADES_FILE = "daily_trades.json"


def fetch_gold_data(timeframe_str):
  interval_map = {
      "1H": Interval.INTERVAL_1_HOUR,
      "4H": Interval.INTERVAL_4_HOURS,
  }
  selected_interval = interval_map.get(timeframe_str, Interval.INTERVAL_1_HOUR)

  candidates = [
      {"symbol": "XAUUSD", "exchange": "OANDA", "screener": "cfd"},
      {"symbol": "XAUUSD", "exchange": "PEPPERSTONE", "screener": "cfd"},
      {"symbol": "XAUUSD", "exchange": "FOREXCOM", "screener": "cfd"},
  ]

  analysis = None
  for c in candidates:
    try:
      handler = TA_Handler(
          symbol=c["symbol"],
          exchange=c["exchange"],
          screener=c["screener"],
          interval=selected_interval,
      )
      handler.add_indicators(["ATR", "VWAP"])
      res = handler.get_analysis()
      if res and res.indicators and res.indicators.get("close"):
        analysis = res
        break
    except Exception:
      continue

  if not analysis or not analysis.indicators:
    raise Exception("Chưa lấy được dữ liệu nến XAUUSD từ các sàn.")

  ind = analysis.indicators

  atr_val = ind.get("ATR")
  if not atr_val or atr_val == 0.0:
    atr_val = abs(ind.get("high", 0.0) - ind.get("low", 0.0))

  vwap_val = ind.get("VWAP") or ind.get("VWMA") or ind.get("close", 0.0)

  return {
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


# ==================== BỘ QUẢN LÝ VÀ THEO DÕI LỆNH ====================
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
    print(f"Lỗi lưu file nhật ký lệnh: {e}")


def update_trade_engine(market_data):
  trades = load_trades()
  high, low, close = (
      market_data["high"],
      market_data["low"],
      market_data["close"],
  )
  atr = market_data["atr"] if market_data["atr"] > 0 else 5.0
  time_now_str = datetime.now().strftime("%H:%M")
  summary = market_data["summary"]

  # 1. Cập nhật các lệnh đang mở (Kiểm tra xem nến vừa rồi có chạm TP/SL không)
  def evaluate_trade(t):
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

  # Kiểm tra chế độ Single
  curr_s = trades["single_mode"]["current_trade"]
  if curr_s and evaluate_trade(curr_s):
    trades["single_mode"]["closed_trades"].append(curr_s)
    trades["single_mode"]["current_trade"] = None

  # Kiểm tra chế độ Multi
  still_open_m = []
  for t in trades["multi_mode"]["open_trades"]:
    if evaluate_trade(t):
      trades["multi_mode"]["closed_trades"].append(t)
    else:
      still_open_m.append(t)
  trades["multi_mode"]["open_trades"] = still_open_m

  # 2. Phát sinh lệnh mới theo tín hiệu
  new_type = None
  if summary in ["STRONG_BUY", "BUY"]:
    new_type = "BUY"
  elif summary in ["STRONG_SELL", "SELL"]:
    new_type = "SELL"

  if new_type:
    sl = (
        round(close - 1.5 * atr, 2)
        if new_type == "BUY"
        else round(close + 1.5 * atr, 2)
    )
    tp = (
        round(close + 2.0 * atr, 2)
        if new_type == "BUY"
        else round(close - 2.0 * atr, 2)
    )

    new_trade_template = {
        "time": time_now_str,
        "type": new_type,
        "entry": close,
        "sl": sl,
        "tp": tp,
        "status": "Đang mở",
        "pnl": 0.0,
    }

    # Mục 1: Chỉ mở khi chưa có lệnh nào đang chạy
    if trades["single_mode"]["current_trade"] is None:
      trades["single_mode"]["current_trade"] = dict(new_trade_template)

    # Mục 2: Luôn mở thêm 1 lệnh độc lập
    trades["multi_mode"]["open_trades"].append(dict(new_trade_template))

  save_trades(trades)
  return trades


def build_daily_summary_message(trades, final_close):
  day_str = datetime.now().strftime("%d/%m/%Y")

  # Thu gom toàn bộ lệnh trong ngày (đóng lệnh còn treo theo giá đóng cửa cuối phiên)
  def finalize_list(current, closed):
    res = list(closed)
    if current:
      c = dict(current)
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

  m_list = list(trades["multi_mode"]["closed_trades"])
  for t in trades["multi_mode"]["open_trades"]:
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
      return "<i>• Không phát sinh tín hiệu lệnh trong ngày.</i>"
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

  msg = f"""📋 <b>TỔNG KẾT GIAO DỊCH VÀNG CUỐI NGÀY (XAU/USD)</b>
⏱ <b>Ngày kết thúc:</b> {day_str} (Khối lượng: <code>0.01 lot</code>)
🎯 <b>Quy tắc:</b> SL = 1.5 x ATR | TP = 2.0 x ATR

━━━━━━━━━━━━━━━━━━━━
<b>1️⃣ CHẾ ĐỘ 1 LỆNH DUY NHẤT (Single Position):</b>
<i>(Chờ lệnh hiện tại chạm TP/SL mới nhận tín hiệu lệnh tiếp theo)</i>
• <b>Tổng lệnh:</b> {s_tot} | <b>Thắng:</b> {s_win} ✅ | <b>Thua:</b> {s_loss} ❌ | <b>Tỷ lệ thắng:</b> {s_wr:.1f}%
• <b>Tổng P&L:</b> <code>{s_sign}{s_pnl:.2f} USD</code> ({s_sign}{s_pnl*10:.0f} pips)

<b>Chi tiết lệnh:</b>
{render_detail(s_list)}

━━━━━━━━━━━━━━━━━━━━
<b>2️⃣ CHẾ ĐỘ ĐA LỆNH ĐỘC LẬP (Multi Positions):</b>
<i>(Mỗi cây nến có tín hiệu đều mở 1 lệnh riêng biệt)</i>
• <b>Tổng lệnh:</b> {m_tot} | <b>Thắng:</b> {m_win} ✅ | <b>Thua:</b> {m_loss} ❌ | <b>Tỷ lệ thắng:</b> {m_wr:.1f}%
• <b>Tổng P&L:</b> <code>{m_sign}{m_pnl:.2f} USD</code> ({m_sign}{m_pnl*10:.0f} pips)

<b>Chi tiết lệnh:</b>
{render_detail(m_list)}
"""
  return msg


# ==================== PHÂN TÍCH VÀ GỬI TIN ====================
def build_fallback_analysis(d):
  close, ema20, ema50, ema200 = d["close"], d["ema20"], d["ema50"], d["ema200"]
  rsi, macd, macd_sig = d["rsi"], d["macd"], d["macd_signal"]
  adx, atr, vwap = d.get("adx", 0.0), d.get("atr", 0.0), d.get("vwap", 0.0)
  low, high, bb_up = d["low"], d["high"], d["bb_upper"]

  if close > ema20 and ema20 > ema50:
    trend = (
        f"Xu hướng tăng ngắn hạn duy trì tích cực khi giá ({close:.2f}) nằm"
        f" trên dải EMA20 ({ema20:.2f}) và EMA50 ({ema50:.2f})."
    )
  elif close < ema20 and ema20 < ema50:
    trend = (
        f"Xu hướng giảm ngắn hạn chiếm ưu thế khi giá ({close:.2f}) chịu áp lực"
        f" cản dưới EMA20 ({ema20:.2f}) và EMA50 ({ema50:.2f})."
    )
  else:
    trend = (
        f"Thị trường đang tích lũy giằng co quanh vùng hội tụ EMA20"
        f" ({ema20:.2f}) và EMA50 ({ema50:.2f})."
    )

  adx_str = (
      f"Chỉ số ADX ({adx:.1f}) > 25 xác nhận xu hướng đang rất mạnh."
      if adx >= 25
      else (
          f"Chỉ số ADX ({adx:.1f}) < 20 cho thấy thị trường đang sideway giằng"
          " co."
          if adx > 0
          else ""
      )
  )
  mom_rsi = (
      f"RSI ({rsi:.1f}) chạm vùng quá mua."
      if rsi > 70
      else (
          f"RSI ({rsi:.1f}) vào vùng quá bán."
          if rsi < 30
          else f"RSI ({rsi:.1f}) ở mức trung tính ổn định."
      )
  )
  mom_macd = (
      f"Đường MACD ({macd:.2f}) nằm trên Signal ({macd_sig:.2f}) củng cố lực"
      " mua."
      if macd > macd_sig
      else (
          f"Đường MACD ({macd:.2f}) dưới Signal ({macd_sig:.2f}) cho thấy lực"
          " mua suy yếu."
      )
  )
  momentum = f"{mom_rsi} {mom_macd} {adx_str}".strip()

  support = min(low, ema20) if ema20 > 0 else low
  resistance = max(high, bb_up) if bb_up > 0 else high
  vwap_str = (
      f" Giá đang ở {'trên' if close >= vwap else 'dưới'} mốc trung bình dòng"
      f" tiền ({vwap:.2f} USD)."
      if vwap > 0
      else ""
  )
  sr = (
      f"Hỗ trợ gần nhất quanh {support:.2f} USD; kháng cự cần vượt là"
      f" {resistance:.2f} USD.{vwap_str}"
  )

  atr_str = (
      f" (Biên độ dao động ATR ~{atr:.2f} USD, gợi ý khoảng cắt lỗ an toàn"
      f" ~{1.5*atr:.2f} USD)."
      if atr > 0
      else "."
  )
  if close > ema20 and rsi >= 50 and macd > macd_sig:
    scenario = (
        "Ưu tiên canh mua thuận xu hướng khi giá hồi về kiểm định vùng"
        f" {support:.2f} USD, mục tiêu hướng tới {resistance:.2f} USD{atr_str}"
    )
  elif close < ema20 and rsi < 50:
    scenario = (
        f"Ưu tiên canh bán lướt sóng nếu giá hồi lên vùng cản {ema20:.2f} USD"
        f" nhưng suy yếu{atr_str}"
    )
  else:
    scenario = (
        f"Giá đang dao động tích lũy trong vùng {support:.2f} -"
        f" {resistance:.2f} USD, khuyến nghị chờ tín hiệu phá vỡ rõ ràng{atr_str}"
    )

  return f"• {trend}\n• {momentum}\n• {sr}\n• {scenario}"


def generate_ai_analysis(data):
  if not GEMINI_API_KEY:
    return build_fallback_analysis(data)

  prompt = f"""
Bạn là chuyên gia phân tích kỹ thuật thị trường vàng (XAUUSD). Hãy viết nhận định ngắn gọn dựa trên các thông số nến vừa đóng khung {data['timeframe']}:
- Giá đóng cửa: {data['close']:.2f} (Biên độ: {data['low']:.2f} - {data['high']:.2f})
- RSI (14): {data['rsi']:.2f} | ADX (14): {data['adx']:.2f}
- MACD: {data['macd']:.2f} (Signal: {data['macd_signal']:.2f})
- Đường xu hướng: EMA20={data['ema20']:.2f}, EMA50={data['ema50']:.2f}, EMA200={data['ema200']:.2f}
- Dải Bollinger: {data['bb_lower']:.2f} - {data['bb_upper']:.2f}
- Biến động & Dòng tiền: ATR={data['atr']:.2f} USD, VWAP/VWMA={data['vwap']:.2f} USD
- Tín hiệu tổng quan: {data['summary']}

Trình bày đúng 4 gạch đầu dòng ngắn gọn:
1. Đánh giá cấu trúc xu hướng và vị thế giá so với các đường EMA.
2. Trạng thái động lượng và sức mạnh xu hướng (kết hợp RSI, MACD và ADX).
3. Vị thế dòng tiền so với VWAP cùng các vùng hỗ trợ/kháng cự trọng yếu.
4. Kịch bản xu hướng tiếp theo và khoảng cách dừng lỗ/chốt lời gợi ý theo ATR.
Không dùng lời chào hỏi, đi thẳng vào các gạch đầu dòng.
"""

  headers = {
      "Content-Type": "application/json",
      "x-goog-api-key": GEMINI_API_KEY,
  }
  payload = {"contents": [{"parts": [{"text": prompt}]}]}

  models = ["gemini-2.5-flash-lite", "gemini-3.8-flash"]
  for model_name in models:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    try:
      response = requests.post(url, json=payload, headers=headers, timeout=12)
      res_json = response.json()
      if "candidates" in res_json and len(res_json["candidates"]) > 0:
        return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
      pass

  return build_fallback_analysis(data)


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
  safe_opinion = html.escape(ai_opinion)

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
{safe_opinion}
"""
  send_telegram_raw(message_html)


if __name__ == "__main__":
  tf = sys.argv[1] if len(sys.argv) > 1 else "1H"
  market_data = fetch_gold_data(tf)

  if tf == "1H":
    # 1. Cập nhật nhật ký lệnh theo nến 1H
    trade_state = update_trade_engine(market_data)

    # 2. Gửi báo cáo nến 1H định kỳ
    analysis_text = generate_ai_analysis(market_data)
    send_telegram(market_data, analysis_text)

    # 3. Kiểm tra xem có đúng mốc 05:00 sáng VN (tức 22:00 UTC - kết phiên Mỹ) để gửi tổng kết ngày không
    hour_utc = datetime.utcnow().strftime("%H")
    if hour_utc == "22" or (len(sys.argv) > 2 and sys.argv[2] == "summary"):
      summary_card = build_daily_summary_message(
          trade_state, market_data["close"]
      )
      send_telegram_raw(summary_card)
      # Xóa file nhật ký để ngày mới bắt đầu lại từ đầu
      if os.path.exists(TRADES_FILE):
        try:
          os.remove(TRADES_FILE)
        except Exception:
          pass
  else:
    # Báo cáo khung 4H
    analysis_text = generate_ai_analysis(market_data)
    send_telegram(market_data, analysis_text)
