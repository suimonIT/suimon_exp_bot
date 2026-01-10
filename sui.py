import os
import asyncio
import logging
import sqlite3
import threading
import json
import http.server
import socketserver
import base64
import requests
import time

from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from typing import Optional, Dict, List, Tuple
from aiogram.filters import Command
from aiohttp import web
from http.server import HTTPServer, BaseHTTPRequestHandler
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandObject

# ---- Load config ----
load_dotenv(".env.sui")
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEB_PORT = int(os.getenv('WEB_PORT', 8080))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

# ---- Setup logging ----
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# Multiple admin support
OWNER_IDS = []
owner_ids_str = os.getenv('OWNER_IDS')
if owner_ids_str:
    try:
        OWNER_IDS = [int(uid.strip()) for uid in owner_ids_str.split(',') if uid.strip().isdigit()]
    except ValueError:
        OWNER_IDS = []
        logger.warning("Invalid OWNER_IDS format in environment variables")

# Keep backward compatibility with single OWNER_ID
if not OWNER_IDS and os.getenv('OWNER_ID'):
    try:
        OWNER_IDS = [int(os.getenv('OWNER_ID'))]
    except ValueError:
        OWNER_IDS = []
        logger.warning("Invalid OWNER_ID format in environment variables")

# Add these new configuration variables
ALLOWED_GROUP_ID = int(os.getenv('ALLOWED_GROUP_ID')) if os.getenv('ALLOWED_GROUP_ID') else None
ALLOWED_GROUP_LINK = os.getenv('ALLOWED_GROUP_LINK', 'the authorized group')
DEVELOPER_CONTACT = os.getenv('DEVELOPER_CONTACT', '@iceflurryx')

# GitHub Configuration
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_USERNAME = os.getenv('GITHUB_USERNAME', 'Prosper0013')
REPO_NAME = os.getenv('REPO_NAME', 'xp-leaderboard-data')
BRANCH = os.getenv('BRANCH', 'main')

# Weekly Reset Configuration
WEEKLY_RESET_DAY = 6  # Sunday (0=Monday, 6=Sunday)
WEEKLY_RESET_HOUR = 22  # 11 PM
WEEKLY_RESET_MINUTE = 0
WEEKLY_WINNERS_COUNT = 10

if not BOT_TOKEN:
    raise SystemExit('Please set BOT_TOKEN in .env')

