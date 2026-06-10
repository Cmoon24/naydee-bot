import os
import sqlite3
import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load environment variables from .env
load_dotenv()

DATABASE_PATH = os.environ.get("DATABASE_PATH", "database.db")
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def get_db_connection():
    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import DictCursor
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode='require', cursor_factory=DictCursor)
        return conn
    else:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def main():
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY is not set in environment or .env file.")
        return

    print("Connecting to database...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if table exists (SQLite vs Postgres)
        if DATABASE_URL:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'feedbacks')")
            table_exists = cursor.fetchone()[0]
        else:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feedbacks'")
            table_exists = cursor.fetchone()
            
        if not table_exists:
            print("Table 'feedbacks' does not exist yet. Please run app.py or interact with the bot first.")
            conn.close()
            return

        cursor.execute("SELECT id, user_id, feedback_text, timestamp FROM feedbacks ORDER BY timestamp DESC")
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"Error querying database: {e}")
        return

    if not rows:
        print("No feedback found in the database. Add some feedback first!")
        return

    print(f"Found {len(rows)} feedback entry/entries. Formatting data...")
    formatted_feedbacks = []
    for row in rows:
        dt = datetime.datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        formatted_feedbacks.append(
            f"ID: {row['id']}\nUser: {row['user_id']}\nTime: {dt}\nFeedback: {row['feedback_text']}\n-------------------"
        )
    
    feedback_payload = "\n".join(formatted_feedbacks)

    print("Sending feedbacks to Gemini API for analysis...")
    client = genai.Client(api_key=GEMINI_API_KEY)

    system_instruction = """คุณคือผู้เชี่ยวชาญด้านการวิเคราะห์ Product Feedback ของบอท 'Moon' ซึ่งเป็นบอทช่วยเหลือประชาชนเรื่องข้อกฎหมายไทย
หน้าที่ของคุณคือ:
1. วิเคราะห์และจัดกลุ่มฟีดแบ็กที่มีเนื้อหาคล้ายคลึงกัน (Feedback Clustering)
2. นับความถี่ของแต่ละประเด็น (Frequency Count)
3. วิเคราะห์หาประเด็นปัญหาที่แท้จริงและระดับผลกระทบต่อผู้ใช้งาน (Impact & Urgency Analysis)
4. เสนอแนะ Action Plan แผนการปรับปรุงระบบทีละขั้นอย่างละเอียดและเป็นรูปธรรม
5. รายงานผลลัพธ์เป็นภาษาไทยที่สุภาพ เข้าใจง่าย ในรูปแบบ Markdown ที่สวยงาม

รูปแบบรายงาน Markdown ที่ต้องการ:
# รายงานผลการวิเคราะห์และแผนการปรับปรุงระบบ (Feedback Analysis & Action Plan Report)
(ข้อมูล ณ วันที่ [ระบุวันที่])

## 📊 บทสรุปภาพรวม (Overview Executive Summary)
[สรุปสั้นๆ ว่าพบประเด็นใดมากที่สุด และระดับความเร่งด่วนโดยรวม]

## 🔍 ประเด็นปัญหาและข้อเสนอแนะที่พบ (Key Findings)
จัดกลุ่มแยกเป็นข้อๆ เรียงลำดับจากความเร่งด่วน/ความถี่สูงไปต่ำ:

### [หัวข้อประเด็นปัญหา]
- **จำนวนครั้งที่พบ (Frequency):** [จำนวนครั้ง]
- **ระดับผลกระทบ (Impact level):** [High/Medium/Low] พร้อมคำอธิบายสั้นๆ ว่ามีผลต่อระบบจริงหรือไม่
- **รายละเอียดสิ่งที่ผู้ใช้แจ้ง:** [สรุปสิ่งที่ผู้ใช้ติชม/บ่น]
- **แผนการปรับปรุง (Action Plan):** [เสนอวิธีแก้ปัญหาอย่างเป็นระบบทีละขั้นตอน]

## 💡 สิ่งที่ผู้ใช้ชื่นชอบ (Positive Feedbacks)
- [รวบรวมคำชมหรือจุดแข็งของระบบ]
"""

    prompt = f"นี่คือข้อมูลฟีดแบ็กจากผู้ใช้งานบอท 'Moon' โปรดวิเคราะห์และสรุปตามแนวทางที่กำหนด:\n\n{feedback_payload}"

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=3000,
            ),
        )
        report_content = response.text
        print("\n--- Analysis Report Generated ---")
        try:
            print(report_content)
        except UnicodeEncodeError:
            try:
                import sys
                sys.stdout.buffer.write(report_content.encode(sys.stdout.encoding or 'utf-8', errors='replace'))
                print()
            except Exception:
                print(report_content.encode('ascii', errors='replace').decode('ascii'))
        
        # Save to output file
        artifact_dir = r"C:\Users\C\.gemini\antigravity-ide\brain\f0e5e3b7-952f-4457-8697-0ed64953bdc6"
        output_paths = [
            "feedback_analysis_report.md",  # current directory
            os.path.join(artifact_dir, "feedback_analysis_report.md")
        ]
        
        for path in output_paths:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(report_content)
                print(f"Successfully saved report to: {path}")
            except Exception as save_err:
                print(f"Could not save to {path}: {save_err}")
                
    except Exception as e:
        print(f"Gemini API Error: {e}")

if __name__ == "__main__":
    main()
