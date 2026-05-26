from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction,
    FollowEvent, PostbackEvent, PostbackAction
)
from linebot.exceptions import InvalidSignatureError
from google import genai
from google.genai import types
import os
import logging
import time
import json
from collections import defaultdict
from dotenv import load_dotenv
from pydantic import BaseModel, Field

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
# In-memory storage to cache the last full answer for each user: {user_id: full_answer}
user_last_response = {}

# Limits to save tokens and prevent billing abuse
RATE_LIMIT_PER_MINUTE = 5
RATE_LIMIT_PER_DAY = 50

class SourceItem(BaseModel):
    title: str = Field(description="ชื่อกฎหมาย มาตรา หรือชื่อหน่วยงานรัฐบาลที่เป็นแหล่งอ้างอิง เช่น พระราชบัญญัติการทวงถามหนี้ พ.ศ. 2558, เว็บไซต์กรมบังคับคดี")
    url: str = Field(description="ลิงก์ URL อ้างอิงตรงที่ถูกต้องและเข้าใช้งานได้จริง (ต้องเป็นเว็บของรัฐบาล เช่น .go.th หรือแหล่งข้อมูลกฎหมายของทางการ เช่น krisdika.go.th, led.go.th เท่านั้น)")

# Schema for Gemini structured JSON response
class LegalResponse(BaseModel):
    summary: str = Field(description="สรุปคำตอบแบบย่อสั้นๆ กระชับ เข้าใจง่าย บอก action plan ชัดเจนทีละขั้น มี disclaimer ท้ายคำตอบ (ยังไม่ต้องแนบรายการลิงก์อ้างอิงท้ายข้อความนี้ เดี๋ยวระบบจะดึงไปต่อท้ายเอง)")
    full: str = Field(description="รายละเอียดคำตอบแบบเต็ม ครบถ้วนตามข้อกฎหมาย มีขั้นตอนการดำเนินการ และ disclaimer ท้ายคำตอบ (ยังไม่ต้องแนบรายการลิงก์อ้างอิงท้ายข้อความนี้ เดี๋ยวระบบจะดึงไปต่อท้ายเอง)")
    sources: list[SourceItem] = Field(default=[], description="รายการแหล่งอ้างอิงทางกฎหมายหรือหน่วยงานรัฐที่เกี่ยวข้อง (จำกัดไม่เกิน 3 แหล่งอ้างอิง)")

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
ทุกคำตอบต้องจบด้วย disclaimer สั้นๆ ว่าเป็นข้อมูลเบื้องต้น ไม่ใช่คำแนะนำทางกฎหมายอย่างเป็นทางการ

ในการอ้างอิงแหล่งข้อมูลทางกฎหมาย:
1. ทุกครั้งที่ให้คำแนะนำ ให้ระบุแหล่งอ้างอิงจากกฎหมายไทย เช่น พระราชบัญญัติ มาตรา หรือหน่วยงานของรัฐบาลลงในลิสต์ sources
2. สำหรับ URL อ้างอิง (url) ต้องใช้เว็บไซต์ของรัฐบาล (ลงท้ายด้วย .go.th เช่น krisdika.go.th, moj.go.th, consumer.go.th, led.go.th) หรือหน่วยงานทางกฎหมายที่เชื่อถือได้
3. ห้ามเดาหรือสร้างลิงก์ลึก (Deep Link) ที่สุ่มเสี่ยงจะเข้าใช้งานไม่ได้ (404) หากไม่ทราบลิงก์หน้าข้อมูลเจาะจง ให้ใช้ URL หน้าแรกของหน่วยงานที่รับผิดชอบโดยตรงแทน เช่น 'https://www.krisdika.go.th' หรือ 'https://www.led.go.th'"""

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

def send_split_messages(reply_token, text, quick_reply=None):
    """
    LINE limits text messages to 5000 characters. 
    Split the response into chunks of 4900 characters and send them.
    The quick reply is attached only to the last chunk.
    """
    try:
        limit = 4900
        chunks = [text[i:i+limit] for i in range(0, len(text), limit)][:5]
        messages = []
        for idx, chunk in enumerate(chunks):
            if idx == len(chunks) - 1 and quick_reply:
                messages.append(TextSendMessage(text=chunk, quick_reply=quick_reply))
            else:
                messages.append(TextSendMessage(text=chunk))
        
        line_bot_api.reply_message(reply_token, messages)
    except Exception as e:
        logger.error(f"Line Reply Error: {e}")

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
                response_mime_type="application/json",
                response_schema=LegalResponse,
            ),
        )
        
        try:
            response_json = json.loads(response.text)
            summary = response_json.get("summary", "")
            full = response_json.get("full", "")
            sources = response_json.get("sources", [])
            if not isinstance(sources, list):
                sources = []
            
            if not summary or not full:
                raise ValueError("JSON missing summary or full key")
            
            # Filter and format sources
            formatted_sources = []
            for item in sources:
                if isinstance(item, dict):
                    title = item.get("title", "")
                    url = item.get("url", "")
                else:
                    title = getattr(item, "title", "")
                    url = getattr(item, "url", "")
                
                if title and url:
                    # Clean up URL format
                    url = url.strip()
                    if not (url.startswith("http://") or url.startswith("https://")):
                        url = "https://" + url
                    formatted_sources.append(f"- {title}\n  🔗 {url}")
            
            if formatted_sources:
                sources_text = "\n\n🌐 แหล่งอ้างอิงของรัฐบาล/กฎหมาย:\n" + "\n".join(formatted_sources)
                summary += sources_text
                full += sources_text
                
        except (json.JSONDecodeError, ValueError) as json_err:
            logger.warning(f"Failed to parse Gemini JSON: {json_err}. Falling back to raw text.")
            full = response.text
            summary = (full[:400] + "...\n\n(นี่คือคำตอบย่อ กรุณากดปุ่มด้านล่างเพื่อดูคำตอบเต็ม)") if len(full) > 400 else full

        # Cache the full response
        user_last_response[user_id] = full

        # Create quick reply for the summary response
        reply_items = [
            QuickReplyButton(action=PostbackAction(
                label="📖 อ่านคำตอบแบบเต็ม",
                data="action=show_full"
            ))
        ]
        reply_items.extend(QUICK_REPLIES.items)
        summary_quick_reply = QuickReply(items=reply_items)

        send_split_messages(event.reply_token, summary, summary_quick_reply)

    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="ขออภัยครับ ขณะนี้ระบบประมวลผลขัดข้อง กรุณาลองใหม่อีกครั้งในภายหลัง",
                    quick_reply=QUICK_REPLIES
                )
            )
        except Exception as e_reply:
            logger.error(f"Failed to reply error message: {e_reply}")

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    
    if data == "action=show_full":
        full_answer = user_last_response.get(user_id)
        if full_answer:
            send_split_messages(event.reply_token, full_answer, QUICK_REPLIES)
        else:
            try:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="ขออภัยครับ ข้อมูลคำตอบหมดอายุแล้ว กรุณาส่งคำถามใหม่อีกครั้งครับ 🙏",
                        quick_reply=QUICK_REPLIES
                    )
                )
            except Exception as e:
                logger.error(f"Line Reply Expired Postback Error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)