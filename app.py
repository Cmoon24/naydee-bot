from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction,
    FollowEvent
)
from linebot.exceptions import InvalidSignatureError
from google import genai
from google.genai import types
import os
import logging
import time
from collections import defaultdict
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

# In-memory storage to track user request timestamps: {user_id: [timestamp1, timestamp2, ...]}
user_request_timestamps = defaultdict(list)

# Limits to save tokens and prevent billing abuse
RATE_LIMIT_PER_MINUTE = 5
RATE_LIMIT_PER_DAY = 50

def is_rate_limited(user_id):
    if not user_id:
        return False, ""
        
    now = time.time()
    timestamps = user_request_timestamps[user_id]
    
    # Filter out timestamps older than 24 hours (86400 seconds)
    timestamps = [t for t in timestamps if now - t < 86400]
    user_request_timestamps[user_id] = timestamps
    
    # Check daily limit
    if len(timestamps) >= RATE_LIMIT_PER_DAY:
        return True, "ขออภัยครับ คุณใช้งานครบกำหนดสูงสุดต่อวัน (50 ครั้ง/วัน) แล้ว กรุณาลองใหม่ในวันพรุ่งนี้ครับ 🙏"
        
    # Check minute limit (60 seconds)
    recent_minute = [t for t in timestamps if now - t < 60]
    if len(recent_minute) >= RATE_LIMIT_PER_MINUTE:
        return True, "คุณส่งข้อความเร็วเกินไป กรุณาเว้นช่วงสักครู่แล้วค่อยลองใหม่อีกครั้งครับ ⏱️"
        
    # Add current timestamp
    user_request_timestamps[user_id].append(now)
    return False, ""

SYSTEM_PROMPT = """คุณคือ "นายดี" ผู้ช่วยด้านกฎหมายไทย
เชี่ยวชาญเรื่องสิทธิ์ของประชาชนไทยทั่วไป
ตอบเป็นภาษาไทยเข้าใจง่าย บอก action plan ชัดเจนทีละขั้น
ทุกคำตอบต้องจบด้วย disclaimer สั้นๆ ว่าเป็นข้อมูลเบื้องต้น ไม่ใช่คำแนะนำทางกฎหมายอย่างเป็นทางการ"""

# Quick reply จากข้อมูล Facebook ที่พบบ่อยที่สุด
QUICK_REPLIES = QuickReply(items=[
    QuickReplyButton(action=MessageAction(
        label="🦈 ถูกทวงหนี้ผิดกฎหมาย",
        text="ถูกทวงหนี้ผิดกฎหมาย โทรมาขู่ทำให้อับอาย ผมมีสิทธิ์ทำอะไรได้บ้าง?"
    )),
    QuickReplyButton(action=MessageAction(
        label="💸 ไม่ได้รับเงินที่ตกลงไว้",
        text="ตกลงซื้อขายกันแล้วแต่อีกฝ่ายไม่จ่ายเงิน หรือไม่ส่งของตามสัญญา ทำยังไงได้บ้าง?"
    )),
    QuickReplyButton(action=MessageAction(
        label="🏠 ปัญหามรดกและที่ดิน",
        text="มีปัญหาเรื่องมรดกหรือที่ดิน ผู้จัดการมรดกไม่แบ่งให้ยุติธรรม ทำยังไงได้บ้าง?"
    )),
    QuickReplyButton(action=MessageAction(
        label="⚖️ ถามเรื่องอื่น",
        text="อยากถามเรื่องกฎหมายอื่นๆ"
    )),
])

WELCOME_MESSAGE = """สวัสดีครับ! ผมนายดี 👋
ผู้ช่วยด้านกฎหมายไทยที่พูดภาษาคนธรรมดา

พิมพ์ปัญหาของคุณได้เลย หรือเลือกหัวข้อที่เจอบ่อยด้านล่างครับ 👇"""

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

@handler.add(FollowEvent)
def handle_follow(event):
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=WELCOME_MESSAGE,
                quick_reply=QUICK_REPLIES
            )
        )
    except Exception as e:
        logger.error(f"Line Reply Follow Error: {e}")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    
    # Welcome message เมื่อพิมพ์ครั้งแรกหรือ greeting
    greetings = ["สวัสดี", "หวัดดี", "hello", "hi", "เริ่ม", "start"]
    if any(g in user_message.lower() for g in greetings):
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=WELCOME_MESSAGE,
                    quick_reply=QUICK_REPLIES
                )
            )
        except Exception as e:
            logger.error(f"Line Reply Welcome Error: {e}")
        return

    # Check rate limits per user to prevent API spam and save token billing
    user_id = event.source.user_id
    limited, limit_message = is_rate_limited(user_id)
    if limited:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=limit_message)
            )
        except Exception as e:
            logger.error(f"Line Reply Rate Limit Error: {e}")
        return

    try:
        max_tokens = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", 1500))
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
        chunks = [reply[i:i+limit] for i in range(0, len(reply), limit)][:5]
        messages = []
        for idx, chunk in enumerate(chunks):
            if idx == len(chunks) - 1:
                messages.append(TextSendMessage(text=chunk, quick_reply=QUICK_REPLIES))
            else:
                messages.append(TextSendMessage(text=chunk))
        
        line_bot_api.reply_message(
            event.reply_token,
            messages
        )
    except Exception as e:
        logger.error(f"Line Reply Error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)