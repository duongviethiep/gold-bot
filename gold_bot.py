import os
import sys
from datetime import datetime
import requests
from tradingview_ta import Interval, TA_Handler

# Lấy thông tin từ GitHub Secrets
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


def fetch_gold_data(timeframe_str):
  interval_map = {
      "1H": Interval.INTERVAL_1_HOUR,
      "4H": Interval.INTERVAL_4_HOURS,
  }
  selected_interval = interval_map.get(timeframe_str, Interval.INTERVAL_1_HOUR)

  # Danh sách cấu hình sàn vàng trên TradingView (ưu tiên CFD)
  candidates = [
      {"symbol": "XAUUSD", "exchange": "PEPPERSTONE", "screener": "cfd"},
      {"symbol": "XAUUSD", "exchange": "OANDA", "screener": "cfd"},
      {"symbol": "XAUUSD", "exchange": "FOREXCOM", "screener": "cfd"},
  ]

  analysis = None
  for c in candidates:
    try:
      print(f"Đang kết nối sàn {c['exchange']} ({c['screener']})...")
      handler = TA_Handler(
          symbol=c["symbol"],
          exchange=c["exchange"],
          screener=c["screener"],
          interval=selected_interval,
      )
      analysis = handler.get_analysis()
      if analysis and analysis.indicators:
        print(f"-> Lấy dữ liệu thành công từ {c['exchange']}!")
        break
    except Exception as err:
      print(f"-> Thử sàn {c['exchange']} chưa được: {err}")
      continue

  if not analysis or not analysis.indicators:
    raise Exception(
        "Không thể lấy được chỉ số XAUUSD từ các sàn trên TradingView."
    )

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


def generate_ai_analysis(data):
  if not GEMINI_API_KEY:
    return (
        "Chưa thiết lập GEMINI_API_KEY trong mục Secrets nên tạm thời bỏ qua"
        " nhận định AI."
    )

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

  url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
  headers = {"Content-Type": "application/json"}
  payload = {"contents": [{"parts": [{"text": prompt}]}]}

  try:
    response = requests.post(url, json=payload, headers=headers, timeout=20)
    res_json = response.json()
    if "candidates" in res_json and len(res_json["candidates"]) > 0:
      return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
    return f"Lỗi từ Gemini: {res_json.get('error', {}).get('message', 'Không xác định')}"
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
  res = requests.post(url, json=payload, timeout=15)
  if res.status_code != 200:
    print(f"Lỗi gửi Telegram: {res.text}")
    raise Exception(f"Telegram trả lời lỗi: {res.text}")


if __name__ == "__main__":
  tf = sys.argv[1] if len(sys.argv) > 1 else "1H"
  print(f"=== BẮT ĐẦU CHẠY KHUNG {tf} ===")

  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print(
        "LỖI: Chưa có TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong GitHub"
        " Secrets!"
    )
    sys.exit(1)

  market_data = fetch_gold_data(tf)
  print(f"Giá đóng cửa: {market_data['close']} | RSI: {market_data['rsi']:.2f}")

  analysis_text = generate_ai_analysis(market_data)
  print("Đã tạo xong nhận định, đang gửi tin nhắn Telegram...")

  send_telegram(market_data, analysis_text)
  print("HOÀN TẤT: Đã gửi báo cáo thành công về Telegram!")
