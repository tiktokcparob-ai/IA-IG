#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import sqlite3
import time
import threading
import logging
from datetime import datetime
from queue import Queue
import telebot
from telebot.types import Message
from groq import Groq
from instagrapi import Client
from instagrapi.exceptions import LoginRequired
from rich.console import Console
from rich.logging import RichHandler

# ========== الإعدادات الأساسية ==========
console = Console()
logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[RichHandler(rich_tracebacks=True)])
logger = logging.getLogger("koda7")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", 0))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not BOT_TOKEN or not GROQ_API_KEY:
    logger.error("❌ تأكد من تعيين BOT_TOKEN و GROQ_API_KEY في المتغيرات البيئية")
    sys.exit(1)

DB_PATH = "koda7.db"

# ========== قاعدة البيانات ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        platform TEXT PRIMARY KEY,
        session_data TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT,
        platform TEXT,
        status TEXT DEFAULT 'pending',
        result TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

# ========== أدوات الذكاء الاصطناعي ==========
class AIEngine:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)
        self.model = "mixtral-8x7b-32768"

    def understand_command(self, text: str) -> dict:
        """تحويل الأمر الطبيعي إلى أوامر برمجية قابلة للتنفيذ"""
        system = (
            "أنت محلل أوامر. قم بتحليل طلب المستخدم واستخراج: "
            "المنصة (instagram, telegram, facebook), الفعل (post, story, comment, like, follow), "
            "المحتوى أو المستهدف, وأي معلمات أخرى. أخرج النتيجة بصيغة JSON."
        )
        messages = [{"role": "user", "content": text}]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}] + messages,
            temperature=0.3,
            max_tokens=1024
        )
        try:
            return json.loads(response.choices[0].message.content)
        except:
            return {"platform": "unknown", "action": "unknown", "content": text}

# ========== مدير الإنستغرام (مع الجلسة) ==========
class InstagramManager:
    def __init__(self):
        self.client = None
        self.session_data = self.load_session()

    def load_session(self) -> str:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT session_data FROM sessions WHERE platform='instagram'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def save_session(self, data: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO sessions (platform, session_data) VALUES (?,?)", ("instagram", data))
        conn.commit()
        conn.close()

    def login(self, username: str, password: str) -> bool:
        """تسجيل الدخول وحفظ الجلسة"""
        try:
            cl = Client()
            # محاولة استعادة الجلسة أولاً
            if self.session_data:
                try:
                    cl.load_settings(self.session_data)
                    cl.get_timeline_feed()
                    self.client = cl
                    logger.info("✅ تم استعادة جلسة الإنستغرام")
                    return True
                except:
                    logger.info("⚠️ الجلسة المخزنة غير صالحة، نعيد تسجيل الدخول")

            # تسجيل دخول جديد
            cl.login(username, password)
            self.client = cl
            # حفظ الجلسة
            self.save_session(cl.get_settings())
            logger.info(f"✅ تم تسجيل الدخول إلى إنستغرام كـ {username}")
            return True
        except Exception as e:
            logger.error(f"❌ فشل تسجيل الدخول: {e}")
            return False

    def interact_with_stories(self, action: str = "view") -> dict:
        """التفاعل مع ستوريات المتابعين (مشاهدة، إعجاب، رد)"""
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}

        try:
            stories = self.client.get_user_stories(self.client.user_id)
            interacted = 0
            for story in stories:
                # مشاهدة الستوري
                self.client.story_seen(story.id)
                # إذا كان الأمر يتطلب إعجاب أو رد
                if action == "like" or action == "react":
                    self.client.story_like(story.id)
                elif action == "reply" and story.mentions:
                    self.client.story_comment(story.id, "🔥 رد تلقائي")
                interacted += 1
                time.sleep(2)  # تجنب الحظر
            return {"success": True, "interacted": interacted}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ========== الوكيل الرئيسي ==========
class KODA7Agent:
    def __init__(self):
        self.bot = telebot.TeleBot(BOT_TOKEN)
        self.ai = AIEngine()
        self.ig = InstagramManager()
        self.task_queue = Queue()
        self.running = True
        self.register_handlers()
        self.start_background_threads()

    def register_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def start_cmd(msg: Message):
            self.bot.reply_to(msg, "👋 مرحباً! أنا وكيلك الذكي.\n"
                                   "أرسل لي أوامر طبيعية مثل:\n"
                                   "- 'سجل دخول إلى إنستغرام' (ثم اسم المستخدم وكلمة السر)\n"
                                   "- 'تفاعل مع ستوريات المتابعين'\n"
                                   "- 'انشر صورة مع تعليق'")

        @self.bot.message_handler(commands=['login'])
        def login_cmd(msg: Message):
            # الصيغة: /login instagram username password
            parts = msg.text.split()
            if len(parts) != 4:
                self.bot.reply_to(msg, "الصيغة: /login instagram <اسم_المستخدم> <كلمة_السر>")
                return
            platform = parts[1].lower()
            if platform == "instagram":
                success = self.ig.login(parts[2], parts[3])
                if success:
                    self.bot.reply_to(msg, "✅ تم تسجيل الدخول إلى إنستغرام وحفظ الجلسة.")
                else:
                    self.bot.reply_to(msg, "❌ فشل تسجيل الدخول. تحقق من البيانات.")
            else:
                self.bot.reply_to(msg, f"⚠️ المنصة '{platform}' غير مدعومة حالياً.")

        @self.bot.message_handler(func=lambda m: True)
        def natural_command(msg: Message):
            """معالجة الأوامر الطبيعية"""
            logger.info(f"📩 أمر طبيعي: {msg.text}")
            # تحليل الأمر باستخدام الذكاء الاصطناعي
            analysis = self.ai.understand_command(msg.text)
            platform = analysis.get("platform", "unknown").lower()
            action = analysis.get("action", "unknown").lower()

            # تنفيذ الأمر حسب التحليل
            if platform == "instagram":
                if action == "story" or "تفاعل مع ستوريات" in msg.text:
                    result = self.ig.interact_with_stories(action="view")
                    if result["success"]:
                        self.bot.reply_to(msg, f"✅ تم التفاعل مع {result['interacted']} ستوري.")
                    else:
                        self.bot.reply_to(msg, f"❌ فشل التفاعل: {result['error']}")
                elif action == "like" or action == "react":
                    result = self.ig.interact_with_stories(action="like")
                    self.bot.reply_to(msg, f"✅ تم الإعجاب بـ {result['interacted']} ستوري." if result["success"] else f"❌ {result['error']}")
                else:
                    self.bot.reply_to(msg, "⚠️ الأمر غير معروف للإنستغرام. حاول: 'تفاعل مع ستوريات'")
            elif platform == "telegram":
                self.bot.reply_to(msg, "💬 أنا هنا! يمكنك استخدام الأوامر المباشرة: /start, /login")
            else:
                self.bot.reply_to(msg, "❓ لم أفهم الأمر. تأكد من ذكر المنصة (مثل: إنستغرام) والفعل المطلوب.")

    def start_background_threads(self):
        """تشغيل خلفية لمعالجة المهام"""
        threading.Thread(target=self.task_worker, daemon=True).start()
        threading.Thread(target=self.bot_polling, daemon=True).start()

    def task_worker(self):
        while self.running:
            try:
                # تنفيذ المهام المعلقة من قاعدة البيانات
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT id, command, platform FROM tasks WHERE status='pending' LIMIT 5")
                tasks = c.fetchall()
                conn.close()

                for task_id, command, platform in tasks:
                    logger.info(f"⚙️ تنفيذ المهمة #{task_id}: {command}")
                    # هنا يمكن إضافة منطق تنفيذ المهام المجدولة
                    # ...
                    # تحديث الحالة
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("UPDATE tasks SET status='completed', result='تم التنفيذ' WHERE id=?", (task_id,))
                    conn.commit()
                    conn.close()
                time.sleep(30)
            except Exception as e:
                logger.error(f"خطأ في معالج المهام: {e}")
                time.sleep(60)

    def bot_polling(self):
        while self.running:
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=30)
            except Exception as e:
                logger.error(f"⚠️ توقف البوت: {e}. إعادة المحاولة...")
                time.sleep(5)

    def run(self):
        logger.info("🚀 تشغيل KODA-7 Agent...")
        self.bot.send_message(CHAT_ID, "🌟 الوكيل يعمل الآن.")
        while self.running:
            time.sleep(1)

if __name__ == "__main__":
    agent = KODA7Agent()
    agent.run()
