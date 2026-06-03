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

import sqlite3

DATABASE_PATH = os.environ.get("DATABASE_PATH", "database.db")

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    logger.info("Initializing database...")
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Rate limit table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS rate_limits (
                    user_id TEXT,
                    timestamp REAL
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_rate_limits_user_timestamp ON rate_limits(user_id, timestamp)')
            
            # Response cache table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS response_cache (
                    user_id TEXT PRIMARY KEY,
                    full_answer TEXT,
                    updated_at REAL
                )
            ''')
            
            # Chat history table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    role TEXT,
                    message TEXT,
                    timestamp REAL
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_history_user_timestamp ON chat_history(user_id, timestamp)')
            conn.commit()
            logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

# Call init_db immediately
init_db()

# Caching variables for Obsidian knowledge
obsidian_knowledge_cache = ""
obsidian_cache_last_loaded = 0.0
OBSIDIAN_CACHE_TTL = 300.0 # 5 minutes in seconds

def load_obsidian_knowledge(vault_path):
    global obsidian_knowledge_cache, obsidian_cache_last_loaded
    now = time.time()
    
    # Return cache if TTL has not expired
    if obsidian_knowledge_cache and (now - obsidian_cache_last_loaded < OBSIDIAN_CACHE_TTL):
        return obsidian_knowledge_cache
        
    if not vault_path or not os.path.exists(vault_path):
        logger.warning(f"Obsidian vault path does not exist: {vault_path}")
        return ""
        
    logger.info(f"Loading Obsidian knowledge from {vault_path}...")
    knowledge_text = []
    
    try:
        for root, dirs, files in os.walk(vault_path):
            # Skip hidden directories like .obsidian, .git
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for file in files:
                if file.endswith('.md'):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        rel_path = os.path.relpath(file_path, vault_path)
                        # Format in XML tags for structured LLM parsing
                        knowledge_text.append(f'<document source="{rel_path}">\n{content}\n</document>')
                    except Exception as e:
                        logger.error(f"Error reading file {file_path}: {e}")
                        
        obsidian_knowledge_cache = "\n\n".join(knowledge_text)
        obsidian_cache_last_loaded = now
        logger.info(f"Successfully loaded {len(knowledge_text)} files from Obsidian vault.")
    except Exception as e:
        logger.error(f"Error walking vault path {vault_path}: {e}")
        
    return obsidian_knowledge_cache

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
    one_day_ago = now - 86400
    one_minute_ago = now - 60
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Clean up old timestamps older than 24 hours to keep table small
            cursor.execute('DELETE FROM rate_limits WHERE timestamp < ?', (one_day_ago,))
            
            # Check daily limit
            cursor.execute('SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND timestamp >= ?', (user_id, one_day_ago))
            day_count = cursor.fetchone()[0]
            if day_count >= RATE_LIMIT_PER_DAY:
                return True, "ขออภัยครับ คุณใช้งานครบกำหนดสูงสุดต่อวัน (50 ครั้ง/วัน) แล้ว กรุณาลองใหม่ในวันพรุ่งนี้ครับ 🙏"
                
            # Check minute limit (60 seconds)
            cursor.execute('SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND timestamp >= ?', (user_id, one_minute_ago))
            minute_count = cursor.fetchone()[0]
            if minute_count >= RATE_LIMIT_PER_MINUTE:
                return True, "คุณส่งข้อความเร็วเกินไป กรุณาเว้นช่วงสักครู่แล้วค่อยลองใหม่อีกครั้งครับ ⏱️"
                
            # Add current timestamp
            cursor.execute('INSERT INTO rate_limits (user_id, timestamp) VALUES (?, ?)', (user_id, now))
            conn.commit()
    except Exception as e:
        logger.error(f"Database error in is_rate_limited: {e}")
        # Fail-open: if DB fails, allow request but log error
        
    return False, ""

def cache_full_response(user_id, full_answer):
    if not user_id:
        return
    now = time.time()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO response_cache (user_id, full_answer, updated_at)
                VALUES (?, ?, ?)
            ''', (user_id, full_answer, now))
            # Clean up expired responses older than 2 hours to keep database size small
            cursor.execute('DELETE FROM response_cache WHERE updated_at < ?', (now - 7200,))
            conn.commit()
    except Exception as e:
        logger.error(f"Database error in cache_full_response: {e}")

def get_cached_response(user_id):
    if not user_id:
        return None
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT full_answer FROM response_cache WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if row:
                return row[0]
    except Exception as e:
        logger.error(f"Database error in get_cached_response: {e}")
    return None

def add_chat_turn(user_id, role, text):
    if not user_id or not text:
        return
    now = time.time()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO chat_history (user_id, role, message, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (user_id, role, text, now))
            
            # Keep history to maximum of 10 messages per user to save storage
            cursor.execute('''
                DELETE FROM chat_history 
                WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM chat_history 
                    WHERE user_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 10
                )
            ''', (user_id, user_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Database error in add_chat_turn: {e}")

def get_chat_history_for_gemini(user_id, limit=4):
    """
    Returns a list of types.Content objects representing the conversation history
    for the given user_id, in chronological order.
    """
    history_contents = []
    if not user_id:
        return history_contents
        
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT role, message FROM chat_history 
                WHERE user_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (user_id, limit))
            rows = cursor.fetchall()
            
            # Since we fetched DESC, reverse to make it chronological (ASC)
            rows.reverse()
            
            for row in rows:
                role = row[0]
                message = row[1]
                history_contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=message)]
                    )
                )
    except Exception as e:
        logger.error(f"Database error in get_chat_history_for_gemini: {e}")
        
    return history_contents

def clear_chat_history(user_id):
    if not user_id:
        return
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM chat_history WHERE user_id = ?', (user_id,))
            conn.commit()
            logger.info(f"Cleared chat history for user: {user_id}")
    except Exception as e:
        logger.error(f"Database error in clear_chat_history: {e}")

SYSTEM_PROMPT = """คุณคือ "นายดี" ผู้ช่วยด้านกฎหมายไทย
เชี่ยวชาญเรื่องสิทธิ์ของประชาชนไทยทั่วไป
ตอบเป็นภาษาไทยเข้าใจง่าย บอก action plan ชัดเจนทีละขั้น
ทุกคำตอบต้องจบด้วย disclaimer สั้นๆ ว่าเป็นข้อมูลเบื้องต้น ไม่ใช่คำแนะนำทางกฎหมายอย่างเป็นทางการ

กฎเหล็กในการอ้างอิงแหล่งข้อมูล (sources):
1. ห้ามสร้าง/ห้ามเดาลิงก์ลึก (Deep Link) ที่มีเครื่องหมายทับตัวที่สองต่อจากชื่อโดเมนหลัก (เช่น ห้ามใช้ลิงก์ที่ลงท้ายด้วย .pdf หรือมีโฟลเดอร์ย่อยเด็ดขาด) เพื่อป้องกันลิงก์เสีย 404
2. ให้ใช้เฉพาะ URL หน้าแรกของหน่วยงานที่ถูกต้องจากรายการคู่มือต่อไปนี้เท่านั้น:
   - สำนักงานคณะกรรมการกฤษฎีกา -> https://www.krisdika.go.th
   - กระทรวงแรงงาน -> https://www.mol.go.th
   - กรมสวัสดิการและคุ้มครองแรงงาน -> https://www.labour.go.th
   - สำนักงานคณะกรรมการคุ้มครองผู้บริโภค (สคบ.) -> https://www.ocpb.go.th
   - กรมบังคับคดี -> https://www.led.go.th
   - กระทรวงยุติธรรม -> https://www.moj.go.th
   - สำนักงานตำรวจแห่งชาติ -> https://www.police.go.th
3. ห้ามใช้ URL นอกเหนือจากที่ระบุในรายการคู่มือนี้เด็ดขาด หากเรื่องใดไม่มีในคู่มือนี้ ให้ละการใส่ลิงก์ URL (ให้เว้นว่างหรือใช้ลิงก์กฤษฎีกาแทน)"""

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
            [TextSendMessage(
                text=WELCOME_MESSAGE,
                quick_reply=QUICK_REPLIES
            )]
        )
    except Exception as e:
        logger.error(f"Line Reply Follow Error: {e}")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    user_id = event.source.user_id
    
    # Welcome message เมื่อพิมพ์ครั้งแรกหรือ greeting
    greetings = ["สวัสดี", "หวัดดี", "hello", "hi", "เริ่ม", "start"]
    if any(g in user_message.lower() for g in greetings):
        clear_chat_history(user_id)
        try:
            line_bot_api.reply_message(
                event.reply_token,
                [TextSendMessage(
                    text=WELCOME_MESSAGE,
                    quick_reply=QUICK_REPLIES
                )]
            )
        except Exception as e:
            logger.error(f"Line Reply Welcome Error: {e}")
        return

    # Check rate limits per user to prevent API spam and save token billing
    limited, limit_message = is_rate_limited(user_id)
    if limited:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                [TextSendMessage(text=limit_message)]
            )
        except Exception as e:
            logger.error(f"Line Reply Rate Limit Error: {e}")
        return

    try:
        # Load Obsidian knowledge dynamically
        obsidian_vault_path = os.environ.get("OBSIDIAN_VAULT_PATH", r"c:\llm wiki\gemini second brain")
        knowledge_base = load_obsidian_knowledge(obsidian_vault_path)
        
        # Combine system instruction with knowledge base
        full_system_prompt = SYSTEM_PROMPT
        if knowledge_base:
            full_system_prompt += f"\n\nนี่คือฐานข้อมูลอ้างอิงความรู้และกฎหมายเพิ่มเติมในระบบของคุณ (กรุณาใช้ข้อมูลและระบุแหล่งอ้างอิงเอกสารเหล่านี้เป็นหลักในการตอบผู้ใช้):\n{knowledge_base}"
            
        # Get chat history (up to last 4 turns)
        history = get_chat_history_for_gemini(user_id, limit=4)
        # Construct Gemini API contents
        gemini_contents = history + [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_message)]
            )
        ]
        
        max_tokens = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", 1500))
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=gemini_contents,
            config=types.GenerateContentConfig(
                system_instruction=full_system_prompt,
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
            logger.warning(f"Failed to parse Gemini JSON: {json_err}. Falling back to regex extraction.")
            # Use regex to extract summary and full answer from truncated or malformed JSON
            import re
            summary_match = re.search(r'"summary"\s*:\s*"(.*?)"', response.text, re.DOTALL)
            full_match = re.search(r'"full"\s*:\s*"(.*?)"', response.text, re.DOTALL)
            
            if summary_match or full_match:
                try:
                    summary = summary_match.group(1).encode().decode('unicode-escape', errors='ignore') if summary_match else ""
                except Exception:
                    summary = summary_match.group(1) if summary_match else ""
                    
                try:
                    full = full_match.group(1).encode().decode('unicode-escape', errors='ignore') if full_match else ""
                except Exception:
                    full = full_match.group(1) if full_match else ""
                
                # Replace escaped newlines and double quotes
                summary = summary.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                full = full.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
            else:
                summary = ""
                full = ""

            if not summary or not full:
                # If regex fails completely, fall back to raw text stripping JSON structure
                full = response.text
                summary = (full[:400] + "...\n\n(นี่คือคำตอบย่อ กรุณากดปุ่มด้านล่างเพื่อดูคำตอบเต็ม)") if len(full) > 400 else full

        # Cache the full response in database
        cache_full_response(user_id, full)
        
        # Save user query and assistant response to chat history
        add_chat_turn(user_id, "user", user_message)
        add_chat_turn(user_id, "model", summary)

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
                [TextSendMessage(
                    text="ขออภัยครับ ขณะนี้ระบบประมวลผลขัดข้อง กรุณาลองใหม่อีกครั้งในภายหลัง",
                    quick_reply=QUICK_REPLIES
                )]
            )
        except Exception as e_reply:
            logger.error(f"Failed to reply error message: {e_reply}")

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    
    if data == "action=show_full":
        full_answer = get_cached_response(user_id)
        if full_answer:
            send_split_messages(event.reply_token, full_answer, QUICK_REPLIES)
        else:
            try:
                line_bot_api.reply_message(
                    event.reply_token,
                    [TextSendMessage(
                        text="ขออภัยครับ ข้อมูลคำตอบหมดอายุแล้ว กรุณาส่งคำถามใหม่อีกครั้งครับ 🙏",
                        quick_reply=QUICK_REPLIES
                    )]
                )
            except Exception as e:
                logger.error(f"Line Reply Expired Postback Error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)