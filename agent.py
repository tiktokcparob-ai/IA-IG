import os
import sqlite3
import json
import time
import threading
import asyncio
from datetime import datetime
from telebot import TeleBot
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from instagrapi import Client as InstaClient
from groq import Groq
from croniter import croniter

# ==========================================
# 1. إعداد المتغيرات البيئية
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TG_API_ID = os.getenv("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH")

if not all([BOT_TOKEN, GROQ_API_KEY, TG_API_ID, TG_API_HASH]):
    raise ValueError("Missing essential environment variables.")

# ==========================================
# 2. تهيئة قاعدة البيانات (SQLite)
# ==========================================
DB_PATH = "koda7.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # جدول الجلسات
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (platform TEXT PRIMARY KEY, session_data TEXT)''')
    # جدول المهام الفورية
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, action TEXT, args TEXT, status TEXT)''')
    # جدول المهام المجدولة (cron)
    c.execute('''CREATE TABLE IF NOT EXISTS cron_jobs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, cron_expr TEXT, platform TEXT, action TEXT, args TEXT)''')
    # جدول حالات الانتظار (لرموز التحقق وكلمات المرور)
    c.execute('''CREATE TABLE IF NOT EXISTS pending
                 (user_id TEXT PRIMARY KEY, state TEXT, data TEXT)''')
    # جدول سجلات النظام (Logs)
    c.execute('''CREATE TABLE IF NOT EXISTS logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, level TEXT, message TEXT)''')
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False, fetchall=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(query, params)
    result = None
    if fetch:
        result = c.fetchone()
    elif fetchall:
        result = c.fetchall()
    else:
        conn.commit()
    conn.close()
    return result

def log_event(level, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_execute("INSERT INTO logs (timestamp, level, message) VALUES (?, ?, ?)", (timestamp, level, message))
    print(f"[{level}] {message}")

init_db()

# ==========================================
# 3. محرك الذكاء الاصطناعي (Groq)
# ==========================================
class AIEngine:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)
        self.model = "llama3-70b-8192"

    def understand(self, text):
        prompt = f"""
        أنت وكيل تحليل أوامر. قم بتحليل الأمر التالي واستخرج المعلومات بصيغة JSON فقط بدون أي نص إضافي.
        المنصات المدعومة: instagram, telegram, system
        الأفعال المدعومة لـ instagram: login, post_photo, post_video, interact_stories
        الأفعال المدعومة لـ telegram: login, send_message
        الأفعال المدعومة لـ system: add_cron, status
        
        الأمر: "{text}"
        
        الصيغة المطلوبة (مثال):
        {{"platform": "instagram", "action": "login", "args": {{"username": "x", "password": "y"}}}}
        {{"platform": "system", "action": "add_cron", "args": {{"cron": "0 9 * * *", "task_text": "انشر صورة"}}}}
        """
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            log_event("ERROR", f"Groq Understanding Error: {str(e)}")
            return None

    def generate_reply(self, text):
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "أنت KODA-7، وكيل ذكاء اصطناعي تتحدث العربية الفصحى وتنفذ أوامر الأتمتة."},
                    {"role": "user", "content": text}
                ],
                temperature=0.7
            )
            return response.choices[0].message.content
        except Exception as e:
            return "حدث خطأ أثناء معالجة الرد."

# ==========================================
# 4. مدير منصة Instagram
# ==========================================
class InstagramBot:
    def __init__(self):
        self.cl = InstaClient()
        self.load_session()

    def load_session(self):
        session_row = db_execute("SELECT session_data FROM sessions WHERE platform='instagram'", fetch=True)
        if session_row:
            try:
                self.cl.set_settings(json.loads(session_row['session_data']))
                log_event("INFO", "Instagram session loaded.")
            except Exception as e:
                log_event("ERROR", f"Failed to load IG session: {str(e)}")

    def save_session(self):
        session_data = json.dumps(self.cl.get_settings())
        db_execute("INSERT OR REPLACE INTO sessions (platform, session_data) VALUES ('instagram', ?)", (session_data,))

    def login(self, username, password):
        try:
            self.cl.login(username, password)
            self.save_session()
            return True, "تم تسجيل الدخول بنجاح إلى إنستغرام وحفظ الجلسة."
        except Exception as e:
            return False, f"فشل تسجيل الدخول: {str(e)}"

    def post_photo(self, path, caption):
        try:
            media = self.cl.photo_upload(path, caption)
            return True, f"تم نشر الصورة بنجاح. المعرف: {media.pk}"
        except Exception as e:
            return False, f"فشل نشر الصورة: {str(e)}"

    def interact_stories(self, count=5):
        try:
            user_id = self.cl.user_id
            feed = self.cl.user_following(user_id, amount=count)
            interacted = 0
            for user in feed.values():
                stories = self.cl.user_stories(user.pk)
                if stories:
                    self.cl.story_like(stories[0].pk)
                    interacted += 1
            return True, f"تم التفاعل مع {interacted} ستوري."
        except Exception as e:
            return False, f"فشل التفاعل مع الستوريات: {str(e)}"

# ==========================================
# 5. مدير منصة Telegram (Telethon)
# ==========================================
class TelegramClientBot:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.client = TelegramClient('koda_tg_session', int(TG_API_ID), TG_API_HASH, loop=self.loop)
        
    def _run_async(self, coro):
        asyncio.set_event_loop(self.loop)
        return self.loop.run_until_complete(coro)

    def send_code(self, phone):
        async def _send():
            await self.client.connect()
            result = await self.client.send_code_request(phone)
            return result.phone_code_hash
        try:
            hash_code = self._run_async(_send())
            return True, hash_code
        except Exception as e:
            return False, str(e)

    def sign_in(self, phone, code, phone_hash):
        async def _sign():
            await self.client.connect()
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=phone_hash)
        try:
            self._run_async(_sign())
            return True, "تم تسجيل الدخول إلى تيليجرام بنجاح."
        except SessionPasswordNeededError:
            return False, "2FA_REQUIRED"
        except Exception as e:
            return False, str(e)

    def sign_in_2fa(self, password):
        async def _sign_2fa():
            await self.client.connect()
            await self.client.sign_in(password=password)
        try:
            self._run_async(_sign_2fa())
            return True, "تم تجاوز 2FA وتسجيل الدخول."
        except Exception as e:
            return False, str(e)

    def send_message(self, target, text):
        async def _send_msg():
            await self.client.connect()
            await self.client.send_message(target, text)
        try:
            self._run_async(_send_msg())
            return True, "تم إرسال الرسالة عبر تيليجرام."
        except Exception as e:
            return False, f"فشل الإرسال: {str(e)}"

