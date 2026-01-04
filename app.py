import telebot
from telebot import types
import flask
import psycopg2
from psycopg2.extras import RealDictCursor
import json
import os
import time
import re
import threading

# ************************************************
# تنظیمات محیطی و توکن 
# ************************************************
API_TOKEN = '8534337673:AAFD8TDLsujrOI6QjIcE4gGKewMaMYeNexc' 
DATABASE_URL = os.environ.get('DATABASE_URL') 
WEBHOOK_HOST = os.environ.get('RENDER_EXTERNAL_HOSTNAME') 
WEBHOOK_PORT = int(os.environ.get('PORT', 5000))

if not DATABASE_URL:
    print("❌ خطا: متغیر DATABASE_URL تنظیم نشده است. مطمئن شوید که دیتابیس را در رندر ساخته‌اید.")

bot = telebot.TeleBot(API_TOKEN)
app = flask.Flask(__name__)

# حافظه موقت (RAM) برای کنترل اسپم 
flood_control = {} 

# تنظیمات پیش‌فرض برای چت‌های جدید
DEFAULT_SETTINGS = {
    'welcome_msg': "👋 سلام {user_mention} عزیز، به گروه **{chat_title}** خوش اومدی! لطفا قوانین را رعایت کن.",
    'remove_system_msgs': True,
    'mute_on_link': True,
    'delete_welcome_after': 60,
    'media_locks': { 
        'photo': False,
        'video': False,
        'document': False,
        'sticker': False,
        'audio': False,
        'voice': False,
        'video_note': False,
    },
    'bad_words': ['کلمه۱', 'کلمه۲', 'فحش_ناپسند'],
    'chat_locked': False,
    'max_chars': 1000,
    'anti_flood_limit': 5,
    'anti_tabchi_enabled': True
}
# ************************************************
# بخش دیتابیس (PostgreSQL)
# ************************************************

def get_db_connection():
    """اتصال به دیتابیس پستگرس"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ خطا در اتصال به دیتابیس: {e}")
        return None

def init_db():
    """ساخت جدول تنظیمات اگر وجود نداشته باشد"""
    conn = get_db_connection()
    if conn is None: return
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id BIGINT PRIMARY KEY,
            settings JSONB
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("✅ دیتابیس بررسی و آماده شد.")

def get_settings(chat_id):
    """دریافت تنظیمات یک گروه از دیتابیس"""
    conn = get_db_connection()
    if conn is None: return DEFAULT_SETTINGS.copy()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT settings FROM chat_settings WHERE chat_id = %s", (chat_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()

    if result:
        final_settings = DEFAULT_SETTINGS.copy()
        # Migration: تبدیل قفل رسانه قدیمی به ساختار جدید
        if 'media_locked' in result['settings']:
            is_locked = result['settings'].pop('media_locked')
            if is_locked and 'media_locks' not in result['settings']:
                result['settings']['media_locks'] = {k: True for k in DEFAULT_SETTINGS['media_locks']}

        final_settings.update(result['settings'])
        return final_settings
    else:
        save_settings(chat_id, DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()

def save_settings(chat_id, new_settings):
    """ذخیره یا آپدیت تنظیمات در دیتابیس"""
    conn = get_db_connection()
    if conn is None: return
    cur = conn.cursor()
    settings_json = json.dumps(new_settings)
    
    cur.execute("""
        INSERT INTO chat_settings (chat_id, settings)
        VALUES (%s, %s)
        ON CONFLICT (chat_id) 
        DO UPDATE SET settings = %s;
    """, (chat_id, settings_json, settings_json))
    
    conn.commit()
    cur.close()
    conn.close()

# ************************************************
# توابع کمکی
# ************************************************

def is_admin(chat_id, user_id):
    """بررسی می‌کند آیا کاربر ادمین است یا خیر"""
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False

def delete_msg(chat_id, msg_id):
    """حذف پیام و مدیریت خطاها"""
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

def mute_user(chat_id, user_id, duration=3600):
    """سکوت کردن کاربر برای مدت مشخص"""
    try:
        until = int(time.time()) + duration
        bot.restrict_chat_member(
            chat_id, 
            user_id, 
            until_date=until, 
            can_send_messages=False,
            can_send_media_messages=False 
        )
        return True
    except Exception as e:
        return False

# ************************************************
# هندلرها و فیلترها
# ************************************************

@bot.message_handler(content_types=['new_chat_members', 'left_chat_member'])
def handle_system_msgs(message):
    chat_id = message.chat.id
    settings = get_settings(chat_id)
    
    if settings['remove_system_msgs']:
        delete_msg(chat_id, message.message_id)

    if message.new_chat_members:
        for user in message.new_chat_members:
            if user.id == bot.get_me().id: continue
            
            if settings['anti_tabchi_enabled']:
                flood_control[user.id] = [] 

            if settings['welcome_msg']:
                mention = f"[{user.first_name}](tg://user?id={user.id})"
                text = settings['welcome_msg'].replace('{user_mention}', mention).replace('{chat_title}', message.chat.title)
                try:
                    sent = bot.send_message(chat_id, text, parse_mode='Markdown')
                    if settings['delete_welcome_after'] > 0:
                        threading.Timer(settings['delete_welcome_after'], delete_msg, args=[chat_id, sent.message_id]).start()
                except Exception: pass

# هندلر عمومی برای فیلترها (فقط پیام‌های عادی و بدون دستور / را پردازش می‌کند)
@bot.message_handler(func=lambda m: m.text is None or not m.text.startswith('/'), 
                     content_types=['text', 'photo', 'video', 'document', 'sticker', 'audio', 'voice', 'video_note', 'contact', 'location', 'venue', 'poll', 'dice'])
def handle_content(message):
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if is_admin(chat_id, user_id): return

    settings = get_settings(chat_id)

    # 1. قفل سراسری چت
    if settings['chat_locked']:
        delete_msg(chat_id, message.message_id)
        return

    # 2. قفل رسانه
    if message.content_type in settings['media_locks'] and settings['media_locks'][message.content_type]:
        delete_msg(chat_id, message.message_id)
        return

    text = message.text or message.caption or ""
    
    # 3. ضد اسپم (Flood) و ضد تبچی
    if settings['anti_flood_limit'] > 0:
        now = time.time()
        user_flood = flood_control.get(user_id, [])
        user_flood = [t for t in user_flood if now - t < 5]
        user_flood.append(now)
        flood_control[user_id] = user_flood
        
        if len(user_flood) > settings['anti_flood_limit']:
            delete_msg(chat_id, message.message_id)
            mute_user(chat_id, user_id, 1800) 
            return

    # 4. فیلتر کلمات ممنوعه
    if settings['bad_words'] and text:
        for word in settings['bad_words']:
            if re.search(r'\b' + re.escape(word) + r'\b', text, re.IGNORECASE):
                delete_msg(chat_id, message.message_id)
                mute_user(chat_id, user_id, 600)
                return

    # 5. محدودیت کاراکتر
    if settings['max_chars'] > 0 and len(text) > settings['max_chars']:
        delete_msg(chat_id, message.message_id)
        return

    # 6. ضد لینک
    link_regex = r'(?:https?://|www\.)[^\s<>"]+'
    has_link = False
    
    if re.search(link_regex, text):
        has_link = True
    
    if not has_link and (message.entities or message.caption_entities):
        ents = message.entities or message.caption_entities
        for e in ents:
            if e.type in ['url', 'text_link']:
                has_link = True
                break
    
    if has_link:
        delete_msg(chat_id, message.message_id)
        if settings['mute_on_link']:
            mute_user(chat_id, user_id, 3600) 

# ************************************************
# پنل مدیریت و دستورات (امن شده برای ادمین)
# ************************************************

MEDIA_NAMES = {
    'photo': '🖼 عکس',
    'video': '📹 ویدئو',
    'document': '📄 سند (فایل)',
    'sticker': '🎭 استیکر',
    'audio': '🎵 موسیقی',
    'voice': '🎤 پیام صوتی',
    'video_note': '🎥 ویدئو نوت',
}

# --- ساختار منوی اصلی ---
def get_main_panel_keyboard(settings):
    """ساخت کیبورد پنل اصلی"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    s = settings
    
    btn1 = types.InlineKeyboardButton(f"پ. سیستم: {'❌' if s['remove_system_msgs'] else '✅'}", callback_data='toggle_sys')
    btn2 = types.InlineKeyboardButton(f"سکوت لینک: {'✅' if s['mute_on_link'] else '❌'}", callback_data='toggle_mute_link')
    btn3 = types.InlineKeyboardButton(f"ضد تبچی: {'✅' if s['anti_tabchi_enabled'] else '❌'}", callback_data='toggle_tabchi')
    btn4 = types.InlineKeyboardButton(f"قفل چت: {'🔒' if s['chat_locked'] else '🔓'}", callback_data='toggle_chat')

    btn_media = types.InlineKeyboardButton("📷 تنظیمات رسانه ⬅️", callback_data='show_media_panel')
    
    markup.add(btn1, btn2, btn3, btn4, btn_media)
    return markup

