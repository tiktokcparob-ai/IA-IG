#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 KODA-7: AI Agent System
نظام وكيل ذكاء اصطناعي متكامل
"""

import os
import sys
import json
import time
import re
import subprocess
import traceback
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from pathlib import Path
import threading
import queue

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from groq import Groq
from github import Github, GithubException
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# ═══════════════════════════════════════════════════════════════
# الإعدادات والتهيئة
# ═══════════════════════════════════════════════════════════════

load_dotenv()

console = Console()

# ─── المتغيرات البيئية ───
GH_PAT = os.getenv("g")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REPO_NAME = os.getenv("REPO", "tiktokcparob-ai/IA-IG")

# ─── التحقق من المتغيرات ───
required_vars = ["GROQ_API_KEY", "BOT_TOKEN", "CHAT_ID"]
missing = [v for v in required_vars if not os.getenv(v)]
if missing:
    console.print(f"[red]❌ متغيرات مفقودة: {', '.join(missing)}[/red]")
    sys.exit(1)

# ─── تهيئة العملاء ───
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
groq_client = Groq(api_key=GROQ_API_KEY)
github_client = Github(GH_PAT) if GH_PAT else None

# ─── ملفات الحالة ───
STATE_FILE = "koda_state.json"
LOG_FILE = "koda_logs.json"
MEMORY_FILE = "koda_memory.json"

# ═══════════════════════════════════════════════════════════════
# أنظمة التخزين
# ═══════════════════════════════════════════════════════════════

class StateManager:
    """مدير الحالة والذاكرة"""
    
    def __init__(self):
        self.state = self._load(STATE_FILE, {
            "last_run": None,
            "total_runs": 0,
            "tasks_completed": 0,
            "errors_count": 0,
            "active_tasks": [],
            "user_preferences": {}
        })
        self.memory = self._load(MEMORY_FILE, {
            "conversations": [],
            "learned_patterns": [],
            "custom_commands": {}
        })
        self.logs = self._load(LOG_FILE, {"logs": []})
    
    def _load(self, filepath: str, default: dict) -> dict:
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            console.print(f"[yellow]⚠️ تعذر تحميل {filepath}: {e}[/yellow]")
        return default
    
    def save(self):
        for filepath, data in [(STATE_FILE, self.state), 
                               (MEMORY_FILE, self.memory), 
                               (LOG_FILE, self.logs)]:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                console.print(f"[red]❌ خطأ في الحفظ {filepath}: {e}[/red]")
    
    def log(self, level: str, message: str, context: dict = None):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
            "context": context or {}
        }
        self.logs["logs"].append(entry)
        # الاحتفاظ بآخر 500 سجل فقط
        self.logs["logs"] = self.logs["logs"][-500:]
        self.save()
        
        # طباعة جميلة
        color = {"INFO": "cyan", "SUCCESS": "green", "WARNING": "yellow", "ERROR": "red"}.get(level, "white")
        console.print(f"[{color}][{level}][/{color}] {message}")

state = StateManager()

# ═══════════════════════════════════════════════════════════════
# أنظمة الذكاء الاصطناعي
# ═══════════════════════════════════════════════════════════════

class AIEngine:
    """محرك الذكاء الاصطناعي المتقدم"""
    
    SYSTEM_PROMPT = """أنت KODA-7، وكيل ذكاء اصطناعي متقدم تم تطويره للعمل على GitHub Actions.
لديك القدرة على:
- كتابة وتحليل الأكواد البرمجية
- إدارة مستودعات GitHub
- التواصل عبر Telegram
- تنفيذ المهام المجدولة
- التعلم من التجارب السابقة

