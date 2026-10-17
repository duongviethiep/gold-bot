import os
import sys
from datetime import datetime
import requests
from tradingview_ta import Interval, TA_Handler

# Lấy thông tin bảo mật từ cấu hình GitHub Secrets
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

SYMBOL = "XAUUSD"
EXCHANGE = "PEPPERSTONE"
SCREENER = "forex"


def fetch_gold_data(timeframe_str):
  interval_map = {
      "1H": Interval.INTERVAL_1_HOUR,
      "4H": Interval.INTERVAL_4_HOURS,
  }

  handler = TA_Handler(
      symbol=SYMBOL,
      exchange=EXCHANGE,
      screener=SCREENER,
      interval=interval_map.get(timeframe_str, Interval.INTERVAL_1_HOUR),
  )
  analysis = handler.get_analysis()
  ind = analysis.indicators

  return {
      "timeframe": timeframe_str,
      "close": ind.get("close"),
      "open": ind.get("open"),
      "high": ind.get("high"),
      "low": ind.get("low"),
      "change": ind.get("change"),
      "volume": ind.get("volume"),
      "rsi": ind.get("RSI"),
      "macd": ind.get("MACD.macd"),
      "macd_signal": ind.get("MACD.signal"),
      "ema20": ind.get("EMA20"),
      "ema50": ind.get("EMA50"),
      "ema200": ind.get("EMA200"),
      "bb_upper": ind.get("BB.upper"),
      "bb_lower": ind.get("BB.lower"),
      "summary": analysis.summary.get("RECOMMENDATION", "NEUTRAL"),
  }


def generate_ai_analysis(data):
  prompt = f"""
Bạn là chuyên gia phân tích kỹ thuật thị trường vàng (XAUUSD). Hãy viết nhận định ngắn gọn, sắc bén dựa trên các thông số nến vừa đóng khung {data['timeframe']}:
- Giá đóng cửa: {data['close']:.2f} (Biên độ: {data['low']:.2f} - {data['high']:.2f})
- RSI (14): {data['rsi']:.2f}
- MACD: {data['macd']:.2f} (Signal: {data['macd_signal']:.2f})
- Đường xu hướng: EMA20={data['ema20']:.2f}, EMA50={data['ema50']:.2f}, EMA200={data['ema200']:.2f}
- Dải Bollinger: {data['bb_lower']:.2f} - {data['bb_upper']:.2f}
- Tín hiệu tổng quan: {data['summary']}

Trình bày đúng 4 gạch đầu dòng súc tích:
1. Đánh giá cấu trúc xu hướng và vị thế giá so với các đường EMA.
2. Trạng thái động lượng (RSI/MACD: quá mua, quá bán hay đang hội tụ/phân kỳ).
3. Vùng hỗ trợ và kháng cự gần nhất cần theo dõi.
4. Kịch bản xu hướng tiếp theo (Khuyến nghị ưu tiên mua, bán hay đứng ngoài quan sát).
Không dùng lời chào hỏi, đi thẳng vào các gạch đầu dòng.
"""

  url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
  headers = {"Content-Type": "application/json"}
  payload = {"contents": [{"parts": [{"text": prompt}]}]}

  try:
    response = requests.post(url, json=payload, headers=headers, timeout=20)
    res_json = response.json()
    return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
  except Exception as e:
    return f"Chưa tạo được nhận định tự động: {e}"


def send_telegram(data, ai_opinion):
  time_now = datetime.now().strftime("%d/%m/%Y %H:%M")

  message = f"""📊 <b>BÁO CÁO GIÁ VÀNG XAU/USD ({data['timeframe']})</b>
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
{ai_opinion}
"""

  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "HTML",
  }
  requests.post(url, json=payload, timeout=10)


if __name__ == "__main__":
  tf = sys.argv[1] if len(sys.argv) > 1 else "1H"
  market_data = fetch_gold_data(tf)
  analysis_text = generate_ai_analysis(market_data)
  send_telegram(market_data, analysis_text)
