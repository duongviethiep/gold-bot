from datetime import datetime
import html
import os
import sys
import requests
from tradingview_ta import Interval, TA_Handler

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


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
      res = handler.get_analysis()
      if res and res.indicators and res.indicators.get("close"):
        analysis = res
        break
    except Exception:
      continue

  if not analysis or not analysis.indicators:
    raise Exception("Chưa lấy được dữ liệu nến XAUUSD từ các sàn.")

  ind = analysis.indicators
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
      "ema20": ind.get("EMA20", 0.0),
      "ema50": ind.get("EMA50", 0.0),
      "ema200": ind.get("EMA200", 0.0),
      "bb_upper": ind.get("BB.upper", 0.0),
      "bb_lower": ind.get("BB.lower", 0.0),
      "summary": analysis.summary.get("RECOMMENDATION", "NEUTRAL"),
  }


def build_fallback_analysis(d):
  """Hệ thống phân tích kỹ thuật dự phòng tự động tính toán khi Google AI bận."""
  close, ema20, ema50, ema200 = d["close"], d["ema20"], d["ema50"], d["ema200"]
  rsi, macd, macd_sig = d["rsi"], d["macd"], d["macd_signal"]
  low, high, bb_up, bb_low = (
      d["low"],
      d["high"],
      d["bb_upper"],
      d["bb_lower"],
  )

  # 1. Cấu trúc xu hướng & vị thế EMA
  if close > ema20 and ema20 > ema50:
    trend = (
        f"Xu hướng tăng ngắn hạn duy trì tích cực khi giá ({close:.2f}) vận"
        f" động vững chắc trên dải EMA20 ({ema20:.2f}) và EMA50 ({ema50:.2f})."
    )
  elif close < ema20 and ema20 < ema50:
    trend = (
        f"Xu hướng giảm ngắn hạn chiếm ưu thế khi giá ({close:.2f}) nằm dưới áp"
        f" lực cản của EMA20 ({ema20:.2f}) và EMA50 ({ema50:.2f})."
    )
  else:
    trend = (
        f"Thị trường đang tích lũy giằng co quanh vùng hội tụ EMA20"
        f" ({ema20:.2f}) và EMA50 ({ema50:.2f}), chưa bứt phá rõ rệt."
    )

  # 2. Động lượng RSI & MACD
  if rsi > 70:
    mom_rsi = (
        f"RSI ({rsi:.1f}) chạm vùng quá mua, cảnh báo áp lực chốt lời rung lắc."
    )
  elif rsi < 30:
    mom_rsi = (
        f"RSI ({rsi:.1f}) vào vùng quá bán, tiềm ẩn nhịp hồi phục kỹ thuật."
    )
  else:
    mom_rsi = f"RSI ({rsi:.1f}) vận động ở mức trung tính ổn định."

  if macd > macd_sig:
    mom_macd = (
        f"Đường MACD ({macd:.2f}) duy trì trên Signal ({macd_sig:.2f}), lực"
        " mua chiếm ưu thế."
    )
  else:
    mom_macd = (
        f"Đường MACD ({macd:.2f}) cắt dưới Signal ({macd_sig:.2f}), động lượng"
        " tăng đang suy yếu."
    )
  momentum = f"{mom_rsi} {mom_macd}"

  # 3. Vùng hỗ trợ & kháng cự
  support = min(low, ema20) if ema20 > 0 else low
  resistance = max(high, bb_up) if bb_up > 0 else high
  sr = (
      f"Vùng hỗ trợ then chốt gần nhất quanh {support:.2f} USD; vùng cản kháng"
      f" cự cần vượt qua là {resistance:.2f} USD."
  )

  # 4. Kịch bản xu hướng tiếp theo
  if close > ema20 and rsi >= 50 and macd > macd_sig:
    scenario = (
        "Ưu tiên chiến lược canh mua theo xu hướng khi giá kiểm tra lại vùng hỗ"
        f" trợ {support:.2f} USD, mục tiêu hướng tới {resistance:.2f} USD."
    )
  elif close < ema20 and rsi < 50:
    scenario = (
        f"Ưu tiên quan sát hoặc canh bán ngắn hạn nếu giá hồi phục lên vùng cản"
        f" {ema20:.2f} USD nhưng không vượt được."
    )
  else:
    scenario = (
        f"Giá đang dao động tích lũy trong biên độ {support:.2f} -"
        f" {resistance:.2f} USD, khuyến nghị chờ tín hiệu phá vỡ rõ ràng."
    )

  return (
      f"• {trend}\n• {momentum}\n• {sr}\n• {scenario}"
  )