قواعدك:
1. كن دقيقًا ومهنيًا
2. عند كتابة كود، تأكد من أنه كامل ويعمل
3. استخدم أفضل الممارسات في البرمجة
4. تحدث بالعربية والإنجليزية حسب سياق المحادثة
5. عند مواجهة خطأ، حلله وقدم حلاً"""

    def __init__(self):
        self.conversation_history = []
        self.max_history = 10
    
    def chat(self, message: str, context: str = "", model: str = "llama-3.3-70b-versatile") -> str:
        """دردشة مع الذكاء الاصطناعي"""
        try:
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
            
            # إضافة السياق
            if context:
                messages.append({"role": "system", "content": f"Context: {context}"})
            
            # إضافة التاريخ
            for hist in self.conversation_history[-self.max_history:]:
                messages.append(hist)
            
            messages.append({"role": "user", "content": message})
            
            response = groq_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
                max_tokens=4096,
                top_p=0.9
            )
            
            reply = response.choices[0].message.content
            
            # تحديث التاريخ
            self.conversation_history.append({"role": "user", "content": message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            
            return reply
            
        except Exception as e:
            state.log("ERROR", f"AI Chat Error: {e}")
            return f"❌ خطأ في الذكاء الاصطناعي: {str(e)}"
    
    def generate_code(self, description: str, language: str = "python") -> str:
        """توليد كود برمجي"""
        prompt = f"""اكتب كود {language} كامل ويعمل بناءً على الوصف التالي:
{description}

المتطلبات:
- الكود يجب أن يكون كاملاً وقابل للتشغيل
- أضف تعليقات توضيحية
- اتبع أفضل الممارسات
- لا تترك أجزاء ناقصة

الكود:"""
        return self.chat(prompt, model="llama-3.3-70b-versatile")
    
    def analyze_code(self, code: str) -> str:
        """تحليل كود برمجي"""
        prompt = f"""حلل الكود التالي وقدم:
1. الأخطاء المحتملة
2. اقتراحات التحسين
3. درجة الجودة من 10

الكود:
```{code}
```"""
        return self.chat(prompt)

ai = AIEngine()

# ═══════════════════════════════════════════════════════════════
# أنظمة GitHub
# ═══════════════════════════════════════════════════════════════

class GitHubManager:
    """مدير GitHub المتقدم"""
    
    def __init__(self):
        self.client = github_client
        self.repo = None
        if self.client and REPO_NAME:
            try:
                self.repo = self.client.get_repo(REPO_NAME)
            except Exception as e:
                state.log("ERROR", f"GitHub init error: {e}")
    
    def get_repo_info(self) -> str:
        """معلومات المستودع"""
        if not self.repo:
            return "❌ غير متصل بـ GitHub"
        try:
            info = f"""📁 *معلومات المستودع*
            
🏷️ الاسم: `{self.repo.name}`
📝 الوصف: {self.repo.description or 'لا يوجد'}
⭐ النجوم: {self.repo.stargazers_count}
👀 المشاهدات: {self.repo.watchers_count}
🍴 الفوركات: {self.repo.forks_count}
📅 آخر تحديث: {self.repo.updated_at}
🌿 الفرع الافتراضي: `{self.repo.default_branch}`"""
            return info
        except Exception as e:
            return f"❌ خطأ: {e}"
    
    def create_file(self, path: str, content: str, message: str = None) -> str:
        """إنشاء ملف"""
        if not self.repo:
            return "❌ غير متصل"
        try:
            msg = message or f"🤖 KODA-7: إنشاء {path}"
            self.repo.create_file(path, msg, content.encode('utf-8'))
            state.state["tasks_completed"] += 1
            state.save()
            return f"✅ تم إنشاء `{path}` بنجاح"
        except GithubException as e:
            if e.status == 422:
                return f"⚠️ الملف `{path}` موجود مسبقًا"
            return f"❌ خطأ: {e.data.get('message', str(e))}"
    
    def update_file(self, path: str, content: str, message: str = None) -> str:
        """تحديث ملف"""
        if not self.repo:
            return "❌ غير متصل"
        try:
            file = self.repo.get_contents(path)
            msg = message or f"🤖 KODA-7: تحديث {path}"
            self.repo.update_file(path, msg, content.encode('utf-8'), file.sha)
            state.state["tasks_completed"] += 1
            state.save()
            return f"✅ تم تحديث `{path}` بنجاح"
        except Exception as e:
            return f"❌ خطأ: {e}"
    
    def get_file(self, path: str) -> str:
        """قراءة ملف"""
        if not self.repo:
            return "❌ غير متصل"
        try:
            file = self.repo.get_contents(path)
            content = file.decoded_content.decode('utf-8')
            # تقصير المحتوى الطويل
            if len(content) > 4000:
                content = content[:4000] + "\n\n... (تم تقصير المحتوى)"
            return f"📄 `{path}`:\n```\n{content}\n```"
        except Exception as e:
            return f"❌ خطأ: {e}"
    
    def list_issues(self, state_issue: str = "open") -> str:
        """قائمة المشاكل"""
        if not self.repo:
            return "❌ غير متصل"
        try:
            issues = self.repo.get_issues(state=state_issue)[:10]
            if not issues:
                return f"📭 لا توجد مشاكل `{state_issue}`"
            
            text = f"📋 *مشاكل ({state_issue})*:\n\n"
            for issue in issues:
                text += f"#{issue.number}: {issue.title}\n"
                text += f"🔗 [رابط]({issue.html_url})\n\n"
            return text
        except Exception as e:
            return f"❌ خطأ: {e}"
    
    def create_issue(self, title: str, body: str) -> str:
        """إنشاء مشكلة"""
        if not self.repo:
            return "❌ غير متصل"
        try:
            issue = self.repo.create_issue(title=title, body=body)
            return f"✅ تم إنشاء المشكلة #{issue.number}\n🔗 {issue.html_url}"
        except Exception as e:
            return f"❌ خطأ: {e}"

gh = GitHubManager()

# ═══════════════════════════════════════════════════════════════
# أنظمة المهام
# ═══════════════════════════════════════════════════════════════

class TaskManager:
    """مدير المهام المتقدم"""
    
    def __init__(self):
        self.tasks = queue.Queue()
        self.running = False
        self.worker_thread = None
    
    def add_task(self, task_type: str, data: dict):
        """إضافة مهمة"""
        task = {
            "id": f"task_{int(time.time())}",
            "type": task_type,
            "data": data,
            "created_at": datetime.now().isoformat(),
            "status": "pending"
        }
        self.tasks.put(task)
        state.state["active_tasks"].append(task)
        state.save()
        state.log("INFO", f"Task added: {task_type}")
        return task["id"]
    
    def process_tasks(self):
        """معالجة المهام"""
        while self.running:
            try:
                task = self.tasks.get(timeout=5)
                self._execute_task(task)
            except queue.Empty:
                continue
            except Exception as e:
                state.log("ERROR", f"Task processing error: {e}")
    
    def _execute_task(self, task: dict):
        """تنفيذ مهمة"""
        task["status"] = "running"
        state.save()
        
        try:
            task_type = task["type"]
            data = task["data"]
            
            if task_type == "code_generation":
                result = ai.generate_code(data["description"], data.get("language", "python"))
                if CHAT_ID:
                    send_long_message(CHAT_ID, f"📝 *كود مولد:*\n\n```{data.get('language', 'python')}\n{result}\n```")
            
            elif task_type == "github_commit":
                result = gh.create_file(data["path"], data["content"], data.get("message"))
                if CHAT_ID:
                    bot.send_message(CHAT_ID, result, parse_mode="Markdown")
            
            elif task_type == "analysis":
                result = ai.analyze_code(data["code"])
                if CHAT_ID:
                    send_long_message(CHAT_ID, f"🔍 *تحليل الكود:*\n\n{result}")
            
            task["status"] = "completed"
            state.state["tasks_completed"] += 1
            
        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)
            state.state["errors_count"] += 1
        
        # إزالة من النشطة
        state.state["active_tasks"] = [t for t in state.state["active_tasks"] 
                                       if t.get("id") != task["id"]]
        state.save()
    
    def start(self):
        """بدء المعالجة"""
        self.running = True
        self.worker_thread = threading.Thread(target=self.process_tasks, daemon=True)
        self.worker_thread.start()
    
    def stop(self):
        """إيقاف المعالجة"""
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=10)

task_mgr = TaskManager()

# ═══════════════════════════════════════════════════════════════
# أدوات مساعدة
# ═══════════════════════════════════════════════════════════════

def send_long_message(chat_id: int, text: str, parse_mode: str = "Markdown"):
    """إرسال رسائل طويلة مقسمة"""
    max_length = 4096
    if len(text) <= max_length:
        return bot.send_message(chat_id, text, parse_mode=parse_mode)
    
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        # البحث عن آخر فاصل مناسب
        split_at = text.rfind('\n', 0, max_length)
        if split_at == -1:
            split_at = max_length
        parts.append(text[:split_at])
        text = text[split_at:].strip()
    
    messages = []
    for i, part in enumerate(parts):
        header = f"📄 *(جزء {i+1}/{len(parts)})*\n\n" if len(parts) > 1 else ""
        msg = bot.send_message(chat_id, header + part, parse_mode=parse_mode)
        messages.append(msg)
        time.sleep(0.5)
    
    return messages

def get_system_status() -> str:
    """حالة النظام"""
    uptime = "N/A"
    if state.state["last_run"]:
        last = datetime.fromisoformat(state.state["last_run"])
        uptime = str(datetime.now() - last).split('.')[0]
    
    status_text = f"""🤖 *حالة KODA-7*

