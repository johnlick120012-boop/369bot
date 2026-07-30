import sqlite3
import datetime
import os
import logging
import urllib.request
import urllib.parse
import json
from typing import Any, Optional

logger = logging.getLogger("MemecoinBot.PremiumDB")

# Local SQLite fallback DB file
DB_FILE = os.path.join(os.path.dirname(__file__), "premium.db")

# Supabase Configurations
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

def is_supabase_configured() -> bool:
    """Checks if Supabase is configured in the environment."""
    return bool(SUPABASE_URL and SUPABASE_KEY and "YOUR_SUPABASE" not in SUPABASE_URL and "YOUR_SUPABASE" not in SUPABASE_KEY)

def supabase_request(path: str, method: str = "GET", data: dict = None, prefer_upsert: bool = False) -> Any:
    """Makes a request to the Supabase REST API."""
    if not is_supabase_configured():
        return None
        
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    if prefer_upsert:
        headers["Prefer"] = "resolution=merge-duplicates"
        
    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        status = response.status
        if status in (200, 201):
            body = response.read().decode("utf-8")
            return json.loads(body) if body else []
        elif status == 204:
            return []
        else:
            raise Exception(f"Supabase returned status code {status}")

def parse_iso_datetime(dt_str: str) -> datetime.datetime:
    """Safely parses ISO datetime strings from SQLite or Supabase across python versions."""
    if not dt_str:
        return datetime.datetime.min
    clean_str = dt_str.replace("Z", "+00:00")
    try:
        if "+" in clean_str:
            clean_str = clean_str.split("+")[0]
        return datetime.datetime.fromisoformat(clean_str)
    except Exception:
        return datetime.datetime.min

def get_connection():
    return sqlite3.connect(DB_FILE)

def init_db():
    """Initializes the local fallback SQLite database when Supabase is unavailable."""
    if is_supabase_configured():
        logger.info("Supabase configured; skipping local premium SQLite initialization.")
        return

    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS premium_users (
        user_id TEXT PRIMARY KEY,
        username TEXT,
        purchase_date TEXT,
        expire_date TEXT
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS command_usage (
        user_id TEXT,
        command TEXT,
        use_date TEXT,
        count INTEGER,
        PRIMARY KEY (user_id, command, use_date)
    )
    """)
    
    conn.commit()
    conn.close()
    logger.info("Local premium SQLite fallback database initialized.")

def add_premium_user(user_id: str, username: str, months: int = 1) -> str:
    """Adds a user to premium. Supabase is authoritative when configured."""
    now = datetime.datetime.utcnow()
    expiry = now + datetime.timedelta(days=30 * months)
    now_str = now.isoformat()
    expiry_str = expiry.isoformat()
    
    if is_supabase_configured():
        data = {
            "user_id": str(user_id),
            "username": username,
            "purchase_date": now_str,
            "expire_date": expiry_str
        }
        supabase_request("premium_users?on_conflict=user_id", "POST", data, prefer_upsert=True)
        logger.info(f"[Supabase] Added premium user: {username} ({user_id}) expiring on {expiry_str}")
        return expiry_str

    # Local fallback is only for development when Supabase is not configured.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO premium_users (user_id, username, purchase_date, expire_date)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
        username = excluded.username,
        purchase_date = excluded.purchase_date,
        expire_date = excluded.expire_date
    """, (str(user_id), username, now_str, expiry_str))
    conn.commit()
    conn.close()
    logger.info(f"[SQLite] Added premium user: {username} ({user_id}) expiring on {expiry_str}")
    return expiry_str

def remove_premium_user(user_id: str) -> bool:
    """Removes a user from premium. Supabase is authoritative when configured."""
    if is_supabase_configured():
        safe_uid = urllib.parse.quote(str(user_id))
        supabase_request(f"premium_users?user_id=eq.{safe_uid}", "DELETE")
        logger.info(f"[Supabase] Removed premium user ID: {user_id}")
        return True

    # Local fallback is only for development when Supabase is not configured.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM premium_users WHERE user_id = ?", (str(user_id),))
    affected = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if affected:
        logger.info(f"[SQLite] Removed premium user ID: {user_id}")
    return affected

def is_premium(user_id: str) -> bool:
    """Checks whether a user is currently premium. Supabase is authoritative when configured."""
    if is_supabase_configured():
        try:
            safe_uid = urllib.parse.quote(str(user_id))
            res = supabase_request(f"premium_users?user_id=eq.{safe_uid}&select=expire_date", "GET")
            if res:
                expire_date_str = res[0].get("expire_date")
                expire_date = parse_iso_datetime(expire_date_str)
                return datetime.datetime.utcnow() < expire_date
            return False
        except Exception as e:
            logger.error(f"Supabase is_premium failed; denying premium until Supabase responds: {e}")
            return False

    # Local fallback is only for development when Supabase is not configured.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM premium_users WHERE user_id = ?", (str(user_id),))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return False
        
    expire_date = parse_iso_datetime(row[0])
    return datetime.datetime.utcnow() < expire_date

def get_premium_users() -> list:
    """Gets all premium users. Supabase is authoritative when configured."""
    if is_supabase_configured():
        res = supabase_request("premium_users?select=*", "GET")
        result = []
        now = datetime.datetime.utcnow()
        for row in res or []:
            exp_str = row.get("expire_date")
            expire_date = parse_iso_datetime(exp_str)
            active = now < expire_date
            result.append({
                "user_id": row.get("user_id"),
                "username": row.get("username"),
                "purchase_date": row.get("purchase_date"),
                "expire_date": exp_str,
                "active": active
            })
        return result

    # Local fallback is only for development when Supabase is not configured.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, purchase_date, expire_date FROM premium_users")
    rows = cursor.fetchall()
    conn.close()
    
    result = []
    now = datetime.datetime.utcnow()
    for row in rows:
        expire_date = parse_iso_datetime(row[3])
        active = now < expire_date
        result.append({
            "user_id": row[0],
            "username": row[1],
            "purchase_date": row[2],
            "expire_date": row[3],
            "active": active
        })
    return result

def check_and_increment_usage(user_id: str, command: str, limit: int) -> tuple[bool, int]:
    """
    Checks daily usage for non-premium users.
    If premium, returns (True, 0).
    Uses Supabase when configured; local SQLite is development-only.
    """
    if is_premium(user_id):
        return True, 0
        
    today_str = datetime.date.today().isoformat()
    
    if is_supabase_configured():
        try:
            safe_uid = urllib.parse.quote(str(user_id))
            safe_cmd = urllib.parse.quote(command)
            res = supabase_request(f"command_usage?user_id=eq.{safe_uid}&command=eq.{safe_cmd}&use_date=eq.{today_str}&select=count", "GET")
            current_count = res[0].get("count", 0) if res else 0
            
            if current_count >= limit:
                return False, current_count
                
            new_count = current_count + 1
            data = {
                "user_id": str(user_id),
                "command": command,
                "use_date": today_str,
                "count": new_count
            }
            supabase_request("command_usage", "POST", data, prefer_upsert=True)
            return True, new_count
        except Exception as e:
            logger.error(f"Supabase check_and_increment_usage failed; blocking limited command to avoid local drift: {e}")
            return False, limit

    # Local fallback is only for development when Supabase is not configured.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT count FROM command_usage 
    WHERE user_id = ? AND command = ? AND use_date = ?
    """, (str(user_id), command, today_str))
    
    row = cursor.fetchone()
    current_count = row[0] if row else 0
    
    if current_count >= limit:
        conn.close()
        return False, current_count
        
    new_count = current_count + 1
    cursor.execute("""
    INSERT INTO command_usage (user_id, command, use_date, count)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id, command, use_date) DO UPDATE SET count = excluded.count
    """, (str(user_id), command, today_str, new_count))
    
    conn.commit()
    conn.close()
    return True, new_count

def clean_expired_and_old_usage():
    """Cleans up old usage records in the active storage backend."""
    today_str = datetime.date.today().isoformat()
    
    if is_supabase_configured():
        try:
            supabase_request(f"command_usage?use_date=neq.{today_str}", "DELETE")
            logger.info("Cleaned up old command usage records on Supabase.")
        except Exception as e:
            logger.error(f"Supabase clean_expired_and_old_usage failed: {e}")
        return

    # Local fallback is only for development when Supabase is not configured.
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM command_usage WHERE use_date != ?", (today_str,))
        deleted_usage = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted_usage > 0:
            logger.info(f"Cleaned up {deleted_usage} old command usage records on local SQLite.")
    except Exception as e:
        logger.error(f"SQLite clean_expired_and_old_usage failed: {e}")
