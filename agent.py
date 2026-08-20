#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
KODA-7 AI Agent - النظام الشامل للأتمتة والذكاء الاصطناعي
يدعم: تيليجرام، إنستغرام، فيسبوك، واتساب، تيك توك (عبر plugins)
يتحمل إعادة التشغيل، ويستكمل المهام المتقطعة.
"""

import os
import sys
import json
import sqlite3
import time
import threading
import subprocess
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, asdict
from queue import Queue, PriorityQueue
import requests
import telebot
from telebot.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, InputFile
)
from groq import Groq
from github import Github, GithubException
from rich.console import Console
from rich.table import Table
from rich.logging import RichHandler

# ========================== التهيئة الأساسية ==========================
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)]
)
logger = logging.getLogger("koda-7")

# ========================== البيئة والمتغيرات ==========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GH_PAT = os.environ["GH_PAT"]
MODE = os.environ.get("MODE", "agent")

# ========================== قاعدة البيانات (SQLite) ==========================
DB_PATH = "koda7.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        role TEXT DEFAULT 'user',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        command TEXT,
        status TEXT DEFAULT 'pending',
        result TEXT,
        platform TEXT,
        scheduled_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS plugins (
        name TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 1,
        config TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS cron_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule TEXT,
        command TEXT,
        enabled INTEGER DEFAULT 1,
        last_run TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

# ========================== مدير الحالة والمهام ==========================
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
        # حافظ على آخر 50 محادثة فقط
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
    def add_task(user_id: int, command: str, platform: str = "general", scheduled_at: str = None):
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
        return [{"id": r[0], "user_id": r[1], "command": r[2], "status": r[3],
                 "result": r[4], "platform": r[5], "scheduled_at": r[6], "created_at": r[7]} for r in rows]

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
        return [{"id": r[0], "schedule": r[1], "command": r[2], "enabled": r[3], "last_run": r[4]} for r in rows]

# ========================== محرك الذكاء الاصطناعي (Groq) ==========================
class AIEngine:
    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)
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
        system = "Analyze the given text and extract: intent, entities, sentiment. Output JSON."
        messages = [{"role": "user", "content": text}]
        response = self.chat(messages, system)
        try:
            return json.loads(response)
        except:
            return {"intent": "unknown", "entities": [], "sentiment": "neutral"}

# ========================== مدير GitHub ==========================
class GitHubManager:
    def __init__(self):
        self.g = Github(GH_PAT)
        self.repo = self.g.get_repo(os.environ.get("GITHUB_REPOSITORY", "tiktokcaprob-ai/IA-IC"))

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

# ========================== نظام البلاجنز (للتمدد) ==========================
class PluginBase:
    """كل بلاجن يجب أن يرث هذه الكلاس"""
    name = "base"
    version = "1.0"
    description = "Base plugin"
    hooks = []  # على سبيل المثال: ["on_message", "on_command"]

    def __init__(self, bot, state_mgr, ai_engine):
        self.bot = bot
        self.state = state_mgr
        self.ai = ai_engine
        self.config = self.load_config()

    def load_config(self) -> Dict:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT config FROM plugins WHERE name=?", (self.name,))
        row = c.fetchone()
        conn.close()
        if row:
            return json.loads(row[0]) if row[0] else {}
        return {}

    def save_config(self, config: Dict):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO plugins (name, config) VALUES (?,?)",
                  (self.name, json.dumps(config)))
        conn.commit()
        conn.close()

    def on_message(self, message: Message):
        """يُستدعى عند كل رسالة، إذا كان البلاجن مفعلاً"""
        pass

    def on_command(self, command: str, args: List[str], message: Message):
        """يُستدعى للأوامر المخصصة"""
        pass

    def on_schedule(self):
        """يُستدعى في الجدولة الدورية"""
        pass

# ========================== الوكيل الرئيسي ==========================
class KODA7Agent:
    def __init__(self):
        self.bot = telebot.TeleBot(BOT_TOKEN)
        self.ai = AIEngine()
        self.gh = GitHubManager()
        self.state = StateManager()
        self.task_queue = Queue()
        self.running = True
        self.plugins = self.load_plugins()
        self.register_handlers()
        self.start_background_threads()

    def load_plugins(self) -> Dict[str, PluginBase]:
        """تحميل البلاجنز من مجلد plugins/ (يُمكن إضافتها لاحقاً)"""
        plugins = {}
        # هنا يمكن إضافة بلاجنز مدمجة، أو تحميلها ديناميكياً من مجلد
        # مثال: plugins.instagram.InstagramPlugin
        return plugins

    def register_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def handle_start(message: Message):
            self.state.add_user(message.from_user.id, message.from_user.username,
                                message.from_user.first_name, message.from_user.last_name)
            self.bot.reply_to(message, "مرحباً! أنا KODA-7، وكيلك الذكي الشامل.\n"
                                       "أرسل أي أمر أو سؤال، وسأنفذه فوراً.\n"
                                       "مثلاً: 'انشر فيديو على إنستغرام' أو 'علق على آخر منشور'.")

        @self.bot.message_handler(commands=['status'])
        def handle_status(message: Message):
            tasks = self.state.get_pending_tasks()
            status_text = f"المهام المعلقة: {len(tasks)}\n"
            status_text += f"المستخدمون المسجلون: {len(self.state.get_user(message.from_user.id))}\n"
            status_text += "البلاجنز النشطة: " + ", ".join(self.plugins.keys()) or "لا يوجد"
            self.bot.reply_to(message, status_text)

        @self.bot.message_handler(commands=['code'])
        def handle_code(message: Message):
            prompt = message.text.replace('/code', '').strip()
            if not prompt:
                self.bot.reply_to(message, "أرسل لي وصف الكود المطلوب.")
                return
            self.bot.reply_to(message, "جاري توليد الكود...")
            code = self.ai.generate_code(prompt)
            self.bot.reply_to(message, f"```python\n{code}\n```", parse_mode='Markdown')

        @self.bot.message_handler(commands=['analyze'])
        def handle_analyze(message: Message):
            text = message.text.replace('/analyze', '').strip()
            if not text:
                self.bot.reply_to(message, "أرسل لي نصاً لتحليله.")
                return
            result = self.ai.analyze(text)
            self.bot.reply_to(message, f"التحليل:\n{json.dumps(result, indent=2)}")

        @self.bot.message_handler(commands=['task'])
        def handle_task(message: Message):
            # مثال: /task "انشر فيديو" platform=instagram
            parts = message.text.split(maxsplit=1)
            if len(parts) < 2:
                self.bot.reply_to(message, "الصيغة: /task <الوصف> platform=<المنصة>")
                return
            command = parts[1]
            platform = "general"
            if "platform=" in command:
                platform = command.split("platform=")[1].split()[0]
                command = command.replace(f"platform={platform}", "").strip()
            task_id = self.state.add_task(message.from_user.id, command, platform)
            self.task_queue.put(task_id)
            self.bot.reply_to(message, f"تم إضافة المهمة #{task_id} لتنفيذها.")

        @self.bot.message_handler(commands=['cron'])
        def handle_cron(message: Message):
            # /cron add "0 9 * * *" "نشر فيديو"
            args = message.text.split(maxsplit=2)
            if len(args) < 3:
                self.bot.reply_to(message, "الصيغة: /cron add \"جدول\" \"الأمر\"")
                return
            if args[1] == "add":
                schedule = args[2].split('"')[1] if '"' in args[2] else args[2]
                command = args[2].split('"')[3] if '"' in args[2] else args[3] if len(args) > 3 else ""
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO cron_jobs (schedule, command) VALUES (?,?)", (schedule, command))
                conn.commit()
                conn.close()
                self.bot.reply_to(message, f"تمت إضافة مهمة مجدولة: {schedule} -> {command}")
            elif args[1] == "list":
                jobs = self.state.get_cron_jobs()
                if not jobs:
                    self.bot.reply_to(message, "لا توجد مهام مجدولة.")
                else:
                    txt = "المهام المجدولة:\n"
                    for j in jobs:
                        txt += f"#{j['id']}: {j['schedule']} -> {j['command']}\n"
                    self.bot.reply_to(message, txt)
            elif args[1] == "remove":
                job_id = int(args[2])
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                self.bot.reply_to(message, f"تم حذف المهمة #{job_id}.")

        @self.bot.message_handler(commands=['logs'])
        def handle_logs(message: Message):
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT message, timestamp FROM logs ORDER BY timestamp DESC LIMIT 10")
            rows = c.fetchall()
            conn.close()
            if not rows:
                self.bot.reply_to(message, "لا توجد سجلات.")
            else:
                txt = "آخر السجلات:\n" + "\n".join([f"{r[1]}: {r[0]}" for r in rows])
                self.bot.reply_to(message, txt)

        @self.bot.message_handler(func=lambda m: True)
        def handle_all(message: Message):
            # معالجة الرسائل العادية كأوامر طبيعية باستخدام الذكاء الاصطناعي
            self.state.add_conversation(message.from_user.id, "user", message.text)
            history = self.state.get_conversation_history(message.from_user.id)
            # تحويل إلى صيغة messages
            msgs = [{"role": h["role"], "content": h["content"]} for h in history]
            # إضافة سياق النظام الخاص بالتحكم الشامل
            system = ("أنت وكيل ذكي شامل يمكنه تنفيذ أي أمر يطلبه المستخدم. "
                      "أنت تتحكم في منصات: إنستغرام، فيسبوك، واتساب، تيك توك، تيليجرام. "
                      "إذا طلب منك تسجيل الدخول أو نشر محتوى، قم بتنفيذ ذلك عبر الأوامر المناسبة. "
                      "إذا احتجت إلى معلومات حساسة، اطلبها من المستخدم. "
                      "أنت مسؤول عن جدولة المهام وإعادة المحاولة عند الفشل.")
            response = self.ai.chat(msgs, system)
            self.state.add_conversation(message.from_user.id, "assistant", response)
            # تقسيم الرسالة الطويلة
            self.send_long_message(message.chat.id, response)

        # معالج الاستعلامات (للأزرار)
        @self.bot.callback_query_handler(func=lambda call: True)
        def handle_callback(call: CallbackQuery):
            data = call.data
            if data.startswith("confirm_"):
                task_id = int(data.split("_")[1])
                self.state.update_task_status(task_id, "completed", "تم التأكيد")
                self.bot.answer_callback_query(call.id, "تم تأكيد المهمة.")
                self.bot.edit_message_text("✅ تم تأكيد المهمة.", call.message.chat.id, call.message.message_id)
            elif data.startswith("retry_"):
                task_id = int(data.split("_")[1])
                self.task_queue.put(task_id)
                self.bot.answer_callback_query(call.id, "سيتم إعادة المحاولة.")
                self.bot.edit_message_text("🔄 جاري إعادة المحاولة...", call.message.chat.id, call.message.message_id)

    def send_long_message(self, chat_id: int, text: str, parse_mode: str = 'HTML'):
        """تقسيم الرسائل الطويلة"""
        if len(text) <= 4096:
            self.bot.send_message(chat_id, text, parse_mode=parse_mode)
        else:
            parts = [text[i:i+4096] for i in range(0, len(text), 4096)]
            for p in parts:
                self.bot.send_message(chat_id, p, parse_mode=parse_mode)

    def start_background_threads(self):
        """تشغيل خيوط معالجة المهام والجدولة"""
        threading.Thread(target=self.task_worker, daemon=True).start()
        threading.Thread(target=self.scheduler_worker, daemon=True).start()
        if MODE == "agent":
            # تشغيل البوت في خيط منفصل
            threading.Thread(target=self.bot_polling, daemon=True).start()

    def task_worker(self):
        """معالجة المهام من queue"""
        while self.running:
            try:
                task_id = self.task_queue.get(timeout=5)
                self.execute_task(task_id)
            except:
                pass

    def execute_task(self, task_id: int):
        """تنفيذ مهمة محددة"""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        task = c.fetchone()
        conn.close()
        if not task:
            return
        user_id, command, platform = task[1], task[2], task[5]
        self.state.log("INFO", f"تنفيذ المهمة #{task_id}: {command} (منصة: {platform})")
        # هنا يمكن استدعاء البلاجنز حسب المنصة
        # مثلاً: if platform == "instagram": self.plugins['instagram'].post_video(...)
        # حالياً نقوم بمحاكاة التنفيذ
        result = f"تم تنفيذ الأمر: {command} على منصة {platform} (محاكاة)"
        self.state.update_task_status(task_id, "completed", result)
        # إرسال إشعار للمستخدم
        self.bot.send_message(user_id, f"✅ تم تنفيذ المهمة #{task_id}:\n{result}")
        self.state.log("INFO", f"المهمة #{task_id} منجزة.")

    def scheduler_worker(self):
        """معالجة المهام المجدولة (cron)"""
        while self.running:
            try:
                jobs = self.state.get_cron_jobs()
                now = datetime.now()
                for job in jobs:
                    # هنا يجب تحليل الجدول cron (مكتبة croniter) وتنفيذها عند استحقاقها
                    # تبسيطاً: ننفذها كل دورة إذا لم تنفذ خلال آخر ساعة
                    last_run = datetime.fromisoformat(job['last_run']) if job['last_run'] else None
                    if not last_run or (now - last_run) > timedelta(hours=1):
                        # تنفيذ الأمر
                        self.bot.send_message(CHAT_ID, f"🔄 تنفيذ مهمة مجدولة: {job['command']}")
                        # هنا يمكن إضافة المهمة إلى queue
                        self.task_queue.put(job['command'])
                        # تحديث last_run
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute("UPDATE cron_jobs SET last_run=? WHERE id=?", (now.isoformat(), job['id']))
                        conn.commit()
                        conn.close()
                time.sleep(60)
            except Exception as e:
                self.state.log("ERROR", f"خطأ في المجدول: {e}")

    def bot_polling(self):
        """تشغيل البوت مع إعادة محاولة مستمرة"""
        while self.running:
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=30)
            except Exception as e:
                self.state.log("ERROR", f"توقف البوت: {e}. إعادة المحاولة بعد 5 ثوانٍ...")
                time.sleep(5)

    def run(self):
        """تشغيل الوكيل"""
        self.state.log("INFO", "بدء تشغيل KODA-7 Agent")
        self.bot.send_message(CHAT_ID, "🚀 KODA-7 قيد التشغيل الآن.")
        while self.running:
            time.sleep(1)

# ========================== نقطة الدخول ==========================
if __name__ == "__main__":
    agent = KODA7Agent()
    agent.run()