⏱️ آخر تشغيل: `{state.state['last_run'] or 'أول مرة'}`
🔢 عدد التشغيلات: `{state.state['total_runs']}`
✅ المهام المنجزة: `{state.state['tasks_completed']}`
❌ الأخطاء: `{state.state['errors_count']}`
📋 المهام النشطة: `{len(state.state['active_tasks'])}`
⏳ مدة التشغيل: `{uptime}`

🧠 *الذاكرة:*
- المحادثات: `{len(state.memory['conversations'])}`
- الأوامر المخصصة: `{len(state.memory['custom_commands'])}`
"""
    return status_text

def create_keyboard(options: list) -> InlineKeyboardMarkup:
    """إنشاء لوحة مفاتيح"""
    markup = InlineKeyboardMarkup()
    for opt in options:
        markup.add(InlineKeyboardButton(opt["text"], callback_data=opt["callback"]))
    return markup

# ═══════════════════════════════════════════════════════════════
# معالجات Telegram
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(commands=['start'])
def cmd_start(message):
    """بدء البوت"""
    welcome = f"""🤖 *مرحبًا بك في KODA-7!*

أنا وكيل ذكاء اصطناعي متقدم يعمل على GitHub Actions.
يمكنني مساعدتك في:

📝 كتابة وتحليل الأكواد
📁 إدارة مستودع GitHub
🔍 البحث والتحليل
⚙️ تنفيذ المهام الآلية
📊 مراقبة النظام

*الأوامر المتاحة:*
/start - بدء البوت
/status - حالة النظام
/code - توليد كود
/analyze - تحليل كود
/github - أوامر GitHub
/ai - محادثة مع الذكاء الاصطناعي
/task - إضافة مهمة
/help - المساعدة
"""
    bot.send_message(message.chat.id, welcome, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    """حالة النظام"""
    bot.send_message(message.chat.id, get_system_status(), parse_mode="Markdown")

@bot.message_handler(commands=['help'])
def cmd_help(message):
    """المساعدة"""
    help_text = """📚 *دليل استخدام KODA-7*

*📝 توليد كود:*
`/code اكتب لي بوت تلغرام باستخدام python-telegram-bot`

*🔍 تحليل كود:*
أرسل الكود مع الأمر:
`/analyze`
ثم أرسل الكود في الرسالة التالية

*📁 GitHub:*
`/github info` - معلومات المستودع
`/github file <path>` - قراءة ملف
`/github create <path>` - إنشاء ملف (سيطلب المحتوى)
`/github issues` - قائمة المشاكل

*🤖 الذكاء الاصطناعي:*
`/ai <سؤالك>` - محادثة عامة

*⚙️ المهام:*
`/task code <وصف>` - مهمة توليد كود
`/task commit <path>` - مهمة رفع ملف

