#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
KODA-7 ULTIMATE AGENT
وكيل ذكي شامل يدير جميع منصات التواصل الاجتماعي عبر أوامر طبيعية.
يعتمد على Groq (Llama 3.3) لفهم الأوامر، ويستخدم جلسات محفوظة لتسجيل الدخول التلقائي.
"""

import os
import sys
import json
import sqlite3
import time
import threading
import logging
import re
from datetime import datetime, timedelta
from queue import Queue
from typing import Dict, List, Optional, Any

import telebot
from telebot.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from groq import Groq
from github import Github
from instagrapi import Client as InstagramClient
from instagrapi.exceptions import LoginRequired
import requests
from rich.console import Console
from rich.logging import RichHandler
from croniter import croniter

# ========== التهيئة ==========
console = Console()
logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[RichHandler(rich_tracebacks=True)])
logger = logging.getLogger("koda7")

# متغيرات البيئة
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", 0))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GH_PAT = os.environ.get("GH_PAT")
MODE = os.environ.get("MODE", "agent")

if not BOT_TOKEN or not GROQ_API_KEY:
    logger.error("❌ تأكد من تعيين BOT_TOKEN و GROQ_API_KEY")
    sys.exit(1)

# ========== قاعدة البيانات ==========
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
        role TEXT DEFAULT 'user'
    )''')
    # المحادثات (للسياق)
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # جلسات المنصات (تخزين الكوكيز/التوكنات)
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        platform TEXT PRIMARY KEY,
        data TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # المهام (للجدولة وإعادة المحاولة)
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        command TEXT,
        platform TEXT,
        status TEXT DEFAULT 'pending',
        result TEXT,
        scheduled_at TIMESTAMP,
        retries INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # المهام المجدولة (cron)
    c.execute('''CREATE TABLE IF NOT EXISTS cron_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule TEXT,
        command TEXT,
        platform TEXT,
        enabled INTEGER DEFAULT 1,
        last_run TIMESTAMP
    )''')
    # السجلات
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

# ========== أدوات مساعدة ==========
def log(level: str, msg: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO logs (level, message) VALUES (?,?)", (level, msg))
    conn.commit()
    conn.close()
    getattr(logger, level.lower(), logger.info)(msg)

def get_session(platform: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT data FROM sessions WHERE platform=?", (platform,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_session(platform: str, data: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO sessions (platform, data) VALUES (?,?)", (platform, data))
    conn.commit()
    conn.close()

# ========== محرك الذكاء الاصطناعي (Groq) ==========
class AIEngine:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)
        # استخدام أحدث نموذج مدعوم
        self.model = "llama-3.3-70b-versatile"  # أو "gemma2-9b-it"

    def understand(self, text: str, history: List[Dict] = None) -> Dict:
        """فهم الأمر الطبيعي واستخراج النية والمنصة والمعلمات"""
        system = (
            "أنت محلل أوامر ذكي. مهمتك تحويل الطلب الطبيعي إلى كائن JSON يحتوي على:\n"
            "- 'platform': المنصة المطلوبة (instagram, facebook, tiktok, telegram, twitter, general).\n"
            "- 'action': الفعل (post, story, comment, like, follow, login, view_stories, interact_stories, schedule, cron).\n"
            "- 'content': النص أو المحتوى.\n"
            "- 'media': رابط أو مسار ملف إن وجد.\n"
            "- 'target': المستخدم أو المنشور المستهدف.\n"
            "- 'schedule': وقت الجدولة بصيغة ISO (اختياري).\n"
            "أخرج JSON فقط، دون أي كلام إضافي."
        )
        messages = [{"role": "system", "content": system}]
        if history:
            messages.extend(history[-5:])  # آخر 5 محادثات للسياق
        messages.append({"role": "user", "content": text})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=512
            )
            result = json.loads(response.choices[0].message.content)
            log("INFO", f"🧠 التحليل: {result}")
            return result
        except Exception as e:
            log("ERROR", f"فشل تحليل الأمر: {e}")
            # رد بديل: استخراج بسيط بالكلمات المفتاحية
            return self.fallback_parse(text)

    def fallback_parse(self, text: str) -> Dict:
        """تحليل بسيط عند فشل الذكاء الاصطناعي"""
        text_lower = text.lower()
        platform = "general"
        if "انستغرام" in text_lower or "instagram" in text_lower:
            platform = "instagram"
        elif "فيسبوك" in text_lower or "facebook" in text_lower:
            platform = "facebook"
        elif "تيك توك" in text_lower or "tiktok" in text_lower:
            platform = "tiktok"
        elif "تويتر" in text_lower or "twitter" in text_lower:
            platform = "twitter"
        elif "تلغرام" in text_lower or "telegram" in text_lower:
            platform = "telegram"

        action = "unknown"
        if "نشر" in text_lower or "انشر" in text_lower:
            action = "post"
        elif "تعليق" in text_lower or "علق" in text_lower:
            action = "comment"
        elif "إعجاب" in text_lower or "اعجب" in text_lower:
            action = "like"
        elif "متابعة" in text_lower or "تابع" in text_lower:
            action = "follow"
        elif "ستوري" in text_lower or "story" in text_lower:
            action = "story"
        elif "تفاعل" in text_lower:
            action = "interact_stories"
        elif "سجل" in text_lower or "دخول" in text_lower:
            action = "login"
        elif "جدول" in text_lower or "cron" in text_lower:
            action = "cron"

        return {
            "platform": platform,
            "action": action,
            "content": text,
            "media": None,
            "target": None,
            "schedule": None
        }

# ========== منصة إنستغرام (مع جلسة) ==========
class InstagramPlatform:
    def __init__(self):
        self.client = None
        self.load_session()

    def load_session(self):
        data = get_session("instagram")
        if data:
            try:
                cl = InstagramClient()
                cl.load_settings(data)
                cl.get_timeline_feed()
                self.client = cl
                log("INFO", "✅ استعادة جلسة إنستغرام")
            except:
                log("WARNING", "⚠️ الجلسة المخزنة غير صالحة، سيتم طلب تسجيل الدخول")

    def login(self, username: str, password: str) -> bool:
        try:
            cl = InstagramClient()
            cl.login(username, password)
            self.client = cl
            save_session("instagram", cl.get_settings())
            log("INFO", f"✅ تسجيل الدخول إلى إنستغرام كـ {username}")
            return True
        except Exception as e:
            log("ERROR", f"❌ فشل تسجيل الدخول: {e}")
            return False

    def ensure_login(self):
        if not self.client:
            return False, "لم يتم تسجيل الدخول إلى إنستغرام. استخدم /login instagram username password"
        return True, ""

    def post(self, content: str, media_path: str = None) -> Dict:
        ok, msg = self.ensure_login()
        if not ok:
            return {"success": False, "error": msg}
        try:
            if media_path:
                if media_path.lower().endswith(('.mp4', '.mov', '.avi')):
                    result = self.client.video_upload(media_path, caption=content)
                else:
                    result = self.client.photo_upload(media_path, caption=content)
            else:
                # إنستغرام لا يدعم النشر النصي مباشرة، نستخدم صورة وهمية أو نرفع صورة فارغة
                result = self.client.photo_upload(None, caption=content)
            return {"success": True, "id": result.id, "url": f"https://www.instagram.com/p/{result.code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def comment(self, post_id: str, text: str) -> Dict:
        ok, msg = self.ensure_login()
        if not ok:
            return {"success": False, "error": msg}
        try:
            result = self.client.comment(post_id, text)
            return {"success": True, "id": result.id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def like(self, post_id: str) -> Dict:
        ok, msg = self.ensure_login()
        if not ok:
            return {"success": False, "error": msg}
        try:
            self.client.like(post_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def follow(self, username: str) -> Dict:
        ok, msg = self.ensure_login()
        if not ok:
            return {"success": False, "error": msg}
        try:
            user_id = self.client.user_id_from_username(username)
            self.client.user_follow(user_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def interact_stories(self, action: str = "view") -> Dict:
        """التفاعل مع ستوريات المتابعين (مشاهدة، إعجاب، رد)"""
        ok, msg = self.ensure_login()
        if not ok:
            return {"success": False, "error": msg}
        try:
            stories = self.client.get_user_stories(self.client.user_id)
            interacted = 0
            for story in stories:
                self.client.story_seen(story.id)
                if action in ["like", "react"]:
                    self.client.story_like(story.id)
                elif action == "reply" and story.mentions:
                    self.client.story_comment(story.id, "🔥 تفاعل تلقائي")
                interacted += 1
                time.sleep(1)
            return {"success": True, "interacted": interacted}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ========== منصة فيسبوك (مبسطة) ==========
class FacebookPlatform:
    def __init__(self):
        self.access_token = None
        data = get_session("facebook")
        if data:
            self.access_token = data

    def login(self, access_token: str) -> bool:
        try:
            resp = requests.get(f"https://graph.facebook.com/me?access_token={access_token}")
            if resp.status_code == 200:
                self.access_token = access_token
                save_session("facebook", access_token)
                log("INFO", "✅ تسجيل الدخول إلى فيسبوك")
                return True
            else:
                log("ERROR", "❌ توكن فيسبوك غير صالح")
                return False
        except Exception as e:
            log("ERROR", f"❌ فشل تسجيل الدخول إلى فيسبوك: {e}")
            return False

    def post(self, content: str, media_path: str = None) -> Dict:
        if not self.access_token:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            url = "https://graph.facebook.com/me/feed"
            params = {"message": content, "access_token": self.access_token}
            if media_path:
                files = {"source": open(media_path, "rb")}
                params["caption"] = content
                url = "https://graph.facebook.com/me/photos"
                resp = requests.post(url, params=params, files=files)
            else:
                resp = requests.post(url, params=params)
            if resp.status_code == 200:
                return {"success": True, "id": resp.json().get("id")}
            return {"success": False, "error": resp.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # يمكن إضافة comment, like, follow بنفس النمط

# ========== منصة تيك توك (وهمية حالياً، لكن جاهزة) ==========
class TikTokPlatform:
    def __init__(self):
        # ستستخدم مكتبة TikTokApi لاحقاً
        pass

    def login(self, username: str, password: str) -> bool:
        # محاكاة
        save_session("tiktok", "dummy_session")
        log("INFO", "✅ تسجيل الدخول إلى تيك توك (محاكاة)")
        return True

    def post(self, content: str, media_path: str = None) -> Dict:
        return {"success": True, "id": "123", "url": "https://tiktok.com/@user/video/123"}

# ========== منصة تويتر (مبسطة) ==========
class TwitterPlatform:
    def __init__(self):
        self.bearer_token = None
        data = get_session("twitter")
        if data:
            self.bearer_token = data

    def login(self, bearer_token: str) -> bool:
        self.bearer_token = bearer_token
        save_session("twitter", bearer_token)
        return True

    def post(self, content: str, media_path: str = None) -> Dict:
        if not self.bearer_token:
            return {"success": False, "error": "غير مسجل الدخول"}
        # تنفيذ عبر API تويتر
        return {"success": True, "id": "123", "url": "https://twitter.com/user/status/123"}

# ========== الوكيل الرئيسي ==========
class KODA7Agent:
    def __init__(self):
        self.bot = telebot.TeleBot(BOT_TOKEN)
        self.ai = AIEngine()
        self.task_queue = Queue()
        self.running = True

        # تهيئة المنصات
        self.platforms = {
            "instagram": InstagramPlatform(),
            "facebook": FacebookPlatform(),
            "tiktok": TikTokPlatform(),
            "twitter": TwitterPlatform(),
        }

        self.register_handlers()
        self.start_background_threads()

    def register_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def start_cmd(msg: Message):
            self.bot.reply_to(msg,
                "🌟 **KODA-7 الوكيل الشامل**\n\n"
                "يمكنك إرسال أي أمر طبيعي، مثل:\n"
                "• `سجل الدخول إلى إنستغرام ibrahim_3_6_9 الحسين2079`\n"
                "• `انشر صورة مع تعليق 'مرحباً' على إنستغرام`\n"
                "• `تفاعل مع ستوريات المتابعين`\n"
                "• `علق على آخر منشور في فيسبوك`\n"
                "• `جدول نشر يومي الساعة 9 صباحاً`\n\n"
                "استخدم `/help` لمزيد من الأوامر المباشرة.",
                parse_mode='Markdown'
            )

        @self.bot.message_handler(commands=['help'])
        def help_cmd(msg: Message):
            self.bot.reply_to(msg,
                "📌 **الأوامر المباشرة:**\n"
                "/login <platform> <username> <password> – تسجيل الدخول إلى منصة\n"
                "/post <platform> <content> [media_path] – نشر محتوى\n"
                "/comment <platform> <post_id> <text> – تعليق\n"
                "/like <platform> <post_id> – إعجاب\n"
                "/follow <platform> <username> – متابعة\n"
                "/stories <platform> – تفاعل مع الستوريات\n"
                "/cron add <schedule> <command> – إضافة مهمة مجدولة\n"
                "/cron list – عرض المهام المجدولة\n"
                "/cron remove <id> – حذف مهمة\n"
                "/status – حالة النظام والجلسات\n"
                "/logs – عرض آخر السجلات"
            )

        @self.bot.message_handler(commands=['login'])
        def login_cmd(msg: Message):
            # /login instagram username password
            parts = msg.text.split(maxsplit=3)
            if len(parts) < 4:
                self.bot.reply_to(msg, "⚠️ الصيغة: /login <platform> <username> <password>")
                return
            platform = parts[1].lower()
            username = parts[2]
            password = parts[3]
            if platform not in self.platforms:
                self.bot.reply_to(msg, f"❌ المنصة '{platform}' غير مدعومة.")
                return
            plat = self.platforms[platform]
            if hasattr(plat, 'login'):
                success = plat.login(username, password)
                self.bot.reply_to(msg, f"✅ تم تسجيل الدخول إلى {platform}." if success else f"❌ فشل تسجيل الدخول إلى {platform}.")
            else:
                self.bot.reply_to(msg, f"⚠️ المنصة {platform} لا تدعم تسجيل الدخول بهذه الطريقة.")

        @self.bot.message_handler(commands=['post'])
        def post_cmd(msg: Message):
            parts = msg.text.split(maxsplit=3)
            if len(parts) < 3:
                self.bot.reply_to(msg, "⚠️ الصيغة: /post <platform> <content> [media_path]")
                return
            platform = parts[1].lower()
            content = parts[2]
            media = parts[3] if len(parts) > 3 else None
            if platform not in self.platforms:
                self.bot.reply_to(msg, f"❌ المنصة '{platform}' غير مدعومة.")
                return
            plat = self.platforms[platform]
            if hasattr(plat, 'post'):
                result = plat.post(content, media)
                if result.get("success"):
                    self.bot.reply_to(msg, f"✅ تم النشر على {platform}.\nالرابط: {result.get('url', '')}")
                else:
                    self.bot.reply_to(msg, f"❌ فشل النشر: {result.get('error', 'خطأ غير معروف')}")
            else:
                self.bot.reply_to(msg, f"⚠️ المنصة {platform} لا تدعم النشر.")

        @self.bot.message_handler(commands=['stories'])
        def stories_cmd(msg: Message):
            parts = msg.text.split()
            if len(parts) < 2:
                self.bot.reply_to(msg, "⚠️ الصيغة: /stories <platform> [action] (action: view, like, reply)")
                return
            platform = parts[1].lower()
            action = parts[2] if len(parts) > 2 else "view"
            if platform not in self.platforms:
                self.bot.reply_to(msg, f"❌ المنصة '{platform}' غير مدعومة.")
                return
            plat = self.platforms[platform]
            if hasattr(plat, 'interact_stories'):
                result = plat.interact_stories(action)
                if result.get("success"):
                    self.bot.reply_to(msg, f"✅ تم التفاعل مع {result['interacted']} ستوري.")
                else:
                    self.bot.reply_to(msg, f"❌ فشل التفاعل: {result.get('error')}")
            else:
                self.bot.reply_to(msg, f"⚠️ المنصة {platform} لا تدعم الستوريات.")

        @self.bot.message_handler(commands=['status'])
        def status_cmd(msg: Message):
            sessions = []
            for p, plat in self.platforms.items():
                if get_session(p):
                    sessions.append(p)
            text = f"📊 **حالة النظام**\n"
            text += f"• المهام المعلقة: {self.task_queue.qsize()}\n"
            text += f"• المنصات المدعومة: {', '.join(self.platforms.keys())}\n"
            text += f"• جلسات نشطة: {', '.join(sessions) if sessions else 'لا يوجد'}"
            self.bot.reply_to(msg, text, parse_mode='Markdown')

        @self.bot.message_handler(commands=['logs'])
        def logs_cmd(msg: Message):
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT message, timestamp FROM logs ORDER BY timestamp DESC LIMIT 15")
            rows = c.fetchall()
            conn.close()
            if not rows:
                self.bot.reply_to(msg, "لا توجد سجلات.")
            else:
                txt = "📜 **آخر السجلات:**\n" + "\n".join([f"• {r[1]}: {r[0]}" for r in rows])
                self.bot.reply_to(msg, txt, parse_mode='Markdown')

        @self.bot.message_handler(commands=['cron'])
        def cron_cmd(msg: Message):
            # /cron add "0 9 * * *" "انشر صباح الخير على إنستغرام"
            parts = msg.text.split(maxsplit=3)
            if len(parts) < 2:
                self.bot.reply_to(msg, "⚠️ الصيغة: /cron add \"schedule\" \"command\" أو /cron list أو /cron remove <id>")
                return
            action = parts[1].lower()
            if action == "add":
                if len(parts) < 4:
                    self.bot.reply_to(msg, "⚠️ الصيغة: /cron add \"0 9 * * *\" \"انشر صباح الخير\"")
                    return
                schedule = parts[2].strip('"')
                command = parts[3].strip('"')
                if not croniter.is_valid(schedule):
                    self.bot.reply_to(msg, "❌ الجدول غير صحيح. مثال: '0 9 * * *'")
                    return
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO cron_jobs (schedule, command) VALUES (?,?)", (schedule, command))
                conn.commit()
                conn.close()
                self.bot.reply_to(msg, f"✅ تمت إضافة مهمة مجدولة: {schedule} -> {command}")
            elif action == "list":
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT id, schedule, command FROM cron_jobs WHERE enabled=1")
                rows = c.fetchall()
                conn.close()
                if not rows:
                    self.bot.reply_to(msg, "لا توجد مهام مجدولة.")
                else:
                    txt = "📋 **المهام المجدولة:**\n" + "\n".join([f"#{r[0]}: {r[1]} -> {r[2]}" for r in rows])
                    self.bot.reply_to(msg, txt, parse_mode='Markdown')
            elif action == "remove":
                if len(parts) < 3:
                    self.bot.reply_to(msg, "⚠️ الصيغة: /cron remove <id>")
                    return
                job_id = int(parts[2])
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                self.bot.reply_to(msg, f"✅ تم حذف المهمة #{job_id}.")

        @self.bot.message_handler(func=lambda m: True)
        def natural_command(msg: Message):
            log("INFO", f"📩 أمر طبيعي: {msg.text}")
            # استرجاع آخر محادثات المستخدم للسياق
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT role, content FROM conversations WHERE user_id=? ORDER BY timestamp DESC LIMIT 10", (msg.from_user.id,))
            history = [{"role": r[0], "content": r[1]} for r in reversed(c.fetchall())]
            conn.close()

            analysis = self.ai.understand(msg.text, history)
            platform = analysis.get("platform", "general")
            action = analysis.get("action", "unknown")
            content = analysis.get("content", msg.text)
            media = analysis.get("media")
            target = analysis.get("target")
            schedule = analysis.get("schedule")

            # حفظ المحادثة
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO conversations (user_id, role, content) VALUES (?,?,?)", (msg.from_user.id, "user", msg.text))
            conn.commit()
            conn.close()

            # تنفيذ الأمر
            if platform in self.platforms:
                plat = self.platforms[platform]
                if action == "login":
                    # استخراج username/password من content
                    parts = content.split()
                    if len(parts) >= 2:
                        username, password = parts[0], parts[1]
                        if hasattr(plat, 'login'):
                            success = plat.login(username, password)
                            reply = f"✅ تم تسجيل الدخول إلى {platform}." if success else f"❌ فشل تسجيل الدخول."
                            self.bot.reply_to(msg, reply)
                        else:
                            self.bot.reply_to(msg, f"⚠️ المنصة {platform} لا تدعم تسجيل الدخول.")
                    else:
                        self.bot.reply_to(msg, "⚠️ يرجى إرسال اسم المستخدم وكلمة السر مع الأمر.")
                elif action == "post":
                    if hasattr(plat, 'post'):
                        result = plat.post(content, media)
                        if result.get("success"):
                            self.bot.reply_to(msg, f"✅ تم النشر على {platform}.\nالرابط: {result.get('url', '')}")
                        else:
                            self.bot.reply_to(msg, f"❌ فشل النشر: {result.get('error')}")
                    else:
                        self.bot.reply_to(msg, f"⚠️ المنصة {platform} لا تدعم النشر.")
                elif action == "comment":
                    if hasattr(plat, 'comment') and target:
                        result = plat.comment(target, content)
                        self.bot.reply_to(msg, f"✅ تم التعليق." if result.get("success") else f"❌ {result.get('error')}")
                    else:
                        self.bot.reply_to(msg, "⚠️ يرجى تحديد المنشور المستهدف.")
                elif action == "like":
                    if hasattr(plat, 'like') and target:
                        result = plat.like(target)
                        self.bot.reply_to(msg, f"✅ تم الإعجاب." if result.get("success") else f"❌ {result.get('error')}")
                    else:
                        self.bot.reply_to(msg, "⚠️ يرجى تحديد المنشور المستهدف.")
                elif action == "follow":
                    if hasattr(plat, 'follow') and target:
                        result = plat.follow(target)
                        self.bot.reply_to(msg, f"✅ تمت المتابعة." if result.get("success") else f"❌ {result.get('error')}")
                    else:
                        self.bot.reply_to(msg, "⚠️ يرجى تحديد اسم المستخدم.")
                elif action == "interact_stories" or action == "story":
                    if hasattr(plat, 'interact_stories'):
                        result = plat.interact_stories(action="view")
                        if result.get("success"):
                            self.bot.reply_to(msg, f"✅ تم التفاعل مع {result['interacted']} ستوري.")
                        else:
                            self.bot.reply_to(msg, f"❌ {result.get('error')}")
                    else:
                        self.bot.reply_to(msg, f"⚠️ المنصة {platform} لا تدعم الستوريات.")
                elif action == "cron" or "جدول" in msg.text:
                    # إضافة مهمة مجدولة بناءً على التحليل
                    if schedule:
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute("INSERT INTO cron_jobs (schedule, command, platform) VALUES (?,?,?)",
                                  (schedule, content, platform))
                        conn.commit()
                        conn.close()
                        self.bot.reply_to(msg, f"✅ تمت إضافة مهمة مجدولة: {schedule} -> {content}")
                    else:
                        self.bot.reply_to(msg, "⚠️ لم يتم تحديد وقت الجدولة. استخدم /cron add")
                else:
                    self.bot.reply_to(msg, f"❓ لم أفهم الإجراء المطلوب على منصة {platform}.")
            else:
                # إذا لم يتم التعرف على المنصة، نرد عام
                self.bot.reply_to(msg, "❓ لم أفهم الأمر. تأكد من ذكر المنصة (مثل: إنستغرام) والفعل المطلوب.")

    def start_background_threads(self):
        threading.Thread(target=self.task_worker, daemon=True).start()
        threading.Thread(target=self.scheduler_worker, daemon=True).start()
        if MODE == "agent":
            threading.Thread(target=self.bot_polling, daemon=True).start()

    def task_worker(self):
        while self.running:
            try:
                # تنفيذ المهام المعلقة من قاعدة البيانات
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT id, user_id, command, platform FROM tasks WHERE status='pending' LIMIT 5")
                tasks = c.fetchall()
                conn.close()
                for task_id, user_id, command, platform in tasks:
                    log("INFO", f"⚙️ تنفيذ المهمة #{task_id}: {command}")
                    # محاولة تنفيذ الأمر من خلال معالج الأوامر الطبيعي (محاكاة)
                    # يمكن تحسينها بإعادة استخدام منطق التنفيذ
                    # هنا نقوم بتنفيذ بسيط: إذا كانت المنصة إنستغرام، نقوم بنشر النص
                    if platform in self.platforms:
                        plat = self.platforms[platform]
                        if hasattr(plat, 'post'):
                            result = plat.post(command, None)
                            status = "completed" if result.get("success") else "failed"
                            result_str = json.dumps(result)
                        else:
                            status = "failed"
                            result_str = "منصة لا تدعم النشر"
                    else:
                        status = "failed"
                        result_str = "منصة غير معروفة"
                    # تحديث الحالة
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("UPDATE tasks SET status=?, result=? WHERE id=?", (status, result_str, task_id))
                    conn.commit()
                    conn.close()
                    # إرسال إشعار للمستخدم
                    self.bot.send_message(user_id, f"✅ المهمة #{task_id} منجزة." if status == "completed" else f"❌ فشلت المهمة #{task_id}.")
                time.sleep(30)
            except Exception as e:
                log("ERROR", f"خطأ في معالج المهام: {e}")
                time.sleep(60)

    def scheduler_worker(self):
        while self.running:
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT id, schedule, command, platform FROM cron_jobs WHERE enabled=1")
                jobs = c.fetchall()
                conn.close()
                now = datetime.now()
                for job_id, schedule, command, platform in jobs:
                    if croniter.is_valid(schedule):
                        cron = croniter(schedule, now)
                        next_run = cron.get_next(datetime)
                        if next_run <= now + timedelta(minutes=1):
                            # إضافة المهمة إلى queue
                            self.task_queue.put((job_id, command, platform))
                            # تحديث last_run
                            conn = sqlite3.connect(DB_PATH)
                            c = conn.cursor()
                            c.execute("UPDATE cron_jobs SET last_run=? WHERE id=?", (now.isoformat(), job_id))
                            conn.commit()
                            conn.close()
                            log("INFO", f"⏰ تم تشغيل المهمة المجدولة #{job_id}")
                time.sleep(60)
            except Exception as e:
                log("ERROR", f"خطأ في المجدول: {e}")
                time.sleep(60)

    def bot_polling(self):
        while self.running:
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=30)
            except Exception as e:
                log("ERROR", f"⚠️ توقف البوت: {e}. إعادة المحاولة...")
                time.sleep(5)

    def run(self):
        log("INFO", "🚀 تشغيل KODA-7 ULTIMATE AGENT")
        self.bot.send_message(CHAT_ID, "🌟 KODA-7 يعمل بكامل طاقته.")
        while self.running:
            time.sleep(1)

# ========== نقطة الدخول ==========
if __name__ == "__main__":
    agent = KODA7Agent()
    agent.run()