# ---- SQLite Database ----
class SQLiteStorage:
    def __init__(self, db_path="xp_bot.db"):
        self.db_path = db_path
        self._init_db()
        self.create_weekly_reports_table()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # Users table - stores XP, streaks, and user info
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    xp INTEGER DEFAULT 0,
                    streak INTEGER DEFAULT 0,
                    last_checkin TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Daily activity table - tracks message counts per day per chat
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_activity (
                    date TEXT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    message_count INTEGER DEFAULT 0,
                    PRIMARY KEY (date, chat_id, user_id)
                )
            """)
            
            # Role thresholds table - chat-specific role requirements
            conn.execute("""
                CREATE TABLE IF NOT EXISTS role_thresholds (
                    chat_id INTEGER,
                    role_name TEXT,
                    level_threshold INTEGER,
                    PRIMARY KEY (chat_id, role_name)
                )
            """)
            
            # User roles table - tracks assigned roles
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_roles (
                    chat_id INTEGER,
                    user_id INTEGER,
                    role_name TEXT,
                    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id, role_name)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS weekly_reports (
                    week_start_date TEXT PRIMARY KEY,
                    winners_data TEXT,
                    total_participants INTEGER,
                    total_xp_distributed INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes for better performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_xp ON users(xp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_activity_date_chat ON daily_activity(date, chat_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_activity_count ON daily_activity(message_count DESC)")
    
    # XP operations
    def award_xp(self, user_id: int, amount: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO users (user_id, xp) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET xp = xp + ?",
                (user_id, amount, amount)
            )
    
    def get_xp_and_rank(self, user_id: int) -> Tuple[int, Optional[int]]:
        with sqlite3.connect(self.db_path) as conn:
            # Get user XP
            user_xp = conn.execute(
                "SELECT xp FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            
            if not user_xp:
                return 0, None
            
            # Get rank (count of users with more XP)
            rank = conn.execute(
                "SELECT COUNT(*) + 1 FROM users WHERE xp > ?", (user_xp[0],)
            ).fetchone()[0]
            
            return user_xp[0], rank
    
    def get_leaderboard(self, limit=10) -> List[Tuple]:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT user_id, username, first_name, xp FROM users ORDER BY xp DESC LIMIT ?",
                (limit,)
            ).fetchall()
    
    # User profile operations
    def update_user_profile(self, user_id: int, username: str, first_name: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO users (user_id, username, first_name) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET 
                    username = excluded.username, 
                    first_name = excluded.first_name
            """, (user_id, username, first_name))
    
    def get_user_profile(self, user_id: int):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT username, first_name, xp, streak, last_checkin FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
    
    # Daily check-in operations
    def can_check_in_today(self, user_id: int, today: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            last_checkin = conn.execute(
                "SELECT last_checkin FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return not last_checkin or last_checkin[0] != today
    
    def update_streak_and_checkin(self, user_id: int, streak: int, today: str, xp_earned: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET streak = ?, last_checkin = ?, xp = xp + ? WHERE user_id = ?",
                (streak, today, xp_earned, user_id)
            )
    
    def get_streak(self, user_id: int) -> int:
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                "SELECT streak FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return result[0] if result else 0
    
    # Daily activity operations
    def track_daily_message(self, user_id: int, chat_id: int, today: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO daily_activity (date, chat_id, user_id, message_count) 
                VALUES (?, ?, ?, 1)
                ON CONFLICT(date, chat_id, user_id) DO UPDATE SET message_count = message_count + 1
            """, (today, chat_id, user_id))
    
    def get_daily_activity(self, date: str, chat_id: int, limit=10):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("""
                SELECT da.user_id, u.username, u.first_name, da.message_count
                FROM daily_activity da
                LEFT JOIN users u ON da.user_id = u.user_id
                WHERE da.date = ? AND da.chat_id = ?
                ORDER BY da.message_count DESC
                LIMIT ?
            """, (date, chat_id, limit)).fetchall()
    
    def clear_daily_activity(self, date: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM daily_activity WHERE date = ?", (date,))
    
    # Role operations
    def set_role_threshold(self, chat_id: int, role_name: str, level: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO role_thresholds (chat_id, role_name, level_threshold)
                VALUES (?, ?, ?)
            """, (chat_id, role_name, level))
    
    def get_role_thresholds(self, chat_id: int):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT role_name, level_threshold FROM role_thresholds WHERE chat_id = ?",
                (chat_id,)
            ).fetchall()
            return {role: level for role, level in rows}
    
    def remove_role_threshold(self, chat_id: int, role_name: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM role_thresholds WHERE chat_id = ? AND role_name = ?",
                (chat_id, role_name)
            )
    
    def assign_user_role(self, chat_id: int, user_id: int, role_name: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO user_roles (chat_id, user_id, role_name)
                VALUES (?, ?, ?)
            """, (chat_id, user_id, role_name))
    
    def user_has_role(self, chat_id: int, user_id: int, role_name: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                "SELECT 1 FROM user_roles WHERE chat_id = ? AND user_id = ? AND role_name = ?",
                (chat_id, user_id, role_name)
            ).fetchone()
            return result is not None

    def get_weekly_leaderboard(self, limit=10) -> List[Tuple]:
        """Get leaderboard for the current week"""
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT user_id, username, first_name, xp FROM users ORDER BY xp DESC LIMIT ?",
                (limit,)
            ).fetchall()

    def reset_weekly_xp(self):
        """Reset all users' XP to 0 for new week"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE users SET xp = 0")
            logger.info("Weekly XP reset completed")

    def backup_weekly_results(self, winners_data: List[Dict]):
        """Backup weekly winners to a separate table for history"""
        with sqlite3.connect(self.db_path) as conn:
            # Create weekly history table if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS weekly_winners_history (
                    week_start_date TEXT,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    final_xp INTEGER,
                    rank INTEGER,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (week_start_date, user_id)
                )
            """)
            
            # Get week start date (previous Sunday)
            today = datetime.now(timezone.utc)
            week_start = today - timedelta(days=(today.weekday() + 1) % 7)
            week_start_str = week_start.strftime('%Y-%m-%d')
            
            # Insert winners data
            for winner in winners_data:
                conn.execute("""
                    INSERT INTO weekly_winners_history 
                    (week_start_date, user_id, username, first_name, final_xp, rank)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    week_start_str,
                    winner['user_id'],
                    winner['username'],
                    winner['first_name'],
                    winner['xp'],
                    winner['rank']
                ))

    def create_weekly_reports_table(self):
        """Create weekly reports table for historical data"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS weekly_reports (
                    week_start_date TEXT PRIMARY KEY,
                    winners_data TEXT,  -- JSON string of winners
                    total_participants INTEGER,
                    total_xp_distributed INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            logger.info("Weekly reports table ensured")

    def save_weekly_report(self, week_start_date: str, winners_data: List[Dict], 
                          total_participants: int, total_xp_distributed: int):
        """Save weekly report to database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO weekly_reports 
                (week_start_date, winners_data, total_participants, total_xp_distributed)
                VALUES (?, ?, ?, ?)
            """, (
                week_start_date,
                json.dumps(winners_data),
                total_participants,
                total_xp_distributed
            ))

    def get_weekly_report(self, week_start_date: str) -> Optional[Dict]:
        """Get weekly report by start date"""
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute("""
                SELECT week_start_date, winners_data, total_participants, total_xp_distributed, created_at
                FROM weekly_reports WHERE week_start_date = ?
            """, (week_start_date,)).fetchone()
            
            if result:
                return {
                    'week_start_date': result[0],
                    'winners_data': json.loads(result[1]),
                    'total_participants': result[2],
                    'total_xp_distributed': result[3],
                    'created_at': result[4]
                }
            return None

    def get_all_weekly_reports(self) -> List[Dict]:
        """Get all weekly reports sorted by date"""
        with sqlite3.connect(self.db_path) as conn:
            results = conn.execute("""
                SELECT week_start_date, winners_data, total_participants, total_xp_distributed, created_at
                FROM weekly_reports ORDER BY week_start_date DESC
            """).fetchall()
            
            reports = []
            for result in results:
                reports.append({
                    'week_start_date': result[0],
                    'winners_data': json.loads(result[1]),
                    'total_participants': result[2],
                    'total_xp_distributed': result[3],
                    'created_at': result[4]
                })
            return reports

    def get_available_weeks(self) -> List[str]:
        """Get list of all available week start dates"""
        with sqlite3.connect(self.db_path) as conn:
            results = conn.execute("""
                SELECT week_start_date FROM weekly_reports ORDER BY week_start_date DESC
            """).fetchall()
            return [result[0] for result in results]
        
# Initialize database
db = SQLiteStorage("xp_bot.db")

# Constants
DAILY_CHECKIN_BASE_XP = 50
MAX_STREAK_BONUS = 50
WEEKLY_STREAK_BONUS = 200
ACTIVE_USER_XP = [100, 75, 50]  # 1st, 2nd, 3rd place

# Level formula
LEVEL_XP = 100

# Bot and dispatcher setup
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Web server setup
web_app = web.Application()
routes = web.RouteTableDef()

# Utility functions
async def should_process_message(message: types.Message) -> bool:
    """
    Check if the bot should process this message
    Returns True if allowed, False if not allowed
    """
    # Allow messages from any bot admin in any chat
    if message.from_user.id in OWNER_IDS:
        return True
    
    # Handle private chats
    if message.chat.type == 'private':
        # Only allow admins in private chats
        if message.from_user.id in OWNER_IDS:
            return True
        else:
            # Send contact message for unauthorized private chats
            await message.answer(
                f"🤖 This bot only works in the authorized group.\n\n"
                f"📍 Join our group: {ALLOWED_GROUP_LINK}\n"
                f"👨‍💻 Contact developer: {DEVELOPER_CONTACT}"
            )
            return False
    
    # Handle group chats
    if message.chat.type in ['group', 'supergroup']:
        # Check if this is the allowed group
        if ALLOWED_GROUP_ID and message.chat.id == ALLOWED_GROUP_ID:
            return True
        else:
            # Send contact message for unauthorized groups
            await message.answer(
                f"🚫 This bot is not authorized for this group.\n\n"
                f"🤖 This bot only works in: {ALLOWED_GROUP_LINK}\n"
                f"👨‍💻 Contact developer: {DEVELOPER_CONTACT}"
            )
            return False
    
    # Deny all other chat types
    return False

async def is_user_admin(user_id: int) -> bool:
    """Check if user is the bot owner/admin"""
    return user_id in OWNER_IDS

async def delete_command_message(message: types.Message):
    """Delete the command message after processing"""
    try:
        # Only delete in groups, not in private chats
        if message.chat.type in ['group', 'supergroup']:
            await message.delete()
            logger.info(f"Deleted command message from {message.from_user.id} in chat {message.chat.id}")
    except Exception as e:
        logger.debug(f"Could not delete message: {e}")

def format_response_with_username(message: types.Message, response: str) -> str:
    """Format response with username mention"""
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    if username:
        mention = f"@{username}"
    else:
        mention = first_name or "User"
    
    return f"{mention}, {response}"
    
def xp_to_level(xp: int) -> int:
    return xp // LEVEL_XP

def get_today_key() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

def award_xp(user: types.User, amount: int, reason: str = ""):
    db.award_xp(user.id, amount)
    db.update_user_profile(user.id, user.username or '', user.first_name or '')
    logger.info(f"Awarded {amount} XP to {user.id} for: {reason}")

def get_xp_and_rank(user_id: int) -> Tuple[int, Optional[int]]:
    return db.get_xp_and_rank(user_id)

def start_windows_server():
    """Start Windows built-in HTTP server"""
    handler = http.server.SimpleHTTPRequestHandler
    
    # Create custom handler to serve our API
    class CustomHandler(handler):
        def do_GET(self):
            if self.path == '/api/leaderboard':
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                
                try:
                    top_users = db.get_leaderboard(20)
                    leaderboard_data = []
                    for rank, (user_id, username, first_name, xp) in enumerate(top_users, 1):
                        display_name = f"@{username}" if username else (first_name or f"User {user_id}")
                        level = xp_to_level(xp)
                        leaderboard_data.append({
                            'rank': rank,
                            'user_id': user_id,
                            'username': display_name,
                            'xp': xp,
                            'level': level
                        })
                    
                    response = {
                        'success': True,
                        'leaderboard': leaderboard_data,
                        'last_updated': datetime.now(timezone.utc).isoformat(),
                        'total_users': len(leaderboard_data)
                    }
                    self.wfile.write(json.dumps(response).encode())
                except Exception as e:
                    self.wfile.write(json.dumps({'success': False, 'error': str(e)}).encode())
            else:
                self.send_error(404)
    
    with socketserver.TCPServer(("", 8082), CustomHandler) as httpd:
        print("🌐 Windows HTTP server on port 8082")
        httpd.serve_forever()

# Web API Routes
class APIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/leaderboard':
            try:
                # Get leaderboard data from your database
                top_users = db.get_leaderboard(20)
                leaderboard_data = []
                for rank, (user_id, username, first_name, xp) in enumerate(top_users, 1):
                    display_name = f"@{username}" if username else (first_name or f"User {user_id}")
                    level = xp_to_level(xp)
                    leaderboard_data.append({
                        'rank': rank,
                        'user_id': user_id,
                        'username': display_name,
                        'xp': xp,
                        'level': level
                    })
                
                response = {
                    'success': True,
                    'leaderboard': leaderboard_data,
                    'last_updated': datetime.now(timezone.utc).isoformat(),
                    'total_users': len(leaderboard_data)
                }
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'error': str(e)}).encode())
                
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Disable default logging
        return

def start_simple_server():
    """Start a simple HTTP server on port 8081"""
    try:
        server = HTTPServer(('0.0.0.0', 8081), APIHandler)
        logger.info("🌐 Simple API server started on port 8081")
        logger.info("📊 API URL: http://204.12.218.42:8081/api/leaderboard")
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Failed to start simple server: {e}")

def update_github_leaderboard():
    """Update leaderboard data on GitHub with better error handling"""
    try:
        if not GITHUB_TOKEN:
            logger.warning("GitHub token not configured")
            return False
            
        # Get leaderboard data from your database
        top_users = db.get_leaderboard(20)
        leaderboard_data = []
        for rank, (user_id, username, first_name, xp) in enumerate(top_users, 1):
            display_name = f"@{username}" if username else (first_name or f"User {user_id}")
            level = xp // 100
            leaderboard_data.append({
                'rank': rank,
                'user_id': user_id,
                'username': display_name,
                'xp': xp,
                'level': level
            })
        
        data = {
            'success': True,
            'leaderboard': leaderboard_data,
            'last_updated': datetime.now().isoformat(),
            'total_users': len(leaderboard_data)
        }
        
        # Convert to JSON string
        json_data = json.dumps(data, indent=2)
        
        # GitHub API URL
        url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{REPO_NAME}/contents/leaderboard.json"
        
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        # Try to get current file to get SHA (for updates)
        sha = None
        try:
            current_response = requests.get(url, headers=headers)
            if current_response.status_code == 200:
                current_data = current_response.json()
                sha = current_data['sha']
                logger.debug("Found existing file, will update")
        except Exception as e:
            logger.debug(f"No existing file found or error: {e}")
        
        # Prepare update data
        update_data = {
            "message": f"Auto-update leaderboard - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "content": base64.b64encode(json_data.encode()).decode(),
            "branch": BRANCH
        }
        
        if sha:
            update_data["sha"] = sha
        
        # Make the API request
        response = requests.put(url, json=update_data, headers=headers)
        
        if response.status_code in [200, 201]:
            logger.info(f"✅ GitHub updated successfully at {datetime.now()}")
            return True
        else:
            logger.error(f"❌ GitHub update failed: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ GitHub sync error: {e}")
        return False

def github_sync_loop():
    """Background thread to sync with GitHub every 2 minutes"""
    while True:
        try:
            update_github_leaderboard()
            time.sleep(120)  # 2 minutes
        except Exception as e:
            logger.error(f"GitHub sync loop error: {e}")
            time.sleep(60)  # Wait 1 minute on error

# Start the sync thread
def start_github_sync():
    sync_thread = threading.Thread(target=github_sync_loop, daemon=True)
    sync_thread.start()
    print("🚀 GitHub sync started - updating every 2 minutes")
    
@routes.get('/api/leaderboard')
async def get_leaderboard(request):
    """API endpoint to get leaderboard data"""
    try:
        top_users = db.get_leaderboard(20)
        
        leaderboard_data = []
        for rank, (user_id, username, first_name, xp) in enumerate(top_users, 1):
            display_name = f"@{username}" if username else (first_name or f"User {user_id}")
            level = xp_to_level(xp)
            leaderboard_data.append({
                'rank': rank,
                'user_id': user_id,
                'username': display_name,
                'xp': xp,
                'level': level
            })
        
        return web.json_response({
            'success': True,
            'leaderboard': leaderboard_data,
            'last_updated': datetime.now(timezone.utc).isoformat(),
            'total_users': len(leaderboard_data)
        })
    except Exception as e:
        logger.error(f"API Error: {e}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

@routes.get('/api/stats')
async def get_stats(request):
    """API endpoint to get overall bot statistics"""
    try:
        with sqlite3.connect("xp_bot.db") as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_xp = conn.execute("SELECT SUM(xp) FROM users").fetchone()[0] or 0
            avg_xp = conn.execute("SELECT AVG(xp) FROM users").fetchone()[0] or 0
            
        return web.json_response({
            'success': True,
            'stats': {
                'total_users': total_users,
                'total_xp': total_xp,
                'average_xp': round(avg_xp, 2),
                'top_level': xp_to_level(total_xp) if total_xp else 0
            },
            'last_updated': datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)}, status=500)

@routes.get('/')
async def serve_redirect(request):
    """Redirect to your live website"""
    return web.HTTPFound('https://suimonatsui.xyz/')

# Add CORS support for cross-domain requests
async def cors_middleware(app, handler):
    async def middleware_handler(request):
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    return middleware_handler

web_app.middlewares.append(cors_middleware)
web_app.add_routes(routes)

async def start_web_server():
    """Start the web server on port 80"""
    
    # CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, HEAD'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    web_app.middlewares.append(cors_middleware)
    
    # Add OPTIONS handler for preflight requests
    async def options_handler(request):
        return web.Response()
    
    web_app.router.add_route('OPTIONS', '/api/leaderboard', options_handler)
    web_app.router.add_route('OPTIONS', '/api/stats', options_handler)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    
    # Try port 80 first, fallback to 8080 if permission denied
    try:
        site = web.TCPSite(runner, '0.0.0.0', 80)
        await site.start()
        logger.info(f"🌐 Web server started on port 80")
        logger.info(f"📊 Public API: http://204.12.218.42/api/leaderboard")
    except OSError as e:
        if "permission denied" in str(e).lower():
            logger.warning("⚠️  Cannot bind to port 80, falling back to port 8080")
            site = web.TCPSite(runner, '0.0.0.0', 8080)
            await site.start()
            logger.info(f"🌐 Web server started on port 8080")
            logger.info(f"📊 Public API: http://204.12.218.42:8080/api/leaderboard")
        else:
            raise e

# Daily Check-in System
def can_check_in_today(user_id: int) -> bool:
    return db.can_check_in_today(user_id, get_today_key())

def get_user_streak(user_id: int) -> int:
    return db.get_streak(user_id)

def process_daily_checkin(user: types.User) -> Dict:
    """Process daily check-in and return reward details"""
    
    user_id = user.id

    # 🔧 WICHTIGER FIX:
    # Stelle sicher, dass der User sofort in der DB existiert
    db.update_user_profile(user.id, user.username or '', user.first_name or '')

    today_key = get_today_key()
    
    # Check if already checked in today
    if not can_check_in_today(user_id):
        return {'success': False, 'message': 'You have already checked in today!'}

    # Get current streak
    current_streak = get_user_streak(user_id)
    user_profile = db.get_user_profile(user_id)
    last_checkin_date = user_profile[4] if user_profile else None

    # Check streak continuity
    if last_checkin_date:
        last_date = datetime.strptime(last_checkin_date, '%Y-%m-%d').date()
        today_date = datetime.now(timezone.utc).date()
        yesterday = today_date - timedelta(days=1)

        if last_date == yesterday:
            new_streak = current_streak + 1
        elif last_date == today_date:
            return {'success': False, 'message': 'You have already checked in today!'}
        else:
            new_streak = 1
    else:
        new_streak = 1

    # Calculate XP reward
    streak_bonus = min(new_streak * 10, MAX_STREAK_BONUS)
    total_xp = DAILY_CHECKIN_BASE_XP + streak_bonus

    # Weekly bonus
    weekly_bonus = 0
    if new_streak % 7 == 0:
        weekly_bonus = WEEKLY_STREAK_BONUS
        total_xp += weekly_bonus

    # Update records
    db.update_streak_and_checkin(user_id, new_streak, today_key, total_xp)

    return {
        'success': True,
        'xp': total_xp,
        'streak': new_streak,
        'base_xp': DAILY_CHECKIN_BASE_XP,
        'streak_bonus': streak_bonus,
        'weekly_bonus': weekly_bonus,
        'message': '✅ Daily check-in successful!'
    }

# Most Active User System
def track_daily_message(user: types.User, chat_id: int):
    """Track user's daily message count"""
    db.track_daily_message(user.id, chat_id, get_today_key())
    db.update_user_profile(user.id, user.username or '', user.first_name or '')

async def award_most_active_users():
    """Award XP to most active users from previous day and reset counters"""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # Get all unique chat IDs from yesterday's activity
    with sqlite3.connect("xp_bot.db") as conn:
        chat_ids = conn.execute(
            "SELECT DISTINCT chat_id FROM daily_activity WHERE date = ?", (yesterday,)
        ).fetchall()
    
    for (chat_id,) in chat_ids:
        top_users = db.get_daily_activity(yesterday, chat_id, limit=3)
        
        if not top_users:
            continue
        
        # Award XP to top 3 active users
        for i, (user_id, username, first_name, message_count) in enumerate(top_users):
            if i < len(ACTIVE_USER_XP):
                xp_reward = ACTIVE_USER_XP[i]
                
                # Award XP
                db.award_xp(user_id, xp_reward)
                
                # Notify chat about winners
                try:
                    display_name = f"@{username}" if username else (first_name or f"User {user_id}")
                    place = ['1st', '2nd', '3rd'][i]
                    await bot.send_message(
                        chat_id,
                        f"🏆 {place} Most Active User Yesterday: {display_name}\n"
                        f"📊 Messages: {message_count} | Reward: +{xp_reward} XP"
                    )
                except Exception as e:
                    logger.debug(f"Could not send active user announcement to {chat_id}: {e}")
    
    # Clean up yesterday's data
    db.clear_daily_activity(yesterday)

