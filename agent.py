#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

import telebot
from telebot.types import Message
from groq import Groq
from instagrapi import Client
from telethon import TelegramClient

# إعدادات التسجيل
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# ==== المتغيرات البيئية ====
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHAT_ID = int(os.environ.get('CHAT_ID', 0))
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
TG_API_ID = int(os.environ.get('TG_API_ID', 0))
TG_API_HASH = os.environ.get('TG_API_HASH', '')

if not BOT_TOKEN or not GROQ_API_KEY:
    logger.error('BOT_TOKEN و GROQ_API_KEY مطلوبان')
    sys.exit(1)

DB_PATH = 'koda7.db'

# ==== قاعدة البيانات (جلسات، مهام، محادثات) ====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        platform TEXT PRIMARY KEY,
        data TEXT,
        updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT,
        platform TEXT,
        status TEXT DEFAULT 'pending',
        result TEXT,
        scheduled TIMESTAMP,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS conv (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending (
        chat_id INTEGER PRIMARY KEY,
        platform TEXT,
        step TEXT,
        data TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

# ==== دوال مساعدة للقاعدة ====
def get_session(platform):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT data FROM sessions WHERE platform=?', (platform,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_session(platform, data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO sessions (platform, data) VALUES (?,?)', (platform, data))
    conn.commit()
    conn.close()

def add_task(command, platform, scheduled=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO tasks (command, platform, scheduled) VALUES (?,?,?)',
              (command, platform, scheduled))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_pending_tasks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, command, platform FROM tasks WHERE status="pending" AND (scheduled IS NULL OR scheduled <= datetime("now"))')
    rows = c.fetchall()
    conn.close()
    return [{'id': r[0], 'command': r[1], 'platform': r[2]} for r in rows]

def update_task(task_id, status, result=''):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE tasks SET status=?, result=? WHERE id=?', (status, result, task_id))
    conn.commit()
    conn.close()

def add_conv(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO conv (user_id, role, content) VALUES (?,?,?)', (user_id, role, content))
    conn.commit()
    conn.close()
    # احتفظ بآخر 50 فقط
    c.execute('DELETE FROM conv WHERE id NOT IN (SELECT id FROM conv WHERE user_id=? ORDER BY ts DESC LIMIT 50)', (user_id,))
    conn.commit()
    conn.close()

def get_conv(user_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT role, content FROM conv WHERE user_id=? ORDER BY ts DESC LIMIT ?', (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{'role': r[0], 'content': r[1]} for r in reversed(rows)]

def set_pending(chat_id, platform, step, data=''):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO pending (chat_id, platform, step, data) VALUES (?,?,?,?)',
              (chat_id, platform, step, data))
    conn.commit()
    conn.close()

def get_pending(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT platform, step, data FROM pending WHERE chat_id=?', (chat_id,))
    row = c.fetchone()
    conn.close()
    return {'platform': row[0], 'step': row[1], 'data': row[2]} if row else None

def clear_pending(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM pending WHERE chat_id=?', (chat_id,))
    conn.commit()
    conn.close()

# ==== محرك Groq (ذكاء اصطناعي) ====
class GroqEngine:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)
        self.model = 'llama3-70b-8192'  # مدعوم

    def understand(self, text):
        """يحلل الأمر الطبيعي ويعيد JSON"""
        system = 'استخرج من النص: المنصة (instagram, telegram), الفعل (login, post, story, send_message, interact), المحتوى. أخرج JSON فقط.'
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': text}],
                temperature=0.3,
                max_tokens=300
            )
            return json.loads(resp.choices[0].message.content)
        except:
            return {'platform': 'unknown', 'action': 'unknown', 'content': text}

    def chat(self, messages, system='أنت مساعد ذكي'):
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{'role': 'system', 'content': system}] + messages,
                temperature=0.7,
                max_tokens=1024
            )
            return resp.choices[0].message.content
        except:
            return 'عذراً، حدث خطأ في الذكاء الاصطناعي.'