# --- ساختار منوی رسانه ---
def get_media_panel_keyboard(settings):
    """ساخت کیبورد پنل تنظیمات رسانه"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    locks = settings['media_locks']
    
    for media_type, name in MEDIA_NAMES.items():
        is_locked = locks.get(media_type, False)
        # 🟢 مجاز است (قفل نیست) | 🔴 حذف می‌شود (قفل است)
        emoji = '🔴' if is_locked else '🟢' 
        
        btn = types.InlineKeyboardButton(f"{emoji} {name}", callback_data=f'toggle_media_{media_type}')
        markup.add(btn)

    btn_back = types.InlineKeyboardButton("بازگشت به منوی اصلی 🔙", callback_data='show_main_panel')
    markup.add(btn_back)
    return markup


@bot.message_handler(commands=['panel', 'پنل'])
def cmd_panel(message):
    """نمایش پنل مدیریتی"""
    # **شرط امنیتی ادمین فعال شد**
    if not is_admin(message.chat.id, message.from_user.id): return
    
    settings = get_settings(message.chat.id)
    bot.send_message(message.chat.id, "⚙️ **پنل اصلی تنظیمات گروه**", reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    """هندلر دکمه‌های شیشه‌ای (چندسطحی)"""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    
    # **شرط امنیتی ادمین فعال شد**
    if not is_admin(chat_id, call.from_user.id):
        return bot.answer_callback_query(call.id, "فقط ادمین می‌تواند تنظیمات را تغییر دهد.")
        
    settings = get_settings(chat_id)
    d = call.data
    
    # --- مدیریت جابجایی بین منوها ---
    if d == 'show_media_panel':
        bot.edit_message_text("📷 **تنظیمات قفل رسانه**\n(🔴: حذف می‌شود | 🟢: مجاز است)", chat_id, msg_id, 
                              reply_markup=get_media_panel_keyboard(settings), parse_mode='Markdown')
        return bot.answer_callback_query(call.id)
        
    elif d == 'show_main_panel':
        bot.edit_message_text("⚙️ **پنل اصلی تنظیمات گروه**", chat_id, msg_id, 
                              reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')
        return bot.answer_callback_query(call.id)

    # --- مدیریت Toggle های منوی اصلی و رسانه ---
    elif d == 'toggle_sys': settings['remove_system_msgs'] = not settings['remove_system_msgs']
    elif d == 'toggle_mute_link': settings['mute_on_link'] = not settings['mute_on_link']
    elif d == 'toggle_chat': settings['chat_locked'] = not settings['chat_locked']
    elif d == 'toggle_tabchi': settings['anti_tabchi_enabled'] = not settings['anti_tabchi_enabled']
    
    elif d.startswith('toggle_media_'):
        media_type = d.split('_')[-1]
        if media_type in settings['media_locks']:
            settings['media_locks'][media_type] = not settings['media_locks'][media_type]
            save_settings(chat_id, settings)
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=get_media_panel_keyboard(settings))
            return bot.answer_callback_query(call.id, "✅ تنظیمات رسانه با موفقیت ذخیره شد.")
        else:
            return bot.answer_callback_query(call.id, "خطا در شناسایی نوع رسانه!")

    # ذخیره و به‌روزرسانی پنل اصلی
    save_settings(chat_id, settings)
    bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=get_main_panel_keyboard(settings))
    bot.answer_callback_query(call.id, "✅ تنظیمات با موفقیت ذخیره شد.")
    
@bot.message_handler(commands=['clean', 'پاکسازی'])
def cmd_clean(message):
    """حذف n پیام آخر گروه"""
    if not is_admin(message.chat.id, message.from_user.id): return
    delete_msg(message.chat.id, message.message_id)
    
    try:
        count = min(int(message.text.split()[1]), 50)
    except: count = 10
    
    for i in range(1, count + 1):
        delete_msg(message.chat.id, message.message_id - i)
    
    sent = bot.send_message(message.chat.id, f"🗑️ **{count}** پیام آخر حذف شد.", parse_mode='Markdown')
    threading.Timer(5, delete_msg, args=[message.chat.id, sent.message_id]).start()

@bot.message_handler(commands=['mute', 'سکوت'])
def cmd_mute(message):
    """سکوت کردن کاربر با ریپلای"""
    if not is_admin(message.chat.id, message.from_user.id) or not message.reply_to_message: return
    target_user = message.reply_to_message.from_user
    mute_duration = 86400
    
    if mute_user(message.chat.id, target_user.id, mute_duration):
        bot.reply_to(message, f"🚫 کاربر **{target_user.first_name}** با موفقیت ساکت شد.", parse_mode='Markdown')
    else:
         bot.reply_to(message, "❌ خطا: ربات دسترسی محدودسازی ندارد.")


@bot.message_handler(commands=['unmute', 'آزادسازی'])
def cmd_unmute(message):
    """آزادسازی کاربر با ریپلای"""
    if not is_admin(message.chat.id, message.from_user.id) or not message.reply_to_message: return
    target_user = message.reply_to_message.from_user
    
    try:
        bot.restrict_chat_member(
            message.chat.id, 
            target_user.id, 
            can_send_messages=True, 
            can_send_media_messages=True
        )
        bot.reply_to(message, f"✅ کاربر **{target_user.first_name}** با موفقیت آزاد شد.", parse_mode='Markdown')
    except Exception:
         bot.reply_to(message, "❌ خطا: ربات نتوانست کاربر را آزاد کند.")


# ************************************************
# راه اندازی Webhook و سرور Flask
# ************************************************

@app.route('/' + API_TOKEN, methods=['POST'])
def getMessage():
    """دریافت آپدیت‌های تلگرام از طریق POST"""
    if flask.request.headers.get('content-type') == 'application/json':
        json_string = flask.request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "!", 200
    else:
        flask.abort(403)

@app.route("/")
def webhook():
    """تنظیم Webhook برای تلگرام (وقتی برای اولین بار آدرس باز شود)"""
    bot.remove_webhook()
    
    webhook_url = f"https://{WEBHOOK_HOST}/{API_TOKEN}" if WEBHOOK_HOST else None
    
    if webhook_url:
        bot.set_webhook(url=webhook_url)
        return "✅ Webhook با موفقیت تنظیم شد و ربات آماده کار است!", 200
    else:
        return "❌ خطا: متغیر RENDER_EXTERNAL_HOSTNAME تنظیم نشده است. لطفا آدرس دامنه رندر را بررسی کنید.", 500

if __name__ == "__main__":
    print("===================================================")
    print("        🚀 ربات ضد لینک و دیتابیس فعال شد.           ")
    print("===================================================")
    init_db()
    app.run(host="0.0.0.0", port=WEBHOOK_PORT)