#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
KODA-7 ULTIMATE AGENT
وكيل ذكي يتحدث معه المستخدم وينفذ الأوامر على منصات التواصل الاجتماعي.
يدعم: تليجرام، إنستغرام، تيك توك (قابل للتوسع).
"""

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
from telethon import TelegramClient, events
from telethon.tl.functions.messages import SendMessageRequest
from rich.console import Console
from rich.logging import RichHandler

# ========== الإعدادات ==========
console = Console()
logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[RichHandler(rich_tracebacks=True)])
logger = logging.getLogger("koda7")

# المتغيرات البيئية
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", 0))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GH_PAT = os.environ.get("GH_PAT")  # للـ GitHub

if not BOT_TOKEN or not GROQ_API_KEY:
    logger.error("❌ تأكد من تعيين BOT_TOKEN و GROQ_API_KEY")
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
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

# ========== أدوات الذكاء الاصطناعي (Groq) ==========
class AIEngine:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)
        self.model = "llama3-70b-8192"  # ✅ النموذج المدعوم حالياً

    def understand_command(self, text: str) -> dict:
        """تحويل الأمر الطبيعي إلى هيكل مفهوم (JSON)"""
        system = (
            "أنت محلل أوامر. قم بتحليل طلب المستخدم واستخراج: "
            "المنصة (telegram, instagram, tiktok, facebook), "
            "الفعل (login, post, story, comment, like, follow, send_message), "
            "المحتوى أو المستهدف، وأي معلمات أخرى (مثل اسم المستخدم، كلمة السر، مسار الملف). "
            "أخرج النتيجة بصيغة JSON."
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

# ========== مدير تليجرام (باستخدام Telethon) ==========
class TelegramManager:
    def __init__(self):
        self.client = None
        self.session_data = self.load_session()
        self.api_id = int(os.environ.get("TG_API_ID", 0))  # يجب تعيينه
        self.api_hash = os.environ.get("TG_API_HASH", "")

    def load_session(self) -> str:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT session_data FROM sessions WHERE platform='telegram'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def save_session(self, data: str):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO sessions (platform, session_data) VALUES (?,?)", ("telegram", data))
        conn.commit()
        conn.close()

    def login(self, phone: str, password: str = None) -> bool:
        """تسجيل الدخول إلى تليجرام وحفظ الجلسة"""
        if not self.api_id or not self.api_hash:
            logger.error("❌ يجب تعيين TG_API_ID و TG_API_HASH في المتغيرات البيئية")
            return False

        self.client = TelegramClient('session_' + phone, self.api_id, self.api_hash)
        try:
            # محاولة استعادة الجلسة المخزنة
            if self.session_data:
                self.client.start(phone=phone, password=password)
                # اختبار الاتصال
                self.client.get_me()
                logger.info("✅ تم استعادة جلسة تليجرام")
                self.save_session(self.client.session.save())
                return True
        except:
            pass

        # تسجيل دخول جديد
        try:
            self.client.start(phone=phone, password=password)
            self.save_session(self.client.session.save())
            logger.info(f"✅ تم تسجيل الدخول إلى تليجرام كـ {phone}")
            return True
        except Exception as e:
            logger.error(f"❌ فشل تسجيل الدخول: {e}")
            return False

    def send_message(self, username: str, text: str) -> dict:
        """إرسال رسالة إلى مستخدم أو قناة"""
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            entity = self.client.get_entity(username)
            self.client.send_message(entity, text)
            return {"success": True, "to": username}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ========== مدير إنستغرام (باستخدام instagrapi) ==========
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
        try:
            cl = Client()
            if self.session_data:
                try:
                    cl.load_settings(self.session_data)
                    cl.get_timeline_feed()
                    self.client = cl
                    logger.info("✅ تم استعادة جلسة إنستغرام")
                    return True
                except:
                    logger.info("⚠️ الجلسة المخزنة غير صالحة، نعيد تسجيل الدخول")
            cl.login(username, password)
            self.client = cl
            self.save_session(cl.get_settings())
            logger.info(f"✅ تم تسجيل الدخول إلى إنستغرام كـ {username}")
            return True
        except Exception as e:
            logger.error(f"❌ فشل تسجيل الدخول: {e}")
            return False

    def post_video(self, video_path: str, caption: str) -> dict:
        """نشر فيديو مع تعليق"""
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            result = self.client.video_upload(video_path, caption=caption)
            return {"success": True, "id": result.id, "url": f"https://www.instagram.com/p/{result.code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def interact_with_stories(self, action: str = "view") -> dict:
        """التفاعل مع ستوريات المتابعين"""
        if not self.client:
            return {"success": False, "error": "غير مسجل الدخول"}
        try:
            stories = self.client.get_user_stories(self.client.user_id)
            interacted = 0
            for story in stories:
                self.client.story_seen(story.id)
                if action == "like":
                    self.client.story_like(story.id)
                elif action == "reply":
                    self.client.story_comment(story.id, "🔥 رد تلقائي")
                interacted += 1
                time.sleep(2)
            return {"success": True, "interacted": interacted}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ========== الوكيل الرئيسي ==========
class KODA7Agent:
    def __init__(self):
        self.bot = telebot.TeleBot(BOT_TOKEN)
        self.ai = AIEngine()
        self.ig = InstagramManager()
        self.tg = TelegramManager()
        self.task_queue = Queue()
        self.running = True
        self.register_handlers()
        self.start_background_threads()

    def register_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def start_cmd(msg: Message):
            self.bot.reply_to(msg, "👋 أنا KODA-7، وكيلك الذكي.\n"
                                   "أرسل لي أوامر طبيعية مثل:\n"
                                   "- 'سجل الدخول إلى تليجرام'\n"
                                   "- 'انشر فيديو على إنستغرام'\n"
                                   "- 'أرسل رسالة إلى @username'\n"
                                   "- 'تفاعل مع ستوريات'\n"
                                   "سأنفذها فوراً.")

        @self.bot.message_handler(commands=['login'])
        def login_cmd(msg: Message):
            # /login telegram phone password? (اختياري)
            parts = msg.text.split()
            if len(parts) < 3:
                self.bot.reply_to(msg, "الصيغة: /login <telegram|instagram> <username/phone> <password>")
                return
            platform = parts[1].lower()
            if platform == "telegram":
                phone = parts[2]
                password = parts[3] if len(parts) > 3 else None
                # هنا يجب أن نطلب من المستخدم إرسال رمز التحقق (سيتم التعامل معه عبر الدالة الطبيعية)
                self.bot.reply_to(msg, f"📱 جاري تسجيل الدخول إلى تليجرام برقم {phone}...")
                # نطلق عملية تسجيل الدخول في خيط منفصل لتجنب حظر البوت
                threading.Thread(target=self._telegram_login, args=(msg.chat.id, phone, password), daemon=True).start()
            elif platform == "instagram":
                if len(parts) < 4:
                    self.bot.reply_to(msg, "الصيغة: /login instagram <username> <password>")
                    return
                success = self.ig.login(parts[2], parts[3])
                if success:
                    self.bot.reply_to(msg, "✅ تم تسجيل الدخول إلى إنستغرام وحفظ الجلسة.")
                else:
                    self.bot.reply_to(msg, "❌ فشل تسجيل الدخول. تحقق من البيانات.")
            else:
                self.bot.reply_to(msg, f"⚠️ المنصة '{platform}' غير مدعومة حالياً.")

        def _telegram_login(self, chat_id, phone, password):
            """معالجة تسجيل الدخول إلى تليجرام مع طلب الكود إن لزم"""
            try:
                # هذه الدالة معقدة بسبب تفاعل الكود، سنبسطها بطلب الكود من المستخدم
                self.bot.send_message(chat_id, f"📲 أرسل لي رمز التحقق الذي سيصلك على رقم {phone} (أو كلمة المرور إن وجدت).")
                # سننتظر رد المستخدم (سيتم التعامل معه في دالة natural_command)
                # سنخزن الحالة مؤقتاً
                # نستخدم قاعدة بيانات مؤقتة (يمكن تحسينها)
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS pending_logins (chat_id INTEGER, phone TEXT, password TEXT)")
                c.execute("INSERT OR REPLACE INTO pending_logins (chat_id, phone, password) VALUES (?,?,?)", (chat_id, phone, password))
                conn.commit()
                conn.close()
            except Exception as e:
                self.bot.send_message(chat_id, f"❌ خطأ: {e}")

        @self.bot.message_handler(func=lambda m: True)
        def natural_command(msg: Message):
            """معالجة الأوامر الطبيعية"""
            text = msg.text
            chat_id = msg.chat.id
            logger.info(f"📩 أمر طبيعي: {text}")

            # التحقق من وجود عملية تسجيل دخول معلقة
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT phone, password FROM pending_logins WHERE chat_id=?", (chat_id,))
            pending = c.fetchone()
            conn.close()

            if pending:
                # المستخدم يرسل رمز التحقق
                phone, password = pending
                code = text.strip()
                # محاولة تسجيل الدخول باستخدام الكود
                try:
                    # نعيد إنشاء client مع الكود
                    from telethon import TelegramClient
                    client = TelegramClient('session_' + phone, self.tg.api_id, self.tg.api_hash)
                    client.start(phone=phone, password=password, code=code)
                    # حفظ الجلسة
                    self.tg.client = client
                    self.tg.save_session(client.session.save())
                    self.bot.reply_to(msg, "✅ تم تسجيل الدخول إلى تليجرام بنجاح وحفظ الجلسة.")
                    # حذف السجل المعلق
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("DELETE FROM pending_logins WHERE chat_id=?", (chat_id,))
                    conn.commit()
                    conn.close()
                    return
                except Exception as e:
                    self.bot.reply_to(msg, f"❌ فشل تسجيل الدخول: {e}\nأعد المحاولة أو استخدم /login مرة أخرى.")
                    return

            # إذا لم يكن هناك عملية معلقة، نستخدم الذكاء الاصطناعي لفهم الأمر
            analysis = self.ai.understand_command(text)
            platform = analysis.get("platform", "unknown").lower()
            action = analysis.get("action", "unknown").lower()
            content = analysis.get("content", "")

            # تنفيذ الأمر حسب التحليل
            if platform == "telegram":
                if action == "login":
                    self.bot.reply_to(msg, "استخدم الأمر /login telegram <رقم_الهاتف> <كلمة_السر> (اختياري)")
                elif action == "send_message":
                    # توقع وجود @username والنص
                    # سنحاول استخراج username والنص من الأمر
                    import re
                    match = re.search(r'@(\w+)', text)
                    if match:
                        username = match.group(1)
                        # باقي النص بعد @username
                        parts = text.split(f"@{username}", 1)
                        message_text = parts[1].strip() if len(parts) > 1 else ""
                        if not message_text:
                            self.bot.reply_to(msg, "أدخل النص الذي تريد إرساله.")
                            return
                        result = self.tg.send_message(username, message_text)
                        if result["success"]:
                            self.bot.reply_to(msg, f"✅ تم إرسال الرسالة إلى @{username}")
                        else:
                            self.bot.reply_to(msg, f"❌ فشل الإرسال: {result['error']}")
                    else:
                        self.bot.reply_to(msg, "استخدم الصيغة: أرسل رسالة إلى @username النص")
                else:
                    self.bot.reply_to(msg, "⚠️ الأمر غير معروف لتليجرام. استخدم: 'أرسل رسالة إلى @username'")
            elif platform == "instagram":
                if action == "login":
                    self.bot.reply_to(msg, "استخدم الأمر /login instagram <username> <password>")
                elif action == "post" or "فيديو" in text:
                    # سنطلب رابط الفيديو أو مساره
                    self.bot.reply_to(msg, "أرسل رابط الفيديو (أو ارفعه كملف) وسأقوم بنشره مع التعليق المطلوب.")
                    # هنا يمكن تحسين المعالجة
                elif action == "story" or "ستوري" in text:
                    result = self.ig.interact_with_stories(action="view")
                    if result["success"]:
                        self.bot.reply_to(msg, f"✅ تم التفاعل مع {result['interacted']} ستوري.")
                    else:
                        self.bot.reply_to(msg, f"❌ فشل: {result['error']}")
                else:
                    self.bot.reply_to(msg, "⚠️ الأمر غير معروف للإنستغرام. جرب: 'تفاعل مع ستوريات'")
            else:
                self.bot.reply_to(msg, "❓ لم أفهم الأمر. تأكد من ذكر المنصة (تليجرام، إنستغرام) والفعل المطلوب.")

    def start_background_threads(self):
        threading.Thread(target=self.task_worker, daemon=True).start()
        threading.Thread(target=self.bot_polling, daemon=True).start()

    def task_worker(self):
        while self.running:
            try:
                # تنفيذ المهام المعلقة من قاعدة البيانات (للجدولة المستقبلية)
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT id, command, platform FROM tasks WHERE status='pending' LIMIT 5")
                tasks = c.fetchall()
                conn.close()
                for task_id, command, platform in tasks:
                    logger.info(f"⚙️ تنفيذ المهمة #{task_id}: {command}")
                    # هنا يمكن إضافة منطق تنفيذ المهام المجدولة
                    # ...
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