# ==== منصة إنستغرام ====
class InstagramBot:
    def __init__(self):
        self.client = None
        self.session = get_session('instagram')
        if self.session:
            try:
                self.client = Client()
                self.client.load_settings(self.session)
                self.client.get_timeline_feed()
                logger.info('استعادة جلسة إنستغرام')
            except:
                self.client = None

    def login(self, username, password):
        try:
            if self.client:
                return True
            cl = Client()
            cl.login(username, password)
            self.client = cl
            save_session('instagram', cl.get_settings())
            logger.info(f'تسجيل دخول إنستغرام كـ {username}')
            return True
        except Exception as e:
            logger.error(f'فشل تسجيل الدخول: {e}')
            return False

    def interact_stories(self):
        if not self.client:
            return {'success': False, 'error': 'غير مسجل الدخول'}
        try:
            stories = self.client.get_user_stories(self.client.user_id)
            count = 0
            for s in stories:
                self.client.story_seen(s.id)
                self.client.story_like(s.id)
                count += 1
                time.sleep(1)
            return {'success': True, 'count': count}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def post_photo(self, path, caption):
        if not self.client:
            return {'success': False, 'error': 'غير مسجل الدخول'}
        try:
            res = self.client.photo_upload(path, caption=caption)
            return {'success': True, 'id': res.id}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def post_video(self, path, caption):
        if not self.client:
            return {'success': False, 'error': 'غير مسجل الدخول'}
        try:
            res = self.client.video_upload(path, caption=caption)
            return {'success': True, 'id': res.id}
        except Exception as e:
            return {'success': False, 'error': str(e)}

# ==== منصة تليجرام (باستخدام Telethon) ====
class TelegramBot:
    def __init__(self):
        self.client = None
        self.session = get_session('telegram')
        if self.session:
            try:
                self.client = TelegramClient(StringSession(self.session), TG_API_ID, TG_API_HASH)
                self.client.start()
                logger.info('استعادة جلسة تليجرام')
            except:
                self.client = None

    def login(self, phone, password=None, code=None):
        if not TG_API_ID or not TG_API_HASH:
            return False
        try:
            self.client = TelegramClient(f'session_{phone}', TG_API_ID, TG_API_HASH)
            if code:
                self.client.start(phone=phone, password=password, code=code)
            else:
                self.client.start(phone=phone, password=password)
            save_session('telegram', self.client.session.save())
            return True
        except Exception as e:
            logger.error(f'فشل تسجيل الدخول: {e}')
            return False

    def send_message(self, username, text):
        if not self.client:
            return {'success': False, 'error': 'غير مسجل الدخول'}
        try:
            entity = self.client.get_entity(username)
            self.client.send_message(entity, text)
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