*📊 النظام:*
`/status` - حالة النظام
`/logs` - آخر السجلات
"""
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['code'])
def cmd_code(message):
    """توليد كود"""
    prompt = message.text.replace('/code', '').strip()
    if not prompt:
        bot.send_message(message.chat.id, 
                        "📝 *استخدام:* `/code اكتب لي دالة لحساب فيبوناتشي`",
                        parse_mode="Markdown")
        return
    
    bot.send_message(message.chat.id, "⏳ *جاري توليد الكود...*", parse_mode="Markdown")
    
    try:
        code = ai.generate_code(prompt)
        send_long_message(message.chat.id, f"✅ *الكود المولد:*\n\n{code}")
        state.log("SUCCESS", f"Code generated for: {prompt[:50]}...")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطأ: {e}")

@bot.message_handler(commands=['analyze'])
def cmd_analyze(message):
    """تحليل كود"""
    bot.send_message(message.chat.id, 
                    "📤 *أرسل الكود الذي تريد تحليله* (يمكنك إرساله كملف أو نص)",
                    parse_mode="Markdown")
    bot.register_next_step_handler(message, process_analyze)

def process_analyze(message):
    """معالجة التحليل"""
    code = message.text or (message.caption if hasattr(message, 'caption') else '')
    
    if message.document:
        # تحميل الملف
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            code = downloaded.decode('utf-8')
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ خطأ في قراءة الملف: {e}")
            return
    
    if not code:
        bot.send_message(message.chat.id, "❌ لم يتم العثور على كود")
        return
    
    bot.send_message(message.chat.id, "🔍 *جاري التحليل...*", parse_mode="Markdown")
    
    try:
        analysis = ai.analyze_code(code)
        send_long_message(message.chat.id, f"📊 *نتيجة التحليل:*\n\n{analysis}")
        state.log("SUCCESS", "Code analyzed")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطأ: {e}")

@bot.message_handler(commands=['github'])
def cmd_github(message):
    """أوامر GitHub"""
    args = message.text.split()
    if len(args) < 2:
        keyboard = create_keyboard([
            {"text": "📁 معلومات المستودع", "callback": "gh_info"},
            {"text": "📋 المشاكل", "callback": "gh_issues"},
            {"text": "📄 قراءة ملف", "callback": "gh_read"},
            {"text": "➕ إنشاء ملف", "callback": "gh_create"}
        ])
        bot.send_message(message.chat.id, "📁 *GitHub:* اختر عملية:", 
                        parse_mode="Markdown", reply_markup=keyboard)
        return
    
    subcmd = args[1].lower()
    
    if subcmd == 'info':
        bot.send_message(message.chat.id, gh.get_repo_info(), parse_mode="Markdown")
    
    elif subcmd == 'file' and len(args) >= 3:
        path = args[2]
        bot.send_message(message.chat.id, gh.get_file(path), parse_mode="Markdown")
    
    elif subcmd == 'create' and len(args) >= 3:
        path = args[2]
        bot.send_message(message.chat.id, 
                        f"📤 *أرسل محتوى الملف* `{path}`:\n(يمكنك إرساله في الرسالة التالية)",
                        parse_mode="Markdown")
        bot.register_next_step_handler(message, lambda m: process_create_file(m, path))
    
    elif subcmd == 'issues':
        bot.send_message(message.chat.id, gh.list_issues(), parse_mode="Markdown")
    
    else:
        bot.send_message(message.chat.id, "❌ أمر غير معروف. استخدم /github فقط لرؤية الخيارات")

def process_create_file(message, path: str):
    """معالجة إنشاء ملف"""
    content = message.text or ''
    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            content = downloaded.decode('utf-8')
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ خطأ: {e}")
            return
    
    result = gh.create_file(path, content)
    bot.send_message(message.chat.id, result, parse_mode="Markdown")

@bot.message_handler(commands=['ai'])
def cmd_ai(message):
    """محادثة AI"""
    prompt = message.text.replace('/ai', '').strip()
    if not prompt:
        bot.send_message(message.chat.id, 
                        "🤖 *استخدام:* `/ai ما هي أفضل ممارسات Python؟`",
                        parse_mode="Markdown")
        return
    
    bot.send_message(message.chat.id, "🧠 *جاري التفكير...*", parse_mode="Markdown")
    
    try:
        response = ai.chat(prompt)
        send_long_message(message.chat.id, f"🤖 *KODA-7:*\n\n{response}")
        state.memory["conversations"].append({
            "user": prompt,
            "bot": response,
            "time": datetime.now().isoformat()
        })
        state.save()
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطأ: {e}")

@bot.message_handler(commands=['task'])
def cmd_task(message):
    """إضافة مهمة"""
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        bot.send_message(message.chat.id, 
                        """⚙️ *إدارة المهام*
                        
/task code <وصف> - توليد كود
/task commit <path> - رفع ملف (سيطلب المحتوى)
/task analyze - تحليل كود (سيطلب الكود)""",
                        parse_mode="Markdown")
        return
    
    task_type = args[1].lower()
    
    if task_type == 'code' and len(args) >= 3:
        task_id = task_mgr.add_task("code_generation", {
            "description": args[2],
            "language": "python"
        })
        bot.send_message(message.chat.id, 
                        f"✅ *تمت إضافة المهمة*\n🆔 الرقم: `{task_id}`\n⏳ سيتم المعالجة...",
                        parse_mode="Markdown")
    
    elif task_type == 'commit' and len(args) >= 3:
        path = args[2]
        bot.send_message(message.chat.id, 
                        f"📤 *أرسل محتوى الملف* `{path}`:",
                        parse_mode="Markdown")
        bot.register_next_step_handler(message, lambda m: process_task_commit(m, path))
    
    elif task_type == 'analyze':
        bot.send_message(message.chat.id, "📤 *أرسل الكود للتحليل:*")
        bot.register_next_step_handler(message, process_task_analyze)
    
    else:
        bot.send_message(message.chat.id, "❌ نوع مهمة غير معروف")

def process_task_commit(message, path: str):
    """معالجة مهمة الرفع"""
    content = message.text or ''
    if message.document:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        content = downloaded.decode('utf-8')
    
    task_id = task_mgr.add_task("github_commit", {
        "path": path,
        "content": content,
        "message": f"🤖 KODA-7: إضافة {path}"
    })
    bot.send_message(message.chat.id, 
                    f"✅ *تمت إضافة مهمة الرفع*\n🆔 الرقم: `{task_id}`",
                    parse_mode="Markdown")

def process_task_analyze(message):
    """معالجة مهمة التحليل"""
    code = message.text or ''
    if message.document:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        code = downloaded.decode('utf-8')
    
    task_id = task_mgr.add_task("analysis", {"code": code})
    bot.send_message(message.chat.id, 
                    f"✅ *تمت إضافة مهمة التحليل*\n🆔 الرقم: `{task_id}`",
                    parse_mode="Markdown")

@bot.message_handler(commands=['logs'])
def cmd_logs(message):
    """عرض السجلات"""
    logs = state.logs["logs"][-20:]  # آخر 20 سجل
    if not logs:
        bot.send_message(message.chat.id, "📭 لا توجد سجلات")
        return
    
    text = "📋 *آخر السجلات:*\n\n"
    for log in logs:
        emoji = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌"}.get(log["level"], "•")
        time_str = log["timestamp"].split("T")[1].split(".")[0]
        text += f"{emoji} `{time_str}` {log['message'][:100]}\n"
    
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    """معالجة الأزرار"""
    if call.data == "gh_info":
        bot.edit_message_text(gh.get_repo_info(), 
                             call.message.chat.id, call.message.message_id,
                             parse_mode="Markdown")
    elif call.data == "gh_issues":
        bot.edit_message_text(gh.list_issues(),
                             call.message.chat.id, call.message.message_id,
                             parse_mode="Markdown")
    elif call.data == "gh_read":
        bot.send_message(call.message.chat.id, 
                        "📤 *أرسل مسار الملف:*\nمثال: `agent.py` أو `src/main.py`",
                        parse_mode="Markdown")
        bot.register_next_step_handler(call.message, 
                                       lambda m: bot.send_message(m.chat.id, 
                                                                  gh.get_file(m.text),
                                                                  parse_mode="Markdown"))
    elif call.data == "gh_create":
        bot.send_message(call.message.chat.id,
                        "📤 *أرسل مسار الملف الجديد:*\nمثال: `new_feature.py`",
                        parse_mode="Markdown")
        bot.register_next_step_handler(call.message, 
                                       lambda m: cmd_github(m) if m.text.startswith('/github create') else None)

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    """معالجة النصوص العامة"""
    # تخزين في الذاكرة
    state.memory["conversations"].append({
        "user": message.text,
        "time": datetime.now().isoformat()
    })
    state.save()
    
    # إذا كان الرد على رسالة البوت
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.get_me().id:
        bot.send_message(message.chat.id, "🧠 *جاري المعالجة...*", parse_mode="Markdown")
        try:
            response = ai.chat(message.text)
            send_long_message(message.chat.id, f"🤖 *KODA-7:*\n\n{response}")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ خطأ: {e}")
        return
    
    # رسالة ترحيبية للرسائل العامة
    if message.chat.id == int(CHAT_ID) if CHAT_ID else False:
        pass  # لا شيء للرسائل العامة في القناة

# ═══════════════════════════════════════════════════════════════
# المهام المجدولة (Scheduler)
# ═══════════════════════════════════════════════════════════════

def run_scheduler():
    """تشغيل المهام المجدولة"""
    state.log("INFO", "Scheduler started")
    
    # مهام دورية يمكن إضافتها هنا
    # مثال: فحص المستودع كل 6 ساعات
    
    if CHAT_ID:
        bot.send_message(int(CHAT_ID), 
                        "⏰ *الجدولة نشطة*\nسأقوم بالمهام الدورية تلقائيًا.",
                        parse_mode="Markdown")

# ═══════════════════════════════════════════════════════════════
# الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="KODA-7 AI Agent")
    parser.add_argument('--mode', default='agent', 
                       choices=['agent', 'scheduler', 'maintenance'],
                       help='وضع التشغيل')
    args = parser.parse_args()
    
    # تحديث الحالة
    state.state["last_run"] = datetime.now().isoformat()
    state.state["total_runs"] += 1
    state.save()
    
    console.print(Panel.fit(
        Text("🤖 KODA-7 AI Agent", style="bold cyan"),
        subtitle=f"وضع: {args.mode} | تشغيل: #{state.state['total_runs']}"
    ))
    
    state.log("INFO", f"KODA-7 started in {args.mode} mode")
    
    if args.mode == 'agent':
        # إرسال رسالة بدء
        if CHAT_ID:
            try:
                bot.send_message(int(CHAT_ID), 
                                f"""🚀 *KODA-7 نشط*
                                
🔄 تشغيل رقم: `{state.state['total_runs']}`
📅 الوقت: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`
⚡ الوضع: `Agent Mode`
                                
✅ النظام جاهز للاستلام.""",
                                parse_mode="Markdown")
            except Exception as e:
                console.print(f"[red]❌ فشل إرسال رسالة البدء: {e}[/red]")
        
        # بدء مدير المهام
        task_mgr.start()
        
        # تشغيل البوت
        console.print("[green]🤖 Bot polling started...[/green]")
        try:
            bot.polling(none_stop=True, interval=1, timeout=30)
        except Exception as e:
            state.log("ERROR", f"Bot polling error: {e}")
            traceback.print_exc()
        finally:
            task_mgr.stop()
    
    elif args.mode == 'scheduler':
        run_scheduler()
    
    elif args.mode == 'maintenance':
        # صيانة النظام
        state.log("INFO", "Running maintenance")
        
        # تنظيف السجلات القديمة
        old_logs = len(state.logs["logs"])
        state.logs["logs"] = state.logs["logs"][-1000:]
        
        # تنظيف الذاكرة
        state.memory["conversations"] = state.memory["conversations"][-100:]
        
        state.save()
        
        if CHAT_ID:
            bot.send_message(int(CHAT_ID),
                            f"""🔧 *صيانة النظام*
                            
🧹 تم تنظيف السجلات: `{old_logs - len(state.logs['logs'])}`
💾 حجم الذاكرة: `{len(state.memory['conversations'])}` محادثة
✅ النظام نظيف.""",
                            parse_mode="Markdown")
    
    state.log("INFO", "KODA-7 shutting down")
    state.save()

if __name__ == "__main__":
    main()