def update_github_weekly_winners(week_start_date: str, winners_data: List[Dict]):
    """Update weekly winners data on GitHub"""
    try:
        if not GITHUB_TOKEN:
            logger.warning("GitHub token not configured for weekly winners")
            return False
            
        data = {
            'success': True,
            'week_start_date': week_start_date,
            'winners': winners_data,
            'last_updated': datetime.now().isoformat()
        }
        
        # Convert to JSON string
        json_data = json.dumps(data, indent=2)
        
        # GitHub API URL for weekly winners
        url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{REPO_NAME}/contents/weekly_winners.json"
        
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        # Try to get current file to get SHA (for updates)
        sha = None
        try:
            current_response = requests.get(url, headers=headers)
            if current_response.status_code == 200:
                current_data = current_response.json()
                sha = current_data['sha']
                logger.debug("Found existing weekly winners file, will update")
        except Exception as e:
            logger.debug(f"No existing weekly winners file found: {e}")
        
        # Prepare update data
        update_data = {
            "message": f"Weekly winners update - {week_start_date}",
            "content": base64.b64encode(json_data.encode()).decode(),
            "branch": BRANCH
        }
        
        if sha:
            update_data["sha"] = sha
        
        # Make the API request
        response = requests.put(url, json=update_data, headers=headers)
        
        if response.status_code in [200, 201]:
            logger.info(f"✅ Weekly winners updated on GitHub for week {week_start_date}")
            return True
        else:
            logger.error(f"❌ Weekly winners GitHub update failed: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Weekly winners GitHub sync error: {e}")
        return False
        
async def daily_reset_task():
    """Background task to reset daily counters and award most active users"""
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (next_midnight - now).total_seconds()
        
        logger.info(f"Daily reset scheduled in {sleep_seconds} seconds")
        await asyncio.sleep(sleep_seconds)
        
        try:
            await award_most_active_users()
            logger.info("Daily most active users awarded successfully")
        except Exception as e:
            logger.error(f"Error in daily reset: {e}")

# Role reward system
def set_role_threshold(chat_id: int, role_name: str, level_threshold: int):
    db.set_role_threshold(chat_id, role_name, level_threshold)

def get_role_thresholds(chat_id: int):
    return db.get_role_thresholds(chat_id)

def remove_role_threshold(chat_id: int, role_name: str):
    db.remove_role_threshold(chat_id, role_name)

