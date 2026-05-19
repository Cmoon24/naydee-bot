from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ตรวจสอบว่ามีค่าใน Environment Variables หรือไม่
line_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
line_secret = os.environ.get("LINE_CHANNEL_SECRET")
claude_api_key = os.environ.get("ANTHROPIC_API_KEY")

if not all([line_access_token, line_secret, claude_api_key]):
    print("Error: Missing environment variables. Please check your .env file.")
    exit(1)

line_bot_api = LineBotApi(line_access_token)
handler = WebhookHandler(line_secret)
claude = anthropic.Anthropic(api_key=claude_api_key)

SYSTEM_PROMPT = """คุณคือ "นายดี" ผู้ช่วยด้านกฎหมายไทย
เชี่ยวชาญเรื่องการถูกทวงหนี้ผิดกฎหมาย
ตอบเป็นภาษาไทยเข้าใจง่าย บอก action plan ชัดเจน
ทุกคำตอบต้องจบด้วย disclaimer ว่าเป็นข้อมูลเบื้องต้น
ไม่ใช่คำแนะนำทางกฎหมายอย่างเป็นทางการ"""

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        print("X-Line-Signature is missing.")
        abort(400)
        
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Check your channel access token/channel secret.")
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    
    try:
        response = claude.messages.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        reply = response.content[0].text
    except Exception as e:
        print(f"Anthropic API Error: {str(e)}")
        reply = "ขออภัยครับ ขณะนี้ระบบประมวลผลขัดข้อง กรุณาลองใหม่อีกครั้งในภายหลัง"

    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )
    except Exception as e:
        print(f"Line Reply Error: {str(e)}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)