import os
import datetime

DATABASE_PATH = os.environ.get("DATABASE_PATH", "database.db")
DATABASE_URL = os.environ.get("DATABASE_URL")

def print_summary():
    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import DictCursor
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode='require', cursor_factory=DictCursor)
        cursor = conn.cursor()
    else:
        if not os.path.exists(DATABASE_PATH):
            print(f"Error: Database file '{DATABASE_PATH}' not found. Please interact with the bot first to initialize it.")
            return
        import sqlite3
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
    print("=" * 60)
    print("📊 MOON-BOT DATABASE BACKEND STATUS")
    print("=" * 60)
    
    # 1. Show tables list
    if DATABASE_URL:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        tables = [t[0] for t in cursor.fetchall()]
    else:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [t[0] for t in cursor.fetchall()]
    print(f"Database Tables: {', '.join(tables)}")
    print("-" * 60)
    
    # 2. Check rate limits & tokens
    if "rate_limits" in tables:
        cursor.execute("SELECT COUNT(*), SUM(tokens) FROM rate_limits")
        count, tokens = cursor.fetchone()
        tokens = tokens or 0
        print(f"Rate Limits Log: {count} requests logged, total {tokens:,} tokens used.")
        
        # Top users by usage (hashed user ID)
        cursor.execute("SELECT user_id, COUNT(*), SUM(tokens) FROM rate_limits GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 5")
        top_users = cursor.fetchall()
        if top_users:
            print("\nTop 5 Active Users (Hashed ID):")
            for idx, (user, reqs, toks) in enumerate(top_users, 1):
                print(f"  {idx}. {user[:10]}... | Requests: {reqs} times | Tokens: {toks or 0:,}")
    print("-" * 60)
    
    # 3. Check Chat History
    if "chat_history" in tables:
        cursor.execute("SELECT COUNT(DISTINCT user_id), COUNT(*) FROM chat_history")
        users_count, msgs = cursor.fetchone()
        print(f"Chat History: {msgs} messages across {users_count} active users.")
    print("-" * 60)
    
    # 4. Check Feedbacks
    if "feedbacks" in tables:
        cursor.execute("SELECT COUNT(*) FROM feedbacks")
        feed_count = cursor.fetchone()[0]
        print(f"Feedbacks: {feed_count} entries received.")
        if feed_count > 0:
            cursor.execute("SELECT feedback_text, timestamp FROM feedbacks ORDER BY timestamp DESC LIMIT 3")
            for text, ts in cursor.fetchall():
                dt = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  [{dt}] {text}")
    print("-" * 60)
    
    # 5. Check QA cache
    if "qa_cache" in tables:
        cursor.execute("SELECT COUNT(*) FROM qa_cache")
        cache_count = cursor.fetchone()[0]
        print(f"Duplicate Question Cache: {cache_count} cached queries.")
    
    print("=" * 60)
    conn.close()

if __name__ == "__main__":
    print_summary()