#=====================================================================================================================================
#======================================================= Admin inline button handler =================================================
#=====================================================================================================================================
@dp.message(Command('admin_help'))
async def cmd_admin_help(message: types.Message):
    """Show admin help - Only for admin in private chat"""
    if not await is_user_admin(message.from_user.id):
        return
    
    if message.chat.type != 'private':
        return
    
    help_text = (
        "🛠️ **Admin Commands** (Private Chat Only):\n\n"
        "📊 **Group Management:**\n"
        "• /set_role_threshold <role> <level> - Set role level requirement\n"
        "• /list_role_thresholds - Show all role thresholds\n"
        "• /remove_role_threshold <role> - Remove role threshold\n\n"
        "🔧 **Debug & Info:**\n"
        "• /debug - Show bot status and configuration\n"
        "• /admin_help - Show this help message\n\n"
        "⚙️ **Note:** All admin commands work only in private chat and affect the authorized group automatically."
    )
    
    await message.reply(help_text, parse_mode='Markdown')
    
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    """Admin panel with inline buttons - Private chat only"""
    user_id = message.from_user.id
    logger.info(f"🎯 Admin command received from user {user_id} in chat {message.chat.id}")
    logger.info(f"🔍 User IDs in OWNER_IDS: {OWNER_IDS}")
    logger.info(f"🔍 User is admin: {user_id in OWNER_IDS}")
    logger.info(f"🔍 Chat type: {message.chat.type}")
    
    if user_id not in OWNER_IDS:
        logger.warning(f"🚫 User {user_id} is not in admin list {OWNER_IDS}")
        await message.reply("❌ This command is only available for bot admins.")
        return
    
    if message.chat.type != 'private':
        logger.warning(f"🚫 Admin command used in non-private chat: {message.chat.type}")
        await message.reply("❌ Please use this command in private chat with the bot.")
        return
    
    logger.info(f"✅ Creating admin panel for user {user_id}")
    
    try:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📊 Weekly Reports", callback_data="admin_weekly_reports")],
                [InlineKeyboardButton(text="🔄 Force Weekly Reset", callback_data="admin_force_reset")],
                [InlineKeyboardButton(text="⚙️ Role Management", callback_data="admin_roles")],
                [InlineKeyboardButton(text="📈 Bot Statistics", callback_data="admin_stats")],
                [InlineKeyboardButton(text="🛠️ Debug Info", callback_data="admin_debug")],
                [InlineKeyboardButton(text="❌ Close Panel", callback_data="admin_close")]
            ]
        )
        
        logger.info("📤 Sending admin panel message...")
        await message.reply(
            "🛠️ **Admin Control Panel**\n\n"
            "Select an option below to manage the bot:",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        logger.info("✅ Admin panel sent successfully")
        
    except Exception as e:
        logger.error(f"❌ Failed to send admin panel: {e}", exc_info=True)
        await message.reply("❌ Failed to create admin panel. Check logs for details.")

async def show_admin_panel(callback: CallbackQuery):
    """Show admin panel from callback (replaces cmd_admin for callbacks)"""
    user_id = callback.from_user.id
    
    logger.info(f"🔄 Creating admin panel for user {user_id} from callback")
    
    try:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📊 Weekly Reports", callback_data="admin_weekly_reports")],
                [InlineKeyboardButton(text="🔄 Force Weekly Reset", callback_data="admin_force_reset")],
                [InlineKeyboardButton(text="⚙️ Role Management", callback_data="admin_roles")],
                [InlineKeyboardButton(text="📈 Bot Statistics", callback_data="admin_stats")],
                [InlineKeyboardButton(text="🛠️ Debug Info", callback_data="admin_debug")],
                [InlineKeyboardButton(text="❌ Close Panel", callback_data="admin_close")]
            ]
        )
        
        await callback.message.edit_text(
            "🛠️ **Admin Control Panel**\n\n"
            "Select an option below to manage the bot:",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        logger.info("✅ Admin panel updated successfully from callback")
        
    except Exception as e:
        logger.error(f"❌ Failed to update admin panel from callback: {e}")
        await callback.answer("❌ Failed to update panel", show_alert=True)
        
# Callback query handler for admin buttons
@dp.callback_query(lambda callback: callback.data.startswith('admin_') or callback.data.startswith('weekly_report_') or callback.data.startswith('confirm_'))
async def handle_admin_callbacks(callback: CallbackQuery):
    """Handle all admin inline button callbacks"""
    # Get the actual user who clicked the button (not the bot)
    user_id = callback.from_user.id
    
    logger.info(f"🎯 Admin callback from user {user_id}, data: {callback.data}")
    logger.info(f"🔍 User IDs in OWNER_IDS: {OWNER_IDS}")
    logger.info(f"🔍 User is admin: {user_id in OWNER_IDS}")
    
    if user_id not in OWNER_IDS:
        logger.warning(f"🚫 User {user_id} is not in admin list {OWNER_IDS}")
        await callback.answer("❌ Access denied.", show_alert=True)
        return
    
    data = callback.data
    
    try:
        if data == "admin_weekly_reports":
            await show_weekly_reports_menu(callback)
        
        elif data == "admin_force_reset":
            await force_weekly_reset_callback(callback)
        
        elif data == "admin_roles":
            await show_role_management(callback)
        
        elif data == "admin_stats":
            await show_bot_stats(callback)
        
        elif data == "admin_debug":
            await show_debug_info(callback)
        
        elif data == "admin_close":
            await callback.message.delete()
            await callback.answer("Panel closed")
        
        elif data.startswith("weekly_report_"):
            week_date = data.replace("weekly_report_", "")
            await show_weekly_report(callback, week_date)
        
        elif data == "confirm_force_reset":
            await handle_confirm_reset(callback)
        
        elif data == "admin_back_main":
            # Create a new admin panel instead of calling cmd_admin
            await show_admin_panel(callback)
    
    except Exception as e:
        logger.error(f"Error in admin callback {data}: {e}")
        if "message is not modified" not in str(e):
            await callback.answer(f"❌ Error: {str(e)[:50]}...", show_alert=True)

# Separate handler for confirmation callbacks
@dp.callback_query(lambda callback: callback.data == "confirm_force_reset")
async def handle_confirm_reset(callback: CallbackQuery):
    """Handle confirmed weekly reset"""
    try:
        await callback.message.edit_text("🔄 Processing weekly reset...")
        await process_weekly_reset()
        await callback.message.edit_text(
            "✅ **Weekly Reset Completed!**\n\n"
            "• Winners have been announced\n"
            "• XP has been reset to 0\n"
            "• New week has started\n"
            "• Report saved to database",
            parse_mode='Markdown'
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ **Reset Failed:** {str(e)}",
            parse_mode='Markdown'
        )

# Force weekly reset callback
async def force_weekly_reset_callback(callback: CallbackQuery):
    """Handle force weekly reset from inline button"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm Reset", callback_data="confirm_force_reset")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_back_main")]
        ]
    )
    
    await callback.message.edit_text(
        "🔄 **Force Weekly Reset**\n\n"
        "⚠️ **Warning:** This will immediately:\n"
        "• Calculate current week's winners\n"
        "• Announce winners in the group\n"
        "• Reset all XP to 0\n"
        "• Start a new week\n\n"
        "Are you sure you want to proceed?",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def show_weekly_reports_menu(callback: CallbackQuery):
    """Show weekly reports menu with available weeks"""
    reports = db.get_all_weekly_reports()
    
    if not reports:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_back_main")]
            ]
        )
        await callback.message.edit_text(
            "📊 **Weekly Reports**\n\n"
            "❌ No weekly reports available yet.\n"
            "The first report will be generated after the weekly reset.",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return
    
    # Create buttons for recent weeks (last 8 weeks)
    keyboard_buttons = []
    for report in reports[:8]:
        week_btn = InlineKeyboardButton(
            text=f"📅 {report['week_start_date']} ({report['total_participants']} users)",
            callback_data=f"weekly_report_{report['week_start_date']}"
        )
        keyboard_buttons.append([week_btn])
    
    # Add back button
    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_back_main")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(
        "📊 **Weekly Reports**\n\n"
        "Select a week to view the full report:",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def show_weekly_report(callback: CallbackQuery, week_date: str):
    """Show specific weekly report"""
    report = db.get_weekly_report(week_date)
    
    if not report:
        await callback.answer("❌ Report not found.", show_alert=True)
        return
    
    # Format the report
    lines = [f"📊 **Weekly Report - {week_date}**\n"]
    lines.append(f"👥 **Total Participants:** {report['total_participants']}")
    lines.append(f"🏆 **Total XP Distributed:** {report['total_xp_distributed']:,}")
    lines.append(f"📅 **Generated:** {report['created_at']}\n")
    lines.append("**🏅 Top 10 Winners:**")
    
    for winner in report['winners_data'][:10]:
        medal = "🥇" if winner['rank'] == 1 else "🥈" if winner['rank'] == 2 else "🥉" if winner['rank'] == 3 else f"{winner['rank']}."
        lines.append(f"{medal} {winner['display_name']} - Level {winner['level']} ({winner['xp']} XP)")
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Back to Reports", callback_data="admin_weekly_reports")],
            [InlineKeyboardButton(text="🔙 Main Menu", callback_data="admin_back_main")]
        ]
    )
    
    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def show_role_management(callback: CallbackQuery):
    """Show role management menu with proper update handling"""
    if not ALLOWED_GROUP_ID:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_back_main")]
            ]
        )
        try:
            await callback.message.edit_text(
                "❌ **Role Management**\n\n"
                "ALLOWED_GROUP_ID is not configured in environment variables.",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        except Exception as e:
            if "message is not modified" not in str(e):
                raise e
            # If message is the same, just answer the callback
            await callback.answer("Role management info is current")
        return
    
    thresholds = get_role_thresholds(ALLOWED_GROUP_ID)
    
    lines = ["⚙️ **Role Management**\n"]
    lines.append(f"**Group ID:** `{ALLOWED_GROUP_ID}`\n")
    
    if thresholds:
        lines.append("**Current Role Thresholds:**")
        for role, level in thresholds.items():
            lines.append(f"• {role}: Level {level}")
    else:
        lines.append("No role thresholds configured.")
    
    lines.append("\nUse commands to manage roles:")
    lines.append("• `/set_role_threshold <role> <level>`")
    lines.append("• `/remove_role_threshold <role>`")
    lines.append("• `/list_role_thresholds`")
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_roles")],
            [InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_back_main")]
        ]
    )
    
    new_text = '\n'.join(lines)
    
    try:
        # Check if content is actually different before editing
        if (callback.message.text != new_text or 
            callback.message.reply_markup != keyboard):
            await callback.message.edit_text(
                new_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            await callback.answer("Role management info is current")
    except Exception as e:
        if "message is not modified" in str(e):
            await callback.answer("Role management info is current")
        else:
            raise e

async def check_and_apply_roles_on_levelup(user: types.User, chat_id: int, old_level: int, new_level: int):
    thresholds = get_role_thresholds(chat_id)
    if not thresholds:
        return
    
    for role, lvl in thresholds.items():
        if old_level < lvl <= new_level:
            # Check if user already has this role
            if db.user_has_role(chat_id, user.id, role):
                continue
                
            # Simplified role application
            try:
                if role.lower() == 'admin':
                    await bot.promote_chat_member(
                        chat_id=chat_id,
                        user_id=user.id,
                        can_delete_messages=True,
                        can_invite_users=True,
                        can_restrict_members=True,
                        can_pin_messages=True,
                    )
                
                # Assign role in database
                db.assign_user_role(chat_id, user.id, role)
                
                # Notify about role achievement
                await bot.send_message(
                    chat_id, 
                    f"🎉 Congrats {user.mention if hasattr(user, 'mention') else user.first_name}! "
                    f"You've reached level {lvl} and earned role: <b>{role}</b>.",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to apply role {role} to {user.id}: {e}")

async def weekly_reset_task():
    """Background task to calculate winners and reset XP every Sunday at 23:00"""
    while True:
        now = datetime.now(timezone.utc)
        
        # Calculate next Sunday at 23:00
        days_until_sunday = (WEEKLY_RESET_DAY - now.weekday()) % 7
        if days_until_sunday == 0 and (now.hour > WEEKLY_RESET_HOUR or 
                                      (now.hour == WEEKLY_RESET_HOUR and now.minute >= WEEKLY_RESET_MINUTE)):
            days_until_sunday = 7  # Move to next week
        
        next_reset = (now + timedelta(days=days_until_sunday)).replace(
            hour=WEEKLY_RESET_HOUR, 
            minute=WEEKLY_RESET_MINUTE, 
            second=0, 
            microsecond=0
        )
        
        sleep_seconds = (next_reset - now).total_seconds()
        logger.info(f"Weekly reset scheduled in {sleep_seconds} seconds (next: {next_reset})")
        await asyncio.sleep(sleep_seconds)
        
        try:
            await process_weekly_reset()
            logger.info("Weekly reset completed successfully")
        except Exception as e:
            logger.error(f"Error in weekly reset: {e}")

async def process_weekly_reset():
    """Process weekly winner calculation and reset"""
    # Get weekly winners
    top_users = db.get_weekly_leaderboard(WEEKLY_WINNERS_COUNT)
    
    if not top_users:
        logger.info("No users found for weekly reset")
        return
    
    # Calculate week start date (previous Sunday)
    today = datetime.now(timezone.utc)
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)
    week_start_str = week_start.strftime('%Y-%m-%d')
    
    # Prepare winners data
    winners_data = []
    total_xp_distributed = 0
    
    for rank, (user_id, username, first_name, xp) in enumerate(top_users, 1):
        display_name = f"@{username}" if username else (first_name or f"User {user_id}")
        level = xp_to_level(xp)
        
        winner_info = {
            'rank': rank,
            'user_id': user_id,
            'username': username or '',
            'first_name': first_name or '',
            'display_name': display_name,
            'xp': xp,
            'level': level
        }
        winners_data.append(winner_info)
        total_xp_distributed += xp
    
    # Save to weekly reports
    total_participants = len(db.get_weekly_leaderboard(limit=1000))  # Get all participants
    db.save_weekly_report(week_start_str, winners_data, total_participants, total_xp_distributed)
    
    # Update GitHub with weekly winners
    update_github_weekly_winners(week_start_str, winners_data)
    
    # Create announcement
    announcement_lines = ["🏆 **WEEKLY WINNERS** 🏆\n"]
    announcement_lines.append(f"Week ending: {today.strftime('%Y-%m-%d %H:%M')}\n")
    announcement_lines.append(f"Total Participants: {total_participants}\n")
    
    for winner in winners_data:
        medal = "🥇" if winner['rank'] == 1 else "🥈" if winner['rank'] == 2 else "🥉" if winner['rank'] == 3 else f"{winner['rank']}."
        announcement_lines.append(f"{medal} {winner['display_name']} - Level {winner['level']} ({winner['xp']} XP)")
    
    announcement_lines.append(f"\n📊 Total XP Distributed: {total_xp_distributed:,}")
    announcement_lines.append("🎉 Congratulations to all winners!")
    announcement_lines.append("📈 Weekly counters have been reset. New week starts now!")
    
    # Announce winners in the allowed group
    if ALLOWED_GROUP_ID:
        try:
            announcement = '\n'.join(announcement_lines)
            await bot.send_message(
                ALLOWED_GROUP_ID,
                announcement,
                parse_mode='Markdown'
            )
            logger.info(f"Weekly winners announced in group {ALLOWED_GROUP_ID}")
        except Exception as e:
            logger.error(f"Failed to announce weekly winners: {e}")
    
    # Reset all XP for new week
    db.reset_weekly_xp()
    
    # Update GitHub leaderboard after reset
    update_github_leaderboard()
    
# Bot Handlers
@dp.message(Command('checkin', 'daily'))
async def cmd_checkin(message: types.Message):
    """Daily check-in command"""
    if not await should_process_message(message):
        return
    
    result = process_daily_checkin(message.from_user)
    
    if result['success']:
        xp_info = f"+{result['base_xp']} XP (Base)"
        if result['streak_bonus'] > 0:
            xp_info += f" +{result['streak_bonus']} XP (Streak bonus)"
        if result['weekly_bonus'] > 0:
            xp_info += f" +{result['weekly_bonus']} XP (Weekly bonus!)"
        
        base_response = (
            f"{result['message']}\n\n"
            f"🔥 Streak: {result['streak']} days\n"
            f"🎁 Total: {result['xp']} XP\n"
            f"📊 Breakdown: {xp_info}"
        )
        response = format_response_with_username(message, base_response)
    else:
        response = format_response_with_username(message, result['message'])
    
    sent_message = await message.reply(response)
    
    # Delete the command message after responding
    await delete_command_message(message)

@dp.message(Command('streak', 'streaks'))
async def cmd_streak(message: types.Message):
    """Check current streak"""
    if not await should_process_message(message):
        return
    
    user_id = message.from_user.id
    streak = get_user_streak(user_id)
    user_profile = db.get_user_profile(user_id)
    
    if user_profile and user_profile[4]:  # last_checkin exists
        checked_in_today = user_profile[4] == get_today_key()
        status = "✅ Checked in today" if checked_in_today else "❌ Not checked in today"
    else:
        status = "❌ Never checked in"
    
    base_response = (
        f"📅 Your Check-in Status:\n"
        f"🔥 Current Streak: {streak} days\n"
        f"📊 Status: {status}\n\n"
        f"Use /checkin to get your daily XP!"
    )
    response = format_response_with_username(message, base_response)
    
    sent_message = await message.reply(response)
    await delete_command_message(message)

@dp.message(Command('xp', 'score'))
async def cmd_xp(message: types.Message):
    """Show user's XP and level"""
    if not await should_process_message(message):
        return
    
    uid = message.from_user.id
    xp, rank = get_xp_and_rank(uid)
    level = xp_to_level(xp)
    
    if rank is None:
        base_response = "You have 0 XP yet - use /checkin to get started!"
        response = format_response_with_username(message, base_response)
        sent_message = await message.reply(response)
        await delete_command_message(message)
        return
    
    # Get streak info
    streak = get_user_streak(uid)
    
    base_text = (
        f"🎖️ Your Stats:\n"
        f"⭐ Level: {level}\n"
        f"📊 XP: {xp}\n"
        f"#️⃣ Rank: {rank}\n"
        f"🔥 Check-in Streak: {streak} days\n\n"
        f"💡 Use /checkin daily and be active to earn more XP!"
    )
    response = format_response_with_username(message, base_text)
    sent_message = await message.reply(response)
    await delete_command_message(message)

@dp.message(Command('leaderboard', 'lb', 'top'))
async def cmd_leaderboard(message: types.Message):
    """Show XP leaderboard"""
    if not await should_process_message(message):
        return
    
    top_users = db.get_leaderboard(10)
    if not top_users:
        base_response = "Leaderboard is empty."
        response = format_response_with_username(message, base_response)
        sent_message = await message.reply(response)
        await delete_command_message(message)
        return

    lines = ["🏆 Top 10 Leaderboard:\n"]
    pos = 1
    for user_id, username, first_name, xp in top_users:
        display_name = f"@{username}" if username else (first_name or f"User {user_id}")
        level = xp_to_level(xp)
        lines.append(f"{pos}. {display_name} — Level {level} ({xp} XP)")
        pos += 1

    lines.append("\n💡 Earn XP via /checkin and being the most active user!")
    base_response = '\n'.join(lines)
    response = format_response_with_username(message, base_response)
    sent_message = await message.reply(response)
    await delete_command_message(message)

@dp.message(Command('active', 'activity'))
async def cmd_active(message: types.Message):
    """Show today's most active users in this chat"""
    if not await should_process_message(message):
        return
    
    today_key = get_today_key()
    chat_id = message.chat.id
    
    top_users = db.get_daily_activity(today_key, chat_id, limit=10)
    
    if not top_users:
        base_response = "No activity recorded today yet. Start chatting!"
        response = format_response_with_username(message, base_response)
        sent_message = await message.reply(response)
        await delete_command_message(message)
        return
    
    lines = [f"Today's Most Active Users (Top 10):\n"]
    pos = 1
    for user_id, username, first_name, message_count in top_users:
        display_name = f"@{username}" if username else (first_name or f"User {user_id}")
        lines.append(f"{pos}. {display_name} — {message_count} messages")
        pos += 1
    
    lines.append(f"\n🎯 Top 3 get XP rewards tomorrow: {ACTIVE_USER_XP[0]}/{ACTIVE_USER_XP[1]}/{ACTIVE_USER_XP[2]} XP")
    base_response = '\n'.join(lines)
    response = format_response_with_username(message, base_response)
    sent_message = await message.reply(response)
    await delete_command_message(message)

@dp.message(Command('profile', 'me'))
async def cmd_profile(message: types.Message):
    """Show user profile"""
    if not await should_process_message(message):
        return
    
    user_id = message.from_user.id
    user_profile = db.get_user_profile(user_id)
    
    if not user_profile:
        base_response = "You don't have a profile yet. Send a message or use /checkin to get started!"
        response = format_response_with_username(message, base_response)
        sent_message = await message.reply(response)
        await delete_command_message(message)
        return
    
    username, first_name, xp, streak, last_checkin = user_profile
    level = xp_to_level(xp)
    xp, rank = get_xp_and_rank(user_id)
    
    display_name = f"@{username}" if username else (first_name or f"User {user_id}")
    
    base_text = (
        f"👤 Profile: {display_name}\n"
        f"⭐ Level: {level}\n"
        f"📊 XP: {xp}\n"
        f"#️⃣ Rank: {rank if rank else 'N/A'}\n"
        f"🔥 Check-in Streak: {streak} days\n"
        f"📅 Last Check-in: {last_checkin if last_checkin else 'Never'}\n\n"
        f"💡 Use /checkin daily to maintain your streak!"
    )
    response = format_response_with_username(message, base_text)
    sent_message = await message.reply(response)
    await delete_command_message(message)

@dp.message(Command('debug'))
async def cmd_debug(message: types.Message):
    """Debug command to check bot status - Only for admin in private chat"""
    # Only allow admin in private chat for debug
    if not await is_user_admin(message.from_user.id):
        await message.reply("❌ This command is only available for the bot admin.")
        return
    
    if message.chat.type != 'private':
        await message.reply("❌ Please use this command in private chat with the bot.")
        return
    
    chat_type = message.chat.type
    chat_id = message.chat.id
    user_id = message.from_user.id
    is_allowed_group = ALLOWED_GROUP_ID and chat_id == ALLOWED_GROUP_ID
    is_owner = await is_user_admin(user_id)
    
    admin_list = ", ".join([str(uid) for uid in OWNER_IDS]) if OWNER_IDS else "None"
    
    debug_info = (
        f"🤖 Bot Debug Info (Admin):\n"
        f"👤 Your ID: {user_id}\n"
        f"💬 Chat Type: {chat_type}\n"
        f"🆔 Chat ID: {chat_id}\n"
        f"✅ Allowed Group ID: {ALLOWED_GROUP_ID}\n"
        f"🔧 In Allowed Group: {is_allowed_group}\n"
        f"📝 Should Process: {await should_process_message(message)}\n\n"
        f"🛠️ Admin commands available in private chat."
    )
    
    await message.reply(debug_info)

@dp.message()
async def handle_message(message: types.Message):
    """Track messages for most active user competition but don't award XP for commands"""
    if not await should_process_message(message):
        return
    
    if message.from_user.is_bot:
        return
    
    # Check if this is a command message (with or without bot username) - if so, don't award XP
    if message.text:
        # Check for commands with bot username (e.g., /command@bot_username)
        bot_username = (await bot.get_me()).username
        command_patterns = [
            message.text.startswith('/'),
            f"@{bot_username}" in message.text
        ]
        
        if any(command_patterns):
            return
    
    # Award XP for regular messaging (1 XP per message)
    award_xp(message.from_user, 1, "message activity")
    
    # Track message for daily activity
    track_daily_message(message.from_user, message.chat.id)

@dp.message(Command('web'))
async def cmd_web(message: types.Message):
    """Show web dashboard information"""
    if not await should_process_message(message):
        return
    
    web_url = f"http://localhost:{WEB_PORT}"
    base_response = (
        f"🌐 **Web Dashboard Available!** 🌐\n\n"
        f"📊 **Live Leaderboard:** {web_url}\n"
        f"🔗 **API Endpoint:** {web_url}/api/leaderboard\n"
        f"📈 **Stats API:** {web_url}/api/stats\n\n"
        f"🔄 *Auto-refreshes every 10 seconds*"
    )
    response = format_response_with_username(message, base_response)
    sent_message = await message.reply(response, parse_mode='Markdown')
    await delete_command_message(message)

# Admin commands
async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.is_chat_admin() or member.status == 'creator'
    except Exception:
        return False

@dp.message(Command('weekly_report'))
async def cmd_weekly_report(message: types.Message):
    """Get weekly report by date - Admin only"""
    if not await is_user_admin(message.from_user.id):
        await message.reply("❌ This command is only available for the bot admin.")
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        # Show available weeks
        available_weeks = db.get_available_weeks()
        if not available_weeks:
            await message.reply("❌ No weekly reports available yet.")
            return
        
        weeks_list = "\n".join([f"- {week}" for week in available_weeks[:10]])  # Show last 10 weeks
        await message.reply(
            f"📅 Available Weekly Reports:\n{weeks_list}\n\n"
            f"Usage: /weekly_report YYYY-MM-DD\n"
            f"Example: /weekly_report {available_weeks[0]}"
        )
        return
    
    week_date = parts[1]
    report = db.get_weekly_report(week_date)
    
    if not report:
        await message.reply(f"❌ No weekly report found for {week_date}")
        return
    
    # Format the report
    lines = [f"📊 **Weekly Report - {week_date}**\n"]
    lines.append(f"👥 Total Participants: {report['total_participants']}")
    lines.append(f"🏆 Total XP Distributed: {report['total_xp_distributed']:,}\n")
    lines.append("**Top Winners:**")
    
    for winner in report['winners_data'][:5]:  # Show top 5 in message
        medal = "🥇" if winner['rank'] == 1 else "🥈" if winner['rank'] == 2 else "🥉" if winner['rank'] == 3 else f"{winner['rank']}."
        lines.append(f"{medal} {winner['display_name']} - Level {winner['level']} ({winner['xp']} XP)")
    
    if len(report['winners_data']) > 5:
        lines.append(f"\n... and {len(report['winners_data']) - 5} more winners")
    
    lines.append(f"\n📅 Report generated: {report['created_at']}")
    
    await message.reply('\n'.join(lines), parse_mode='Markdown')

@dp.message(Command('list_weekly_reports'))
async def cmd_list_weekly_reports(message: types.Message):
    """List all weekly reports - Admin only"""
    if not await is_user_admin(message.from_user.id):
        await message.reply("❌ This command is only available for the bot admin.")
        return
    
    reports = db.get_all_weekly_reports()
    
    if not reports:
        await message.reply("❌ No weekly reports available yet.")
        return
    
    lines = ["📅 **All Weekly Reports:**\n"]
    
    for report in reports[:15]:  # Show last 15 reports
        top_winner = report['winners_data'][0] if report['winners_data'] else {}
        winner_name = top_winner.get('display_name', 'N/A') if top_winner else 'N/A'
        lines.append(
            f"• {report['week_start_date']} - "
            f"👥 {report['total_participants']} users - "
            f"🏆 {winner_name} - "
            f"⭐ {report['total_xp_distributed']:,} XP"
        )
    
    if len(reports) > 15:
        lines.append(f"\n... and {len(reports) - 15} more reports")
    
    lines.append(f"\nUse /weekly_report YYYY-MM-DD to view full report")
    
    await message.reply('\n'.join(lines), parse_mode='Markdown')
    
@dp.message(Command('set_role_threshold'))
async def cmd_set_role_threshold(message: types.Message):
    """Set role threshold - Admin only in private chat"""
    if not await is_user_admin(message.from_user.id):
        await message.reply("❌ This command is only available for the bot admin.")
        return
    
    if message.chat.type != 'private':
        await message.reply("❌ Please use this command in private chat with the bot.")
        return
    
    if not ALLOWED_GROUP_ID:
        await message.reply("❌ ALLOWED_GROUP_ID is not configured.")
        return
        
    parts = message.text.split()
    if len(parts) != 3:
        await message.reply('Usage: /set_role_threshold <role_name> <level>')
        return
    
    role = parts[1]
    try:
        level = int(parts[2])
    except ValueError:
        await message.reply('Level must be an integer.')
        return
    
    set_role_threshold(ALLOWED_GROUP_ID, role, level)
    await message.reply(f'✅ Set role threshold in group {ALLOWED_GROUP_ID}: {role} -> level {level}')

@dp.message(Command('list_role_thresholds'))
async def cmd_list_role_thresholds(message: types.Message):
    """List all role thresholds - Admin only in private chat"""
    if not await is_user_admin(message.from_user.id):
        await message.reply("❌ This command is only available for the bot admin.")
        return
    
    if message.chat.type != 'private':
        await message.reply("❌ Please use this command in private chat with the bot.")
        return
    
    if not ALLOWED_GROUP_ID:
        await message.reply("❌ ALLOWED_GROUP_ID is not configured.")
        return
        
    thresholds = get_role_thresholds(ALLOWED_GROUP_ID)
    
    if not thresholds:
        await message.reply("No role thresholds configured for the group.")
        return
    
    lines = [f"Role thresholds for group {ALLOWED_GROUP_ID}:"]
    for role, level in thresholds.items():
        lines.append(f"- {role}: level {level}")
    
    await message.reply('\n'.join(lines))

@dp.message(Command('remove_role_threshold'))
async def cmd_remove_role_threshold(message: types.Message):
    """Remove role threshold - Admin only in private chat"""
    if not await is_user_admin(message.from_user.id):
        await message.reply("❌ This command is only available for the bot admin.")
        return
    
    if message.chat.type != 'private':
        await message.reply("❌ Please use this command in private chat with the bot.")
        return
    
    if not ALLOWED_GROUP_ID:
        await message.reply("❌ ALLOWED_GROUP_ID is not configured.")
        return
        
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply('Usage: /remove_role_threshold <role_name>')
        return
    
    role = parts[1]
    remove_role_threshold(ALLOWED_GROUP_ID, role)
    await message.reply(f'✅ Removed role threshold for: {role} from group {ALLOWED_GROUP_ID}')

async def show_bot_stats(callback: CallbackQuery):
    """Show bot statistics with proper refresh handling"""
    try:
        with sqlite3.connect("xp_bot.db") as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_xp = conn.execute("SELECT SUM(xp) FROM users").fetchone()[0] or 0
            avg_xp = conn.execute("SELECT AVG(xp) FROM users").fetchone()[0] or 0
            weekly_reports = conn.execute("SELECT COUNT(*) FROM weekly_reports").fetchone()[0]
        
        # Add timestamp to make content unique on each refresh
        current_time = datetime.now(timezone.utc).strftime('%H:%M:%S')
        
        stats_text = (
            "📈 **Bot Statistics**\n\n"
            f"👥 **Total Users:** {total_users}\n"
            f"🏆 **Total XP:** {total_xp:,}\n"
            f"📊 **Average XP:** {avg_xp:.1f}\n"
            f"📅 **Weekly Reports:** {weekly_reports}\n"
            f"🔧 **Bot Uptime:** Running\n"
            f"🌐 **Web Dashboard:** Active\n\n"
            f"**Configuration:**\n"
            f"• Allowed Group: {ALLOWED_GROUP_ID or 'Not set'}\n"
            f"• GitHub Sync: {'✅ Active' if GITHUB_TOKEN else '❌ Inactive'}\n"
            f"• Web Server: ✅ Active\n\n"
            f"🕒 _Last updated: {current_time}_"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_stats")],
                [InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_back_main")]
            ]
        )
        
        try:
            await callback.message.edit_text(
                stats_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            await callback.answer("✅ Statistics refreshed")
        except Exception as e:
            if "message is not modified" in str(e):
                # If content is the same, just show a confirmation
                await callback.answer("✅ Statistics are already up to date")
            else:
                raise e
    
    except Exception as e:
        logger.error(f"Error loading statistics: {e}")
        error_text = (
            f"❌ **Error loading statistics:** {str(e)}\n\n"
            f"🕒 _Error occurred: {datetime.now(timezone.utc).strftime('%H:%M:%S')}_"
        )
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Retry", callback_data="admin_stats")],
                [InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_back_main")]
            ]
        )
        
        try:
            await callback.message.edit_text(
                error_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        except Exception as edit_error:
            if "message is not modified" not in str(edit_error):
                await callback.answer(f"❌ Error: {str(e)[:50]}...", show_alert=True)

async def safe_edit_message(message, new_text, reply_markup=None, parse_mode='Markdown'):
    """Safely edit a message, handling 'message not modified' errors"""
    try:
        await message.edit_text(
            new_text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        return True
    except Exception as e:
        if "message is not modified" in str(e):
            return False  # No change needed
        else:
            raise e  # Re-raise other errors
            
async def show_debug_info(callback: CallbackQuery):
    """Show debug information with proper Markdown escaping"""
    admin_list = ", ".join([str(uid) for uid in OWNER_IDS]) if OWNER_IDS else "None"
    bot_username = (await bot.get_me()).username
    
    # Use code formatting to avoid Markdown parsing issues
    debug_info = (
        "🔧 **Debug Information**\n\n"
        f"👤 **Your ID:** `{callback.from_user.id}`\n"
        f"✅ **Is Admin:** `{await is_user_admin(callback.from_user.id)}`\n"
        f"🤖 **Bot Username:** `@{bot_username}`\n\n"
        f"**Environment:**\n"
        f"• ALLOWED_GROUP_ID: `{ALLOWED_GROUP_ID}`\n"
        f"• WEB_PORT: `{WEB_PORT}`\n"
        f"• LOG_LEVEL: `{LOG_LEVEL}`\n\n"
        f"**Features:**\n"
        f"• Database: ✅ Connected\n"
        f"• Web Server: ✅ Running\n"
        f"• GitHub Sync: {'✅ Active' if GITHUB_TOKEN else '❌ Inactive'}\n"
        f"• Weekly Reset: ✅ Scheduled"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_debug")],
            [InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_back_main")]
        ]
    )
    
    try:
        await callback.message.edit_text(
            debug_info,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
    except Exception as e:
        # Fallback without Markdown if there's still an issue
        debug_info_plain = (
            "🔧 Debug Information\n\n"
            f"👤 Your ID: {callback.from_user.id}\n"
            f"✅ Is Admin: {await is_user_admin(callback.from_user.id)}\n"
            f"🤖 Bot Username: @{bot_username}\n\n"
            f"Environment:\n"
            f"• ALLOWED_GROUP_ID: {ALLOWED_GROUP_ID}\n"
            f"• WEB_PORT: {WEB_PORT}\n"
            f"• LOG_LEVEL: {LOG_LEVEL}\n\n"
            f"Features:\n"
            f"• Database: ✅ Connected\n"
            f"• Web Server: ✅ Running\n"
            f"• GitHub Sync: {'✅ Active' if GITHUB_TOKEN else '❌ Inactive'}\n"
            f"• Weekly Reset: ✅ Scheduled"
        )
        await callback.message.edit_text(
            debug_info_plain,
            reply_markup=keyboard,
            parse_mode=None  # Disable Markdown
        )
   
# Startup
async def on_startup():
    """Start background tasks on bot startup"""
    # Start daily reset task
    asyncio.create_task(daily_reset_task())
    
    # Start weekly reset task
    asyncio.create_task(weekly_reset_task())
    db.create_weekly_reports_table()
    
    # Start GitHub sync
    start_github_sync()
    
    # Start simple HTTP server in a separate thread
    simple_server_thread = threading.Thread(target=start_simple_server, daemon=True)
    simple_server_thread.start()
    
    # Log registered handlers
    logger.info(f"Registered message handlers: {len(dp.message.handlers)}")
    for handler in dp.message.handlers:
        logger.info(f"Handler: {handler}")
    
    logger.info("✅ Daily reset task started")
    logger.info("✅ Weekly reset task started")
    logger.info("✅ GitHub sync started")
    logger.info("✅ Simple HTTP server started")
    logger.info("✅ Bot handlers registered")

async def main():
    # Start background tasks
    await on_startup()
    
    logger.info("Starting bot polling...")
    
    try:
        # Start both web server and bot polling
        web_task = asyncio.create_task(start_web_server())
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot polling failed: {e}")
    finally:
        await bot.session.close()

if __name__ == '__main__':
    logger.info('Starting Telegram XP Bot with Web Dashboard...')
    asyncio.run(main())








