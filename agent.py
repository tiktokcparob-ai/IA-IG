#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
KODA-7 ULTIMATE AGENT
الوكيل الشامل الذي يدير كل منصاتك (تيليجرام، إنستغرام، فيسبوك، تيك توك، واتساب، تويتر، ...)
مع تسجيل دخول تلقائي، تنفيذ أوامر طبيعية، جدولة، وإعادة محاولة ذكية.
"""

import os
import sys
import json
import sqlite3
import time
import threading
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from queue import Queue
import requests
import telebot
from telebot.types import Message, CallbackQuery
from groq import Groq  # ✅ التصحيح: الاستيراد الصحيح
from github import Github, GithubException
from rich.console import Console
from rich.logging import RichHandler

# ========================== التهيئة ==========================
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)]
)
logger = logging.getLogger("koda-7")

# ========================== المتغيرات البيئية ==========================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", 0))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GH_PAT = os.environ.get("GH_PAT")
MODE = os.environ.get("MODE", "agent")

if not all([BOT_TOKEN, CHAT_ID, GROQ_API_KEY, GH_PAT]):
    logger.error("❌ بعض المتغيرات البيئية مفقودة!")
    sys.exit(1)

# ========================== قاعدة البيانات ==========================
DB_PATH = "koda7.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # المستخدمون
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        role TEXT DEFAULT 'user',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # المحادثات
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # المهام
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        command TEXT,
        platform TEXT,
        status TEXT DEFAULT 'pending',
        result TEXT,
        scheduled_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # جلسات المنصات (تخزين الكوكيز/التوكنات)
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        platform TEXT PRIMARY KEY,
        session_data TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # سجلات
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # المهام المجدولة
    c.execute('''CREATE TABLE IF NOT EXISTS cron_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule TEXT,
        command TEXT,
        platform TEXT,
        enabled INTEGER DEFAULT 1,
        last_run TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

# ========================== مدير الحالة ==========================
class StateManager:
    @staticmethod
    def get_user(user_id: int) -> Optional[Dict]:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"id": row[0], "username": row[1], "first_name": row[2],
                    "last_name": row[3], "role": row[4], "created_at": row[5]}
        return None

    @staticmethod
    def add_user(user_id: int, username: str, first_name: str, last_name: str = ""):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (id, username, first_name, last_name) VALUES (?,?,?,?)",
                  (user_id, username, first_name, last_name))
        conn.commit()
        conn.close()

    @staticmethod
    def add_conversation(user_id: int, role: str, content: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO conversations (user_id, role, content) VALUES (?,?,?)",
                  (user_id, role, content))
        conn.commit()
        conn.close()
        # الاحتفاظ بآخر 50 محادثة فقط
        c.execute("DELETE FROM conversations WHERE id NOT IN (SELECT id FROM conversations WHERE user_id=? ORDER BY timestamp DESC LIMIT 50)", (user_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def get_conversation_history(user_id: int, limit: int = 20) -> List[Dict]:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT role, content FROM conversations WHERE user_id=? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
        rows = c.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    @staticmethod
    def add_task(user_id: int, command: str, platform: str = "general", scheduled_at: str = None) -> int:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO tasks (user_id, command, platform, scheduled_at) VALUES (?,?,?,?)",
                  (user_id, command, platform, scheduled_at))
        task_id = c.lastrowid
        conn.commit()
        conn.close()
        return task_id

    @staticmethod
    def update_task_status(task_id: int, status: str, result: str = ""):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tasks SET status=?, result=? WHERE id=?", (status, result, task_id))
        conn.commit()
        conn.close()

    @staticmethod
    def get_pending_tasks() -> List[Dict]:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM tasks WHERE status='pending' AND (scheduled_at IS NULL OR scheduled_at <= datetime('now'))")
        rows = c.fetchall()
        conn.close()
        return [{"id": r[0], "user_id": r[1], "command": r[2], "platform": r[3],
                 "status": r[4], "result": r[5], "scheduled_at": r[6], "created_at": r[7]} for r in rows]

    @staticmethod
    def get_session(platform: str) -> Optional[str]:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT session_data FROM sessions WHERE platform=?", (platform,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    @staticmethod
    def save_session(platform: str, session_data: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO sessions (platform, session_data) VALUES (?,?)",
                  (platform, session_data))
        conn.commit()
        conn.close()

    @staticmethod
    def log(level: str, message: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO logs (level, message) VALUES (?,?)", (level, message))
        conn.commit()
        conn.close()

    @staticmethod
    def get_cron_jobs() -> List[Dict]:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM cron_jobs WHERE enabled=1")
        rows = c.fetchall()
        conn.close()
        return [{"id": r[0], "schedule": r[1], "command": r[2], "platform": r[3],
                 "enabled": r[4], "last_run": r[5]} for r in rows]

    @staticmethod
    def add_cron_job(schedule: str, command: str, platform: str = "general"):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO cron_jobs (schedule, command, platform) VALUES (?,?,?)",
                  (schedule, command, platform))
        conn.commit()
        conn.close()

    @staticmethod
    def delete_cron_job(job_id: int):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
        conn.commit()
        conn.close()

# ========================== محرك الذكاء الاصطناعي ==========================
class AIEngine:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)  # ✅ التصحيح
        self.model = "mixtral-8x7b-32768"

    def chat(self, messages: List[Dict], system_prompt: str = "You are KODA-7, an advanced AI assistant.") -> str:
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        response = self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            temperature=0.7,
            max_tokens=2048
        )
        return response.choices[0].message.content

    def generate_code(self, prompt: str) -> str:
        system = "You are an expert coder. Generate Python code that solves the task. Only output code, no explanations."
        messages = [{"role": "user", "content": prompt}]
        return self.chat(messages, system)

    def analyze(self, text: str) -> Dict:
        system = "Analyze the given text and extract: intent, entities (platform, action, parameters). Output JSON."
        messages = [{"role": "user", "content": text}]
        response = self.chat(messages, system)
        try:
            return json.loads(response)
        except:
            return {"intent": "unknown", "entities": {}, "sentiment": "neutral"}

# ========================== مدير GitHub ==========================
class GitHubManager:
    def __init__(self):
        self.g = Github(GH_PAT)
        # نأخذ اسم المستودع من متغير البيئة GITHUB_REPOSITORY أو نضبطه يدوياً
        repo_name = os.environ.get("GITHUB_REPOSITORY", "tiktokcaprob-ai/IA-IC")
        self.repo = self.g.get_repo(repo_name)

    def read_file(self, path: str) -> Optional[str]:
        try:
            content = self.repo.get_contents(path)
            return content.decoded_content.decode()
        except:
            return None

    def write_file(self, path: str, content: str, commit_msg: str = "Update by KODA-7"):
        try:
            file = self.repo.get_contents(path)
            self.repo.update_file(path, commit_msg, content, file.sha)
        except:
            self.repo.create_file(path, commit_msg, content)

    def create_issue(self, title: str, body: str) -> int:
        issue = self.repo.create_issue(title=title, body=body)
        return issue.number

# ========================== منصة أساسية للبلاجنز ==========================
class PlatformPlugin:
    """كل منصة يجب أن ترث هذه الكلاس وتنفذ دوالها"""
    name = "base"
    version = "1.0"

    def __init__(self, state_mgr, ai_engine):
        self.state = state_mgr
        self.ai = ai_engine
        self.session = None

    def login(self, credentials: Dict) -> bool:
        """تسجيل الدخول إلى المنصة باستخدام بيانات الاعتماد"""
        raise NotImplementedError

    def logout(self):
        """تسجيل الخروج"""
        raise NotImplementedError

    def post(self, content: str, media_path: str = None) -> Dict:
        """نشر محتوى (نص، صورة، فيديو)"""
        raise NotImplementedError

    def comment(self, post_id: str, text: str) -> Dict:
        """التعليق على منشور"""
        raise NotImplementedError

    def like(self, post_id: str) -> Dict:
        """الإعجاب بمنشور"""
        raise NotImplementedError

    def story(self, media_path: str) -> Dict:
        """نشر ستوري"""
        raise NotImplementedError

    def follow(self, username: str) -> Dict:
        """متابعة مستخدم"""
        raise NotImplementedError

    def get_session(self) -> str:
        """استرجاع بيانات الجلسة المخزنة"""
        return self.state.get_session(self.name)

    def save_session(self, data: str):
        """حفظ بيانات الجلسة"""
        self.state.save_session(self.name, data)

# ========================== بلاجن إنستغرام (مثال) ==========================
class InstagramPlugin(PlatformPlugin):
    name = "instagram"
    version = "1.0"

    def __init__(self, state_mgr, ai_engine):
        super().__init__(state_mgr, ai_engine)
        # سنستخدم instagrapi (يجب تثبيته)
        try:
            from instagrapi import Client
            self.Client = Client
            self.client = None
        except ImportError:
            self.Client = None
            self.client = None

    def login(self, credentials: Dict) -> bool:
        if not self.Client:
            self.state.log("ERROR", "مكتبة instagrapi غير مثبتة")
            return False
        self.client = self.Client()
        # محاولة استعادة الجلسة المخزنة
        session_data = self.get_session()
        if session_data:
            try:
                self.client.load_settings(session_data)
                self.client.get_timeline_feed()  # اختبار الجلسة
                self.state.log("INFO", "تم استعادة جلسة إنستغرام")
                return True
            except:
                self.state.log("WARNING", "الجلسة المخزنة غير صالحة، نعيد تسجيل الدخول")
        # تسجيل دخول جديد
        try:
            self.client.login(credentials["username"], credentials["password"])
            # حفظ الجلسة
            self.save_session(self.client.get_settings())
            self.state.log("INFO", f"تسجيل الدخول إلى إنستغرام كـ {credentials['username']}")
            return True
        except Exception as e:
            self.state.log("ERROR", f"فشل تسجيل الدخول إلى إنستغرام: {e}")
            return False

    def post(self, content: str, media_path: str = None) -> Dict:
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            if media_path:
                if media_path.endswith(('.mp4', '.mov')):
                    result = self.client.video_upload(media_path, caption=content)
                else:
                    result = self.client.photo_upload(media_path, caption=content)
            else:
                result = self.client.photo_upload(None, caption=content)  # منشور نصي (غير مدعوم رسمياً)
            return {"success": True, "id": result.id, "url": f"https://www.instagram.com/p/{result.code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def comment(self, post_id: str, text: str) -> Dict:
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            result = self.client.comment(post_id, text)
            return {"success": True, "id": result.id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def like(self, post_id: str) -> Dict:
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            self.client.like(post_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def story(self, media_path: str) -> Dict:
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            if media_path.endswith(('.mp4', '.mov')):
                result = self.client.video_upload_to_story(media_path)
            else:
                result = self.client.photo_upload_to_story(media_path)
            return {"success": True, "id": result.id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def follow(self, username: str) -> Dict:
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            user_id = self.client.user_id_from_username(username)
            self.client.user_follow(user_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ========================== بلاجن فيسبوك (مثال باستخدام requests) ==========================
class FacebookPlugin(PlatformPlugin):
    name = "facebook"
    version = "1.0"

    def __init__(self, state_mgr, ai_engine):
        super().__init__(state_mgr, ai_engine)
        self.access_token = None

    def login(self, credentials: Dict) -> bool:
        # نفترض أن لدينا صفحة أو تطبيق فيسبوك مع access token
        self.access_token = credentials.get("access_token")
        if not self.access_token:
            self.state.log("ERROR", "لا يوجد access_token للفيسبوك")
            return False
        # اختبار التوكن
        try:
            resp = requests.get(f"https://graph.facebook.com/me?access_token={self.access_token}")
            if resp.status_code == 200:
                self.save_session(self.access_token)
                self.state.log("INFO", "تسجيل الدخول إلى فيسبوك بنجاح")
                return True
            else:
                self.state.log("ERROR", "توكن فيسبوك غير صالح")
                return False
        except Exception as e:
            self.state.log("ERROR", f"خطأ في فيسبوك: {e}")
            return False

    def post(self, content: str, media_path: str = None) -> Dict:
        if not self.access_token:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            url = "https://graph.facebook.com/me/feed"
            params = {"message": content, "access_token": self.access_token}
            if media_path:
                # فيسبوك يتطلب تحميل الملفات بطريقة مختلفة
                files = {"source": open(media_path, "rb")}
                params["caption"] = content
                url = "https://graph.facebook.com/me/photos"
                resp = requests.post(url, params=params, files=files)
            else:
                resp = requests.post(url, params=params)
            if resp.status_code == 200:
                return {"success": True, "id": resp.json().get("id")}
            else:
                return {"success": False, "error": resp.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def comment(self, post_id: str, text: str) -> Dict:
        # مشابهة
        pass

    def like(self, post_id: str) -> Dict:
        pass

    def story(self, media_path: str) -> Dict:
        # فيسبوك يدعم الستوري عبر صفحة أو تطبيق
        pass

    def follow(self, username: str) -> Dict:
        pass

# ========================== بلاجن تيك توك (مثال) ==========================
class TikTokPlugin(PlatformPlugin):
    name = "tiktok"
    version = "1.0"

    def login(self, credentials: Dict) -> bool:
        # باستخدام مكتبة مثل TikTokApi
        self.state.log("INFO", "تسجيل الدخول إلى تيك توك (محاكاة)")
        self.save_session("dummy_session")
        return True

    def post(self, content: str, media_path: str = None) -> Dict:
        return {"success": True, "id": "123", "url": "https://tiktok.com/@user/video/123"}

    # باقي الدوال اختصاراً...

# ========================== الوكيل الرئيسي ==========================
class KODA7Agent:
    def __init__(self):
        self.bot = telebot.TeleBot(BOT_TOKEN)
        self.ai = AIEngine()
        self.gh = GitHubManager()
        self.state = StateManager()
        self.task_queue = Queue()
        self.running = True
        self.platforms = self.init_platforms()
        self.register_handlers()
        self.start_background_threads()

    def init_platforms(self) -> Dict[str, PlatformPlugin]:
        """تهيئة البلاجنز المتاحة"""
        plugins = {
            "instagram": InstagramPlugin(self.state, self.ai),
            "facebook": FacebookPlugin(self.state, self.ai),
            "tiktok": TikTokPlugin(self.state, self.ai),
            # يمكن إضافة المزيد هنا
        }
        # محاولة تحميل البلاجنز من مجلد plugins/ إذا وجد
        try:
            import importlib
            import pkgutil
            import plugins
            for loader, name, is_pkg in pkgutil.iter_modules(plugins.__path__):
                module = importlib.import_module(f"plugins.{name}")
                if hasattr(module, "Plugin"):
                    plugin = module.Plugin(self.state, self.ai)
                    plugins[plugin.name] = plugin
                    self.state.log("INFO", f"تم تحميل البلاجن {plugin.name}")
        except:
            pass
        return plugins

    def register_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def handle_start(message: Message):
            self.state.add_user(message.from_user.id, message.from_user.username,
                                message.from_user.first_name, message.from_user.last_name)
            self.bot.reply_to(message, "مرحباً! أنا KODA-7 الوكيل الشامل.\n"
                                       "يمكنك أن تطلب مني أي شيء: تسجيل الدخول إلى منصة، نشر محتوى، تعليق، متابعة، جدولة مهام، إلخ.\n"
                                       "مثال: 'سجل دخول إلى إنستغرام' ثم 'انشر فيديو باسم video.mp4 مع تعليق مرحبا'.")

        @self.bot.message_handler(commands=['status'])
        def handle_status(message: Message):
            tasks = self.state.get_pending_tasks()
            sessions = []
            for p in self.platforms.values():
                if p.get_session():
                    sessions.append(p.name)
            text = f"المهام المعلقة: {len(tasks)}\n"
            text += f"المنصات المسجلة: {', '.join(self.platforms.keys())}\n"
            text += f"جلسات نشطة: {', '.join(sessions) if sessions else 'لا يوجد'}"
            self.bot.reply_to(message, text)

        @self.bot.message_handler(commands=['login'])
        def handle_login(message: Message):
            # /login instagram username password
            parts = message.text.split(maxsplit=3)
            if len(parts) < 4:
                self.bot.reply_to(message, "الصيغة: /login <المنصة> <اسم المستخدم> <كلمة السر>")
                return
            platform_name = parts[1].lower()
            username = parts[2]
            password = parts[3]
            if platform_name not in self.platforms:
                self.bot.reply_to(message, f"المنصة {platform_name} غير مدعومة.")
                return
            plugin = self.platforms[platform_name]
            try:
                success = plugin.login({"username": username, "password": password})
                if success:
                    self.bot.reply_to(message, f"✅ تم تسجيل الدخول إلى {platform_name} بنجاح.")
                else:
                    self.bot.reply_to(message, f"❌ فشل تسجيل الدخول إلى {platform_name}.")
            except Exception as e:
                self.bot.reply_to(message, f"❌ خطأ: {str(e)}")

        @self.bot.message_handler(commands=['post'])
        def handle_post(message: Message):
            # /post instagram "نص" (مسار ملف اختياري)
            parts = message.text.split(maxsplit=2)
            if len(parts) < 3:
                self.bot.reply_to(message, "الصيغة: /post <المنصة> \"النص\" [مسار الملف]")
                return
            platform_name = parts[1].lower()
            content = parts[2].strip('"')
            media_path = None
            if len(parts) > 3:
                media_path = parts[3]
            if platform_name not in self.platforms:
                self.bot.reply_to(message, f"المنصة {platform_name} غير مدعومة.")
                return
            # تنفيذ فوري أو إضافة مهمة
            result = self.platforms[platform_name].post(content, media_path)
            if result.get("success"):
                self.bot.reply_to(message, f"✅ تم النشر على {platform_name}.\nالرابط: {result.get('url', '')}")
            else:
                self.bot.reply_to(message, f"❌ فشل النشر: {result.get('error', 'خطأ غير معروف')}")

        @self.bot.message_handler(commands=['task'])
        def handle_task(message: Message):
            # /task "أمر طبيعي" platform=instagram scheduled=2025-01-01 12:00
            # هذه المرة نستخدم الذكاء الاصطناعي لتحليل الأمر
            raw = message.text.replace('/task', '').strip()
            if not raw:
                self.bot.reply_to(message, "أرسل لي الأمر الطبيعي (مثال: انشر فيديو على إنستغرام الساعة 3 مساءً)")
                return
            # تحليل الأمر بواسطة الذكاء الاصطناعي
            analysis = self.ai.analyze(raw)
            platform = analysis.get("entities", {}).get("platform", "general")
            command = raw
            scheduled = None
            if "scheduled" in analysis["entities"]:
                scheduled = analysis["entities"]["scheduled"]
            task_id = self.state.add_task(message.from_user.id, command, platform, scheduled)
            self.task_queue.put(task_id)
            self.bot.reply_to(message, f"📌 تم إضافة المهمة #{task_id} للتنفيذ على {platform}.")

        @self.bot.message_handler(commands=['cron'])
        def handle_cron(message: Message):
            # /cron add "0 9 * * *" "نشر صباحي" platform=instagram
            args = message.text.split(maxsplit=4)
            if len(args) < 4:
                self.bot.reply_to(message, "الصيغة: /cron add \"جدول\" \"الأمر\" platform=<منصة>")
                return
            action = args[1]
            if action == "add":
                schedule = args[2].strip('"')
                command = args[3].strip('"')
                platform = "general"
                if len(args) > 4 and args[4].startswith("platform="):
                    platform = args[4].split("=")[1]
                self.state.add_cron_job(schedule, command, platform)
                self.bot.reply_to(message, f"✅ تمت إضافة مهمة مجدولة: {schedule} -> {command} (منصة: {platform})")
            elif action == "list":
                jobs = self.state.get_cron_jobs()
                if not jobs:
                    self.bot.reply_to(message, "لا توجد مهام مجدولة.")
                else:
                    txt = "المهام المجدولة:\n" + "\n".join([f"#{j['id']}: {j['schedule']} -> {j['command']} ({j['platform']})" for j in jobs])
                    self.bot.reply_to(message, txt)
            elif action == "remove":
                job_id = int(args[2])
                self.state.delete_cron_job(job_id)
                self.bot.reply_to(message, f"✅ تم حذف المهمة #{job_id}.")

        @self.bot.message_handler(commands=['logs'])
        def handle_logs(message: Message):
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT message, timestamp FROM logs ORDER BY timestamp DESC LIMIT 15")
            rows = c.fetchall()
            conn.close()
            if not rows:
                self.bot.reply_to(message, "لا توجد سجلات.")
            else:
                txt = "آخر السجلات:\n" + "\n".join([f"{r[1]}: {r[0]}" for r in rows])
                self.bot.reply_to(message, txt)

        @self.bot.message_handler(func=lambda m: True)
        def handle_natural(message: Message):
            """معالجة الأوامر الطبيعية باستخدام الذكاء الاصطناعي"""
            self.state.add_conversation(message.from_user.id, "user", message.text)
            history = self.state.get_conversation_history(message.from_user.id)
            msgs = [{"role": h["role"], "content": h["content"]} for h in history]
            system = (
                "أنت وكيل ذكي شامل يمكنه تنفيذ أي أمر يطلبه المستخدم على منصات التواصل الاجتماعي. "
                "أنت تفهم الأوامر الطبيعية مثل 'انشر فيديو على إنستغرام'، 'علق على آخر منشور في فيسبوك'، 'سجل دخول إلى تيك توك'. "
                "إذا طلب منك تسجيل الدخول، اسأل عن بيانات الاعتماد (اسم المستخدم وكلمة السر) إذا لم تكن مخزنة. "
                "إذا طلب نشر محتوى، اطلب النص ومسار الملف إن لزم. "
                "أنت تستخدم البلاجنز المتاحة لتنفيذ العمليات. "
                "يمكنك أيضاً جدولة المهام أو إضافتها إلى قائمة الانتظار."
            )
            response = self.ai.chat(msgs, system)
            self.state.add_conversation(message.from_user.id, "assistant", response)
            self.send_long_message(message.chat.id, response)

    def send_long_message(self, chat_id: int, text: str, parse_mode: str = 'HTML'):
        if len(text) <= 4096:
            self.bot.send_message(chat_id, text, parse_mode=parse_mode)
        else:
            for i in range(0, len(text), 4096):
                self.bot.send_message(chat_id, text[i:i+4096], parse_mode=parse_mode)

    def start_background_threads(self):
        threading.Thread(target=self.task_worker, daemon=True).start()
        threading.Thread(target=self.scheduler_worker, daemon=True).start()
        if MODE == "agent":
            threading.Thread(target=self.bot_polling, daemon=True).start()

    def task_worker(self):
        while self.running:
            try:
                task_id = self.task_queue.get(timeout=5)
                self.execute_task(task_id)
            except:
                pass

    def execute_task(self, task_id: int):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        task = c.fetchone()
        conn.close()
        if not task:
            return
        user_id, command, platform = task[1], task[2], task[3]
        self.state.log("INFO", f"تنفيذ المهمة #{task_id}: {command} (منصة: {platform})")
        # استخدام الذكاء الاصطناعي لتفسير الأمر وتنفيذه على المنصة المناسبة
        if platform in self.platforms:
            plugin = self.platforms[platform]
            # هنا يمكن تحليل الأمر بشكل أكثر دقة
            result = plugin.post(command, None)  # تبسيطاً، ننشر النص كمنشور
            if result.get("success"):
                self.state.update_task_status(task_id, "completed", json.dumps(result))
                self.bot.send_message(user_id, f"✅ تم تنفيذ المهمة #{task_id} بنجاح.")
            else:
                self.state.update_task_status(task_id, "failed", result.get("error", ""))
                self.bot.send_message(user_id, f"❌ فشل تنفيذ المهمة #{task_id}.")
        else:
            self.state.update_task_status(task_id, "failed", "منصة غير معروفة")
            self.bot.send_message(user_id, f"❌ المهمة #{task_id} غير مدعومة.")

    def scheduler_worker(self):
        while self.running:
            try:
                jobs = self.state.get_cron_jobs()
                now = datetime.now()
                for job in jobs:
                    # تحقق بسيط: إذا مرت أكثر من ساعة منذ آخر تنفيذ
                    last_run = datetime.fromisoformat(job['last_run']) if job['last_run'] else None
                    if not last_run or (now - last_run) > timedelta(hours=1):
                        # إضافة المهمة إلى queue
                        self.task_queue.put(job['command'])
                        # تحديث last_run
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute("UPDATE cron_jobs SET last_run=? WHERE id=?", (now.isoformat(), job['id']))
                        conn.commit()
                        conn.close()
                        self.state.log("INFO", f"تم تنفيذ المهمة المجدولة: {job['command']}")
                time.sleep(60)
            except Exception as e:
                self.state.log("ERROR", f"خطأ في المجدول: {e}")
                time.sleep(60)

    def bot_polling(self):
        while self.running:
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=30)
            except Exception as e:
                self.state.log("ERROR", f"توقف البوت: {e}. إعادة المحاولة بعد 5 ثوانٍ...")
                time.sleep(5)

    def run(self):
        self.state.log("INFO", "🚀 بدء تشغيل KODA-7 ULTIMATE AGENT")
        self.bot.send_message(CHAT_ID, "🌟 KODA-7 يعمل الآن بكامل طاقته.")
        while self.running:
            time.sleep(1)

# ========================== نقطة الدخول ==========================
if __name__ == "__main__":
    agent = KODA7Agent()
    agent.run()