def generate_ai_analysis(data):
  if not GEMINI_API_KEY:
    return build_fallback_analysis(data)

  prompt = f"""
Bạn là chuyên gia phân tích kỹ thuật thị trường vàng (XAUUSD). Hãy viết nhận định ngắn gọn dựa trên các thông số nến vừa đóng khung {data['timeframe']}:
- Giá đóng cửa: {data['close']:.2f} (Biên độ: {data['low']:.2f} - {data['high']:.2f})
- RSI (14): {data['rsi']:.2f}
- MACD: {data['macd']:.2f} (Signal: {data['macd_signal']:.2f})
- Đường xu hướng: EMA20={data['ema20']:.2f}, EMA50={data['ema50']:.2f}, EMA200={data['ema200']:.2f}
- Dải Bollinger: {data['bb_lower']:.2f} - {data['bb_upper']:.2f}
- Tín hiệu tổng quan: {data['summary']}

Trình bày đúng 4 gạch đầu dòng ngắn gọn:
1. Đánh giá cấu trúc xu hướng và vị thế giá so với các đường EMA.
2. Trạng thái động lượng (RSI/MACD: quá mua, quá bán hay đang tích lũy).
3. Vùng hỗ trợ và kháng cự gần nhất.
4. Kịch bản xu hướng tiếp theo (Khuyến nghị ưu tiên mua, bán hay quan sát).
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

  # Nếu Google AI quá tải, lập tức kích hoạt bộ phân tích kỹ thuật thông minh
  return build_fallback_analysis(data)


def send_telegram(data, ai_opinion):
  time_now = datetime.now().strftime("%d/%m/%Y %H:%M")
  safe_opinion = html.escape(ai_opinion)

  message_html = f"""📊 <b>BÁO CÁO GIÁ VÀNG XAU/USD ({data['timeframe']})</b>
⏱ <b>Thời điểm đóng nến:</b> {time_now}

💰 <b>Chỉ số giá:</b>
• Giá đóng cửa: <code>{data['close']:.2f} USD</code> ({data['change']:+.2f}%)
• Biên độ nến: <code>{data['low']:.2f}</code> – <code>{data['high']:.2f}</code>

📈 <b>Chỉ báo kỹ thuật:</b>
• RSI (14): <code>{data['rsi']:.2f}</code>
• MACD: <code>{data['macd']:.2f}</code> | Signal: <code>{data['macd_signal']:.2f}</code>
• EMA: EMA20: <code>{data['ema20']:.2f}</code> | EMA50: <code>{data['ema50']:.2f}</code> | EMA200: <code>{data['ema200']:.2f}</code>
• Tín hiệu kỹ thuật: <b>{data['summary']}</b>

🧠 <b>Nhận định & Phân tích chuyên sâu:</b>
{safe_opinion}
"""

  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message_html,
      "parse_mode": "HTML",
  }
  res = requests.post(url, json=payload, timeout=15)

  if res.status_code != 200:
    plain_text = f"""BÁO CÁO GIÁ VÀNG XAU/USD ({data['timeframe']}) - {time_now}
- Giá đóng cửa: {data['close']:.2f} USD ({data['change']:+.2f}%)
- Biên độ nến: {data['low']:.2f} - {data['high']:.2f}
- RSI: {data['rsi']:.2f} | MACD: {data['macd']:.2f}
- EMA20: {data['ema20']:.2f} | EMA50: {data['ema50']:.2f} | EMA200: {data['ema200']:.2f}
- Tín hiệu: {data['summary']}

Nhận định chuyên sâu:
{ai_opinion}"""
    requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": plain_text},
        timeout=15,
    )


if __name__ == "__main__":
  tf = sys.argv[1] if len(sys.argv) > 1 else "1H"
  market_data = fetch_gold_data(tf)
  analysis_text = generate_ai_analysis(market_data)
  send_telegram(market_data, analysis_text)
