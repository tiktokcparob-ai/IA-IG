import os
import telebot
from dotenv import load_dotenv

# تحميل المفاتيح من السر المجمع
load_dotenv()

GH_PAT = os.getenv("GH_PAT")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = telebot.TeleBot(BOT_TOKEN)

def main():
    try:
        # رسالة تأكيد مبدئية لعمل النظام
        bot.send_message(CHAT_ID, "✅ KODA-7، النظام يعمل. تم جلب المفاتيح من سر واحد بنجاح. الوكيل جاهز للبرمجة.")
        print("تم التشغيل بنجاح.")
    except Exception as e:
        print(f"خطأ: {e}")

if __name__ == "__main__":
    main()