# ==========================================
# 6. الوكيل الرئيسي KODA-7
# ==========================================
class KODA7Agent:
    def __init__(self):
        self.bot = TeleBot(BOT_TOKEN)
        self.ai = AIEngine()
        self.ig = InstagramBot()
        self.tg_client = TelegramClientBot()
        self.setup_handlers()

    def setup_handlers(self):
        @self.bot.message_handler(func=lambda message: True)
        def handle_message(message):
            user_id = str(message.chat.id)
            text = message.text

            # 1. التحقق من وجود حالة انتظار (Pending State)
            pending = db_execute("SELECT * FROM pending WHERE user_id=?", (user_id,), fetch=True)
            if pending:
                self.process_pending_state(user_id, text, pending)
                return

            # 2. تحليل الأمر الطبيعي
            self.bot.reply_to(message, "⏳ جاري التحليل...")
            parsed = self.ai.understand(text)
            
            if not parsed:
                reply = self.ai.generate_reply(text)
                self.bot.reply_to(message, reply)
                return

            platform = parsed.get("platform")
            action = parsed.get("action")
            args = parsed.get("args", {})

            # 3. توجيه الأمر فوراً
            self.execute_command(user_id, platform, action, args, message)

    def process_pending_state(self, user_id, text, pending_row):
        state = pending_row['state']
        data = json.loads(pending_row['data'])

        if state == "TG_WAIT_CODE":
            phone = data['phone']
            phone_hash = data['phone_hash']
            success, result = self.tg_client.sign_in(phone, text, phone_hash)
            
            if success:
                db_execute("DELETE FROM pending WHERE user_id=?", (user_id,))
                self.bot.send_message(user_id, "✅ " + result)
            elif result == "2FA_REQUIRED":
                db_execute("UPDATE pending SET state=?, data=? WHERE user_id=?", 
                           ("TG_WAIT_PASSWORD", json.dumps({"phone": phone}), user_id))
                self.bot.send_message(user_id, "🔒 الحساب محمي بخطوتين. أرسل كلمة المرور:")
            else:
                db_execute("DELETE FROM pending WHERE user_id=?", (user_id,))
                self.bot.send_message(user_id, f"❌ فشل تسجيل الدخول: {result}")

        elif state == "TG_WAIT_PASSWORD":
            success, result = self.tg_client.sign_in_2fa(text)
            db_execute("DELETE FROM pending WHERE user_id=?", (user_id,))
            if success:
                self.bot.send_message(user_id, "✅ " + result)
            else:
                self.bot.send_message(user_id, f"❌ فشل تسجيل الدخول: {result}")

    def execute_command(self, user_id, platform, action, args, original_message=None):
        response_msg = ""
        
        if platform == "system":
            if action == "add_cron":
                cron_expr = args.get("cron")
                task_text = args.get("task_text")
                # استخدام الذكاء لتحليل الأمر الداخلي
                inner_parsed = self.ai.understand(task_text)
                if inner_parsed:
                    db_execute("INSERT INTO cron_jobs (cron_expr, platform, action, args) VALUES (?, ?, ?, ?)",
                               (cron_expr, inner_parsed.get("platform"), inner_parsed.get("action"), json.dumps(inner_parsed.get("args", {}))))
                    response_msg = f"📌 تم إضافة المهمة المجدولة بنجاح: {cron_expr}"
                else:
                    response_msg = "❌ لم أتمكن من فهم المهمة المجدولة."
                    
        elif platform == "instagram":
            if action == "login":
                success, msg = self.ig.login(args.get("username"), args.get("password"))
                response_msg = ("✅ " if success else "❌ ") + msg
            elif action == "post_photo":
                success, msg = self.ig.post_photo(args.get("path"), args.get("caption", ""))
                response_msg = ("✅ " if success else "❌ ") + msg
            elif action == "interact_stories":
                success, msg = self.ig.interact_stories(args.get("count", 5))
                response_msg = ("✅ " if success else "❌ ") + msg
                
        elif platform == "telegram":
            if action == "login":
                phone = args.get("phone")
                success, result = self.tg_client.send_code(phone)
                if success:
                    db_execute("INSERT OR REPLACE INTO pending (user_id, state, data) VALUES (?, ?, ?)",
                               (user_id, "TG_WAIT_CODE", json.dumps({"phone": phone, "phone_hash": result})))
                    response_msg = "📲 تم إرسال رمز التحقق إلى تيليجرام. يرجى إرساله هنا الآن:"
                else:
                    response_msg = f"❌ فشل إرسال الرمز: {result}"
            elif action == "send_message":
                success, msg = self.tg_client.send_message(args.get("target"), args.get("text"))
                response_msg = ("✅ " if success else "❌ ") + msg

        if original_message:
            self.bot.reply_to(original_message, response_msg)
        elif CHAT_ID:
            self.bot.send_message(CHAT_ID, response_msg)

    # ==========================================
    # 7. نظام المهام المجدولة (Cron Worker)
    # ==========================================
    def cron_worker(self):
        log_event("INFO", "Cron worker started.")
        while True:
            now = datetime.now()
            jobs = db_execute("SELECT * FROM cron_jobs", fetchall=True)
            for job in jobs:
                cron = croniter(job['cron_expr'], now)
                # إذا كانت المهمة مستحقة في هذه الدقيقة
                if abs(cron.get_prev(datetime).timestamp() - now.timestamp()) < 60:
                    log_event("INFO", f"Executing Cron Job {job['id']}")
                    self.execute_command(CHAT_ID, job['platform'], job['action'], json.loads(job['args']))
            time.sleep(60)

    def start(self):
        log_event("INFO", "KODA-7 Agent starting...")
        if CHAT_ID:
            self.bot.send_message(CHAT_ID, "🚀 KODA-7 بدأ العمل الآن. ينتظر أوامرك.")
        
        # تشغيل الـ Cron في Thread منفصل
        threading.Thread(target=self.cron_worker, daemon=True).start()
        
        # تشغيل البوت
        self.bot.infinity_polling()

if __name__ == "__main__":
    agent = KODA7Agent()
    agent.start()