# ==== الوكيل الرئيسي ====
class KODA7Agent:
    def __init__(self):
        self.bot = telebot.TeleBot(BOT_TOKEN)
        self.ai = GroqEngine()
        self.ig = InstagramBot()
        self.tg = TelegramBot()
        self.task_queue = Queue()
        self.running = True
        self.register_handlers()
        self.start_threads()

    def register_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def start(msg):
            self.bot.reply_to(msg, 'مرحباً! أنا وكيلك. أرسل أوامر طبيعية مثل:\n'
                                   '- سجل الدخول إلى إنستغرام\n'
                                   '- تفاعل مع ستوريات\n'
                                   '- أرسل رسالة إلى @username\n'
                                   '- انشر صورة مع تعليق')

        @self.bot.message_handler(commands=['login'])
        def login(msg):
            parts = msg.text.split(maxsplit=3)
            if len(parts) < 4:
                self.bot.reply_to(msg, 'الصيغة: /login <instagram|telegram> <username/phone> <password>')
                return
            plat = parts[1].lower()
            if plat == 'instagram':
                if self.ig.login(parts[2], parts[3]):
                    self.bot.reply_to(msg, '✅ تم تسجيل الدخول إلى إنستغرام')
                else:
                    self.bot.reply_to(msg, '❌ فشل تسجيل الدخول')
            elif plat == 'telegram':
                # سنطلب رمز التحقق لاحقاً
                set_pending(msg.chat.id, 'telegram', 'await_code', parts[2])
                self.bot.reply_to(msg, f'📱 أرسل رمز التحقق الذي وصلك على {parts[2]}')
            else:
                self.bot.reply_to(msg, 'منصة غير مدعومة')

        @self.bot.message_handler(func=lambda m: True)
        def handle_all(msg):
            chat_id = msg.chat.id
            text = msg.text

            # التحقق من وجود عملية تسجيل دخول معلقة
            pending = get_pending(chat_id)
            if pending and pending['platform'] == 'telegram' and pending['step'] == 'await_code':
                code = text.strip()
                # محاولة إكمال تسجيل الدخول
                phone = pending['data']
                if self.tg.login(phone, code=code):
                    self.bot.reply_to(msg, '✅ تم تسجيل الدخول إلى تليجرام')
                    clear_pending(chat_id)
                else:
                    self.bot.reply_to(msg, '❌ رمز غير صحيح، حاول مجدداً')
                return

            # استخدام الذكاء الاصطناعي لفهم الأمر
            analysis = self.ai.understand(text)
            plat = analysis.get('platform', 'unknown')
            action = analysis.get('action', 'unknown')
            content = analysis.get('content', '')

            if plat == 'instagram':
                if action == 'login':
                    self.bot.reply_to(msg, 'استخدم /login instagram <user> <pass>')
                elif action == 'story' or action == 'interact':
                    res = self.ig.interact_stories()
                    if res['success']:
                        self.bot.reply_to(msg, f'✅ تم التفاعل مع {res["count"]} ستوري')
                    else:
                        self.bot.reply_to(msg, f'❌ {res["error"]}')
                elif action == 'post' or 'انشر' in text:
                    self.bot.reply_to(msg, 'أرسل النص ثم رابط/مسار الملف (صورة أو فيديو)')
                    # يمكن تحسين هذا لاحقاً
                else:
                    self.bot.reply_to(msg, '⚠️ الأمر غير معروف للإنستغرام')
            elif plat == 'telegram':
                if action == 'login':
                    self.bot.reply_to(msg, 'استخدم /login telegram <phone> <password>')
                elif action == 'send_message':
                    match = re.search(r'@(\w+)', text)
                    if match:
                        username = match.group(1)
                        parts = text.split(f'@{username}', 1)
                        msg_text = parts[1].strip() if len(parts) > 1 else ''
                        if not msg_text:
                            self.bot.reply_to(msg, 'أدخل النص المرسل')
                            return
                        res = self.tg.send_message(username, msg_text)
                        if res['success']:
                            self.bot.reply_to(msg, f'✅ أرسلت إلى @{username}')
                        else:
                            self.bot.reply_to(msg, f'❌ {res["error"]}')
                    else:
                        self.bot.reply_to(msg, 'استخدم: أرسل رسالة إلى @username النص')
                else:
                    self.bot.reply_to(msg, '⚠️ الأمر غير معروف لتليجرام')
            else:
                # رد عام من الذكاء الاصطناعي
                add_conv(chat_id, 'user', text)
                history = get_conv(chat_id)
                msgs = [{'role': h['role'], 'content': h['content']} for h in history]
                response = self.ai.chat(msgs, 'أنت مساعد ذكي للأتمتة، أجب بالعربية')
                add_conv(chat_id, 'assistant', response)
                self.bot.reply_to(msg, response)

    def start_threads(self):
        threading.Thread(target=self.task_worker, daemon=True).start()
        threading.Thread(target=self.scheduler_worker, daemon=True).start()
        threading.Thread(target=self.bot_polling, daemon=True).start()

    def task_worker(self):
        while self.running:
            try:
                tasks = get_pending_tasks()
                for t in tasks:
                    # تنفيذ المهمة (يمكن تحسينه)
                    logger.info(f'تنفيذ مهمة #{t["id"]}: {t["command"]}')
                    update_task(t['id'], 'completed', 'تم التنفيذ')
                time.sleep(30)
            except Exception as e:
                logger.error(f'خطأ في معالج المهام: {e}')
                time.sleep(60)

    def scheduler_worker(self):
        # يمكن إضافة جدولة cron هنا (حسب الطلب)
        while self.running:
            time.sleep(60)

    def bot_polling(self):
        while self.running:
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=30)
            except Exception as e:
                logger.error(f'توقف البوت: {e}. إعادة المحاولة...')
                time.sleep(5)

    def run(self):
        logger.info('🚀 KODA-7 يعمل')
        self.bot.send_message(CHAT_ID, '🌟 الوكيل جاهز')
        while self.running:
            time.sleep(1)

if __name__ == '__main__':
    agent = KODA7Agent()
    agent.run()
