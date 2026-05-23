from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from google import genai
from google.genai import types
import os
import logging
from dotenv import load_dotenv

# ตั้งค่า Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
app = Flask(__name__)

# ตรวจสอบว่ามีค่าใน Environment Variables หรือไม่
line_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
line_secret = os.environ.get("LINE_CHANNEL_SECRET")
gemini_api_key = os.environ.get("GEMINI_API_KEY")

if not all([line_access_token, line_secret, gemini_api_key]):
    logger.error("Missing environment variables. Please check your .env file.")
    exit(1)

line_bot_api = LineBotApi(line_access_token)
handler = WebhookHandler(line_secret)
gemini_client = genai.Client(api_key=gemini_api_key)

SYSTEM_PROMPT = """คุณคือ "นายดี" ผู้ช่วยด้านกฎหมายไทย
เชี่ยวชาญเรื่องการถูกทวงหนี้ผิดกฎหมาย
ตอบเป็นภาษาไทยเข้าใจง่าย บอก action plan ชัดเจน
ทุกคำตอบต้องจบด้วย disclaimer ว่าเป็นข้อมูลเบื้องต้น
ไม่ใช่คำแนะนำทางกฎหมายอย่างเป็นทางการ"""

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    if not signature:
        logger.warning("X-Line-Signature is missing.")
        abort(400)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Check your channel access token/channel secret.")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    
    try:
        max_tokens = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", 8192))
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=max_tokens,
            ),
        )
        reply = response.text
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        reply = "ขออภัยครับ ขณะนี้ระบบประมวลผลขัดข้อง กรุณาลองใหม่อีกครั้งในภายหลัง"

    try:
        # LINE limits text messages to 5000 characters. Split the reply into chunks of 4900 characters.
        limit = 4900
        messages = [TextSendMessage(text=reply[i:i+limit]) for i in range(0, len(reply), limit)]
        
        # LINE only allows up to 5 messages per reply token
        line_bot_api.reply_message(
            event.reply_token,
            messages[:5]
        )
    except Exception as e:
        logger.error(f"Line Reply Error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)