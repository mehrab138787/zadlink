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
    
    # تنظیمات جدید و پیشرفته
    'anti_forward_enabled': True,       
    'anti_tag_username_enabled': False, 
    'remove_pin_service_msgs': True,    
    'warn_limit': 3,                    
    'warnings': {},                     
    'warn_punishment_duration': 1800,   # 30 دقیقه
    'log_channel_id': None,             
    
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
        # ادغام تنظیمات ذخیره شده با تنظیمات پیش‌فرض برای پشتیبانی از کلیدهای جدید
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

# --- توابع مربوط به سیستم گزارش‌دهی (Log) ---

def send_log(chat_id, action, user_info, target_info=None, details=""):
    """ارسال پیام گزارش به کانال لاگ تنظیم شده"""
    settings = get_settings(chat_id)
    log_channel_id = settings.get('log_channel_id')
    
    if not log_channel_id:
        return

    # Log Channel ID must be negative (for group/channel IDs)
    if log_channel_id > 0:
        # در محیط واقعی، شما باید ID واقعی کانال لاگ را اینجا داشته باشید.
        # اما برای دیتابیس، ID چت ذخیره شده است. 
        # از ID ذخیره شده استفاده می‌کنیم که باید منفی باشد
        log_channel_id = chat_id 
        
    # اگر ID لاگ، ID خود چت اصلی باشد، لاگ را ارسال نمی‌کنیم تا چت شلوغ نشود.
    if log_channel_id == chat_id:
        return
        
    log_text = f"🤖 **{action}**\n"
    # برای ساخت لینک پیام به گروه، باید از یک ترفند استفاده کنیم:
    # URL: https://t.me/c/ChannelID/MessageID 
    # ChannelID برای گروه ها عدد chat_id بدون -100 است.
    chat_link_id = str(chat_id).replace('-100', '')
    log_text += f"🏠 گروه: [{bot.get_chat(chat_id).title}](https://t.me/c/{chat_link_id}/1)\n"
    log_text += f"👤 کاربر: {user_info}\n"
    if target_info:
        log_text += f"🎯 هدف: {target_info}\n"
    if details:
        log_text += f"📝 جزئیات: {details}\n"
    
    try:
        bot.send_message(log_channel_id, log_text, parse_mode='Markdown')
    except Exception:
        pass 


# --- توابع مربوط به سیستم اخطار (Warn) ---

def get_user_warnings(chat_id, user_id):
    settings = get_settings(chat_id)
    return settings.get('warnings', {}).get(str(user_id), 0)

def set_user_warnings(chat_id, user_id, count):
    settings = get_settings(chat_id)
    warnings = settings.get('warnings', {})
    warnings[str(user_id)] = count
    settings['warnings'] = warnings
    save_settings(chat_id, settings)

def warn_user_action(chat_id, user, message_id_to_reply=None, reason=""):
    """اعمال اخطار به کاربر و محدود کردن در صورت رسیدن به سقف"""
    current_warnings = get_user_warnings(chat_id, user.id)
    settings = get_settings(chat_id)
    warn_limit = settings['warn_limit']
    
    user_mention = f"[{user.first_name}](tg://user?id={user.id})"
    
    if current_warnings < warn_limit - 1:
        new_warnings = current_warnings + 1
        set_user_warnings(chat_id, user.id, new_warnings)
        
        reply_text = (
            f"⚠️ اخطار ({new_warnings}/{warn_limit})\n"
            f"👤 کاربر: {user_mention}\n"
            f"دلیل: {reason}\n"
            f"اگر {warn_limit} اخطار بگیرید، به مدت {int(settings['warn_punishment_duration']/60)} دقیقه محدود خواهید شد."
        )
        send_log(chat_id, "اخطار (Warn)", user_mention, details=f"تعداد: {new_warnings}/{warn_limit}. دلیل: {reason}")

    else:
        # Final warning reached, apply punishment
        new_warnings = 0
        set_user_warnings(chat_id, user.id, new_warnings)
        
        duration = settings['warn_punishment_duration']
        mute_user(chat_id, user.id, duration)
        
        reply_text = (
            f"🚫 **محدودیت اعمال شد!**\n"
            f"👤 کاربر: {user_mention}\n"
            f"شما به حد نصاب اخطار رسیدید ({warn_limit}/{warn_limit}). به مدت {int(duration/60)} دقیقه محدود شدید."
        )
        send_log(chat_id, "محدودیت توسط اخطار", user_mention, details=f"به دلیل رسیدن به {warn_limit} اخطار. مدت: {int(duration/60)} دقیقه.")
        
    bot.send_message(chat_id, reply_text, parse_mode='Markdown', reply_to_message_id=message_id_to_reply or None)


# --- توابع مربوط به Ban/Unban/Unmute ---

def ban_user_action(chat_id, target_user, admin_id, message_id_to_delete=None):
    """بن کردن کاربر هدف"""
    user_mention = f"[{target_user.first_name}](tg://user?id={target_user.id})"
    
    if is_admin(chat_id, target_user.id):
        return bot.send_message(chat_id, "❌ **شما نمی‌توانید یک مدیر گروه را بن کنید!**", 
                                reply_to_message_id=message_id_to_delete or None, 
                                parse_mode='Markdown')
        
    try:
        bot.ban_chat_member(chat_id, target_user.id)
        reply_id = message_id_to_delete or None
        
        bot.send_message(chat_id, 
                         f"🚫 کاربر **{target_user.first_name}** با موفقیت از گروه **بن (اخراج دائم)** شد.", 
                         parse_mode='Markdown', 
                         reply_to_message_id=reply_id)
        send_log(chat_id, "بن (Ban)", user_mention, details=f"توسط ادمین: {admin_id}")
        return True
    except Exception:
         bot.send_message(chat_id, f"❌ خطا: ربات نتوانست کاربر را بن کند. (ممکن است دسترسی کافی نداشته باشد.)", 
                          reply_to_message_id=message_id_to_delete or None)
         return False

def unban_user_action(chat_id, target_user, admin_id, message_id_to_delete=None):
    """آزادسازی کاربر (رفع بن)"""
    user_mention = f"[{target_user.first_name}](tg://user?id={target_user.id})"
    try:
        bot.unban_chat_member(chat_id, target_user.id)
        reply_id = message_id_to_delete or None
        
        bot.send_message(chat_id, 
                         f"✅ کاربر **{target_user.first_name}** با موفقیت از لیست سیاه **آزاد (Unban)** شد.", 
                         parse_mode='Markdown',
                         reply_to_message_id=reply_id)
        send_log(chat_id, "رفع بن (Unban)", user_mention, details=f"توسط ادمین: {admin_id}")
        return True
    except Exception:
         bot.send_message(chat_id, f"❌ خطا: ربات نتوانست کاربر را آزاد کند. (ممکن است دسترسی کافی نداشته باشد.)", 
                          reply_to_message_id=message_id_to_delete or None)
         return False

def cmd_unmute_finalizer(chat_id, target_user, admin_id, message_id_to_reply=None):
    """آزادسازی سکوت (Unmute) کاربر"""
    user_mention = f"[{target_user.first_name}](tg://user?id={target_user.id})"
    try:
        bot.restrict_chat_member(
            chat_id, 
            target_user.id, 
            can_send_messages=True, 
            can_send_media_messages=True
        )
        bot.send_message(chat_id, f"✅ کاربر **{target_user.first_name}** با موفقیت از حالت سکوت خارج شد.", 
                         parse_mode='Markdown', reply_to_message_id=message_id_to_reply)
        send_log(chat_id, "رفع سکوت (Unmute)", user_mention, details=f"توسط ادمین: {admin_id}")
    except Exception:
         bot.send_message(chat_id, "❌ خطا: ربات نتوانست کاربر را آزاد کند.", 
                          reply_to_message_id=message_id_to_reply)


# ************************************************
# هندلرها و فیلترها
# ************************************************

@bot.message_handler(content_types=['new_chat_members', 'left_chat_member', 'pinned_message'])
def handle_system_msgs(message):
    chat_id = message.chat.id
    settings = get_settings(chat_id)
    
    # 1. حذف پیام‌های ورود/خروج
    if message.content_type in ['new_chat_members', 'left_chat_member']:
        if settings['remove_system_msgs']:
            delete_msg(chat_id, message.message_id)

    # 2. حذف پیام‌های پین کردن
    if message.content_type == 'pinned_message':
        if settings.get('remove_pin_service_msgs', True):
            delete_msg(chat_id, message.message_id)
    
    # 3. ارسال پیام خوش‌آمدگویی
    if message.new_chat_members:
        for user in message.new_chat_members:
            if user.id == bot.get_me().id: continue
            
            if settings['anti_tabchi_enabled']:
                # Reset anti-flood for new user
                flood_control[user.id] = [] 

            if settings['welcome_msg']:
                mention = f"[{user.first_name}](tg://user?id={user.id})"
                text = settings['welcome_msg'].replace('{user_mention}', mention).replace('{chat_title}', message.chat.title)
                try:
                    sent = bot.send_message(chat_id, text, parse_mode='Markdown')
                    
                    delete_after = settings.get('delete_welcome_after', 60) 
                    if delete_after > 0:
                        threading.Timer(delete_after, delete_msg, args=[chat_id, sent.message_id]).start()
                except Exception: pass

@bot.message_handler(func=lambda m: m.text is None or not m.text.startswith('/'), 
                     content_types=['text', 'photo', 'video', 'document', 'sticker', 'audio', 'voice', 'video_note', 'contact', 'location', 'venue', 'poll', 'dice'])
def handle_content(message):
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    settings = get_settings(chat_id)
    
    # --- مدیریت دستورات بدون اسلش برای ادمین (بن، رفع بن، پنل) ---
    if is_admin(chat_id, user_id): 
        text_lower = (message.text or "").lower().strip()
        
        # **FIXED: تشخیص دستور 'پنل' بدون اسلش**
        if text_lower in ['پنل', 'panel']:
            delete_msg(chat_id, message.message_id) # حذف دستور "پنل"
            bot.send_message(chat_id, "⚙️ **پنل اصلی تنظیمات گروه (امنیت)**", 
                             reply_markup=get_main_panel_keyboard(settings), 
                             parse_mode='Markdown')
            return
            
        if message.reply_to_message:
            target_user = message.reply_to_message.from_user
            
            # دستور بن (بدون اسلش)
            if text_lower in ['بن', 'ban']:
                delete_msg(chat_id, message.message_id)
                ban_user_action(chat_id, target_user, user_id, message.reply_to_message.message_id)
                return
            
            # دستور رفع بن (بدون اسلش)
            if text_lower in ['رفع بن', 'unban']:
                delete_msg(chat_id, message.message_id)
                unban_user_action(chat_id, target_user, user_id, message.reply_to_message.message_id)
                return

            # دستور آزادسازی سکوت (بدون اسلش)
            if text_lower in ['آزادسازی', 'unmute']:
                delete_msg(chat_id, message.message_id)
                cmd_unmute_finalizer(chat_id, target_user, user_id, message.reply_to_message.message_id)
                return
        
        return # ادمین‌ها از فیلترها معاف هستند

    # --- فیلترهای ضد تخلف برای کاربران عادی ---

    # 1. قفل سراسری چت
    if settings['chat_locked']:
        delete_msg(chat_id, message.message_id)
        return

    # 2. قفل رسانه
    if message.content_type in settings['media_locks'] and settings['media_locks'][message.content_type]:
        delete_msg(chat_id, message.message_id)
        send_log(chat_id, "حذف (قفل رسانه)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details=f"نوع: {message.content_type}")
        return

    text = message.text or message.caption or ""
    
    # 3. ضد فوروارد
    if settings.get('anti_forward_enabled') and (message.forward_from or message.forward_from_chat):
        delete_msg(chat_id, message.message_id)
        send_log(chat_id, "حذف (فوروارد)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details="پیام فوروارد شده حذف شد.")
        return

    # 4. ضد یوزرنیم و تگ 
    if settings.get('anti_tag_username_enabled') and text:
        tag_username_regex = r'(@\w+)|(t\.me/\w+)'
        if re.search(tag_username_regex, text) or (message.entities and any(e.type == 'text_mention' for e in message.entities)):
            delete_msg(chat_id, message.message_id)
            send_log(chat_id, "حذف (تگ/یوزرنیم)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details="پیام حاوی یوزرنیم/تگ حذف شد.")
            return

    # 5. ضد اسپم (Flood) و ضد تبچی
    if settings['anti_flood_limit'] > 0:
        now = time.time()
        user_flood = flood_control.get(user_id, [])
        user_flood = [t for t in user_flood if now - t < 5]
        user_flood.append(now)
        flood_control[user_id] = user_flood
        
        if len(user_flood) > settings['anti_flood_limit']:
            delete_msg(chat_id, message.message_id)
            mute_user(chat_id, user_id, 1800) 
            send_log(chat_id, "محدودیت (Flood)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details="ارسال بیش از حد پیام")
            return

    # 6. فیلتر کلمات ممنوعه (با استفاده از Warn System)
    if settings['bad_words'] and text:
        for word in settings['bad_words']:
            # استفاده از regex برای مطابقت دقیق کلمه (برای جلوگیری از فیلتر شدن کلماتی که فقط شامل بخش کوچکی از فحش هستند)
            if re.search(r'\b' + re.escape(word) + r'\b', text, re.IGNORECASE):
                delete_msg(chat_id, message.message_id)
                send_log(chat_id, "حذف (کلمه ممنوعه)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details=f"حاوی کلمه: {word}")
                warn_user_action(chat_id, message.from_user, message.message_id, reason="استفاده از کلمات ممنوعه")
                return

    # 7. محدودیت کاراکتر
    if settings['max_chars'] > 0 and len(text) > settings['max_chars']:
        delete_msg(chat_id, message.message_id)
        send_log(chat_id, "حذف (کاراکتر زیاد)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details=f"طول پیام: {len(text)}")
        return

    # 8. ضد لینک
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
        send_log(chat_id, "حذف (لینک)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details="لینک تبلیغاتی/خارجی.")
        if settings['mute_on_link']:
            mute_user(chat_id, user_id, 3600)
            send_log(chat_id, "سکوت (لینک)", f"[{message.from_user.first_name}](tg://user?id={user_id})", details="سکوت ۱ ساعته به دلیل لینک.")

# ************************************************
# پنل مدیریت و دستورات ادمین
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

# --- Panel Keyboards ---

def get_main_panel_keyboard(settings):
    """ساخت کیبورد پنل اصلی (امنیت و قفل‌های عمومی)"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    s = settings
    
    # ردیف ۱: قفل‌های عمومی
    btn_chat_lock = types.InlineKeyboardButton(f"🔒 قفل چت: {'فعال' if s['chat_locked'] else 'غیرفعال'}", callback_data='toggle_chat')
    btn_anti_forward = types.InlineKeyboardButton(f"↗️ ضد فوروارد: {'فعال' if s.get('anti_forward_enabled') else 'غیرفعال'}", callback_data='toggle_anti_forward')
    markup.add(btn_chat_lock, btn_anti_forward)

    # ردیف ۲: قفل‌های امنیتی
    btn_tag_username = types.InlineKeyboardButton(f"👤 ضد تگ/یوزرنیم: {'فعال' if s.get('anti_tag_username_enabled') else 'غیرفعال'}", callback_data='toggle_anti_tag')
    btn_link = types.InlineKeyboardButton(f"🔗 محدودیت لینک: {'سکوت' if s['mute_on_link'] else 'فقط حذف'}", callback_data='toggle_mute_link')
    markup.add(btn_tag_username, btn_link)
    
    # ردیف ۳: مدیریت پیام‌های سرویس
    btn_sys = types.InlineKeyboardButton(f"🗑️ حذف ورود/خروج: {'فعال' if s['remove_system_msgs'] else 'غیرفعال'}", callback_data='toggle_sys')
    btn_pin_del = types.InlineKeyboardButton(f"📌 حذف پیام‌های پین: {'فعال' if s.get('remove_pin_service_msgs') else 'غیرفعال'}", callback_data='toggle_pin_del')
    markup.add(btn_sys, btn_pin_del)

    # ردیف ۴: منوهای فرعی
    btn_media = types.InlineKeyboardButton("📷 تنظیمات قفل رسانه ⬅️", callback_data='show_media_panel')
    btn_advanced = types.InlineKeyboardButton("⚙️ قوانین و ابزارها ➡️", callback_data='show_advanced_panel')
    markup.add(btn_media, btn_advanced)
    
    # ردیف ۵: ابزارهای مدیریتی
    btn_unban = types.InlineKeyboardButton("آزادسازی کاربر (Unban) 🔓", callback_data='start_unban_process')
    btn_unmute = types.InlineKeyboardButton("آزادسازی سکوت (Unmute) 🗣️", callback_data='start_unmute_process')
    markup.add(btn_unban, btn_unmute)

    # ردیف آخر: بستن پنل
    btn_close = types.InlineKeyboardButton("بستن پنل و حذف پیام 🗑️", callback_data='close_panel')
    markup.add(btn_close)
    return markup

def get_media_panel_keyboard(settings):
    """ساخت کیبورد پنل تنظیمات رسانه"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    locks = settings['media_locks']
    
    for media_type, name in MEDIA_NAMES.items():
        is_locked = locks.get(media_type, False)
        emoji = '🔴 حذف می‌شود' if is_locked else '🟢 مجاز است' 
        
        btn = types.InlineKeyboardButton(f"{name}: {emoji}", callback_data=f'toggle_media_{media_type}')
        markup.add(btn)

    btn_back = types.InlineKeyboardButton("بازگشت به پنل اصلی 🔙", callback_data='show_main_panel')
    markup.add(btn_back)
    return markup
    
def get_advanced_panel_keyboard(settings):
    """ساخت کیبورد پنل تنظیمات پیشرفته (قوانین، اخطار، گزارش)"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # بخش ۱: اخطار و قوانین
    warn_text = f"🚨 سقف اخطار: {settings.get('warn_limit')} | جریمه: {int(settings.get('warn_punishment_duration', 1800)/60)} دقیقه"
    markup.add(types.InlineKeyboardButton(warn_text, callback_data='edit_warn_limit'))

    max_char_text = f"📏 محدودیت کاراکتر: {settings.get('max_chars') or 'غیرفعال'}"
    markup.add(types.InlineKeyboardButton(max_char_text, callback_data='edit_max_chars'))

    flood_text = f"🛑 محدودیت Flood/اسپم: {settings.get('anti_flood_limit')}"
    markup.add(types.InlineKeyboardButton(flood_text, callback_data='edit_flood_limit'))
    
    # بخش ۲: خوش‌آمدگویی و گزارش
    markup.add(types.InlineKeyboardButton("📝 ویرایش متن خوش‌آمدگویی ✍️", callback_data='edit_welcome_msg'))
    welcome_timer_text = f"⏱️ حذف خوش‌آمدگویی پس از: {settings.get('delete_welcome_after')} ثانیه"
    markup.add(types.InlineKeyboardButton(welcome_timer_text, callback_data='edit_welcome_timer'))
    
    log_status = "✅ فعال" if settings.get('log_channel_id') else "❌ غیرفعال"
    log_text = f"📡 کانال گزارش‌دهی: {log_status}"
    markup.add(types.InlineKeyboardButton(log_text, callback_data='show_log_settings'))
    
    markup.add(types.InlineKeyboardButton("بازگشت به پنل اصلی 🔙", callback_data='show_main_panel'))
    return markup

# --- توابع مربوط به ویرایش متغیرهای عددی ---

def send_number_editor_prompt(call, setting_key, prompt_text):
    """ارسال پیام برای دریافت مقدار عددی جدید"""
    settings = get_settings(call.message.chat.id)
    current_value = settings.get(setting_key)
    
    full_prompt = (
        f"🔢 **{prompt_text}**\n\n"
        f"مقدار فعلی: **{current_value}**\n"
        "_لطفا عدد جدید را وارد کنید (یا 0 برای غیرفعال کردن)._"
    )
    
    bot.answer_callback_query(call.id, "در حال ورود به حالت ویرایش عددی...")
    
    sent_msg = bot.send_message(
        call.message.chat.id, 
        full_prompt, 
        parse_mode='Markdown',
        reply_markup=types.ForceReply(selective=True)
    )
    
    bot.register_next_step_handler(sent_msg, process_new_number, setting_key)
    delete_msg(call.message.chat.id, call.message.message_id)

def process_new_number(message, setting_key):
    """ذخیره مقدار عددی جدید"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if not is_admin(chat_id, user_id):
        return bot.send_message(chat_id, "❌ شما دسترسی ادمین برای تغییر این تنظیمات را ندارید.")
    
    try:
        new_value = int(message.text.strip())
        if new_value < 0: raise ValueError # عدد منفی مجاز نیست
        
        settings = get_settings(chat_id)
        settings[setting_key] = new_value
        
        # اگر سقف اخطار عوض شود، اخطارهای فعلی کاربران را صفر می‌کنیم 
        if setting_key == 'warn_limit':
             settings['warnings'] = {}
             bot.send_message(chat_id, "⚠️ **تعداد اخطارهای کاربران صفر شد** تا با سقف جدید هماهنگ باشد.", parse_mode='Markdown')

        save_settings(chat_id, settings)
        
        bot.send_message(
            chat_id, 
            f"✅ **مقدار جدید {new_value} برای تنظیمات با موفقیت ذخیره شد.**\n\n"
            "برای ادامه مدیریت، می‌توانید مجدداً دستور /panel را ارسال کنید.", 
            parse_mode='Markdown'
        )
        delete_msg(chat_id, message.message_id)
        
    except ValueError:
        bot.send_message(chat_id, "❌ ورودی نامعتبر است. لطفاً فقط یک عدد صحیح و مثبت وارد کنید.")

# --- توابع مربوط به ویرایش متن خوش‌آمدگویی ---

def send_welcome_editor_prompt(call, settings):
    """ارسال پیام برای دریافت متن خوش‌آمدگویی جدید"""
    current_msg = settings['welcome_msg']
    
    prompt_text = (
        "✍️ **لطفاً متن جدید پیام خوش‌آمدگویی را ارسال کنید.**\n\n"
        "تگ‌های **اجباری**:\n"
        "• `{user_mention}`: برای منشن کردن کاربر جدید\n"
        "• `{chat_title}`: برای نمایش نام گروه\n\n"
        "**متن فعلی:**\n"
        f"```\n{current_msg}\n```\n"
        "\n_توجه: فقط پیام بعدی شما به عنوان متن خوش‌آمدگویی ذخیره خواهد شد._"
    )
    
    bot.answer_callback_query(call.id, "در حال ورود به حالت ویرایش پیام خوش‌آمدگویی...")
    
    sent_msg = bot.send_message(
        call.message.chat.id, 
        prompt_text, 
        parse_mode='Markdown',
        reply_markup=types.ForceReply(selective=True)
    )
    
    bot.register_next_step_handler(sent_msg, process_new_welcome_msg)
    
    delete_msg(call.message.chat.id, call.message.message_id)


def process_new_welcome_msg(message):
    """ذخیره متن خوش‌آمدگویی جدید ارسال شده توسط ادمین"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if not is_admin(chat_id, user_id):
        return bot.send_message(chat_id, "❌ شما دسترسی ادمین برای تغییر این تنظیمات را ندارید.")
    
    new_text = message.text
    
    if new_text and new_text.strip():
        new_text_to_save = new_text.strip()
        
        if '{user_mention}' not in new_text_to_save:
            bot.send_message(chat_id, "⚠️ **تگ `{user_mention}` برای منشن کاربر الزامی است!** این تگ به انتهای پیام شما اضافه شد. لطفا دفعه بعد آن را در متن دلخواه خود قرار دهید.", parse_mode='Markdown')
            new_text_to_save = f"{new_text_to_save} {{user_mention}}"
            
        settings = get_settings(chat_id)
        settings['welcome_msg'] = new_text_to_save
        save_settings(chat_id, settings)
        
        bot.send_message(
            chat_id, 
            "✅ **پیام خوش‌آمدگویی جدید با موفقیت ذخیره شد.**\n\n"
            "برای ادامه مدیریت، می‌توانید مجدداً دستور /panel را ارسال کنید.", 
            parse_mode='Markdown'
        )
        delete_msg(chat_id, message.message_id)
    else:
        bot.send_message(chat_id, "❌ متن خوش‌آمدگویی خالی است یا دستور ویرایش لغو شد. لطفاً دوباره تلاش کنید.")

# --- توابع مدیریت دکمه‌های Unban/Unmute از پنل ---

def start_management_process(call, action_type):
    """شروع فرآیند مدیریت (Unban/Unmute) از طریق پنل"""
    
    if action_type == 'unban':
        prompt_text = "🔓 **برای آزادسازی (Unban) کاربر از لیست سیاه، روی پیام او ریپلای کنید و سپس دکمه زیر را بزنید.**"
        callback_prefix = 'finalize_unban'
    else: # unmute
        prompt_text = "🗣️ **برای آزادسازی سکوت (Unmute) کاربر، روی پیام او ریپلای کنید و سپس دکمه زیر را بزنید.**"
        callback_prefix = 'finalize_unmute'

    markup = types.InlineKeyboardMarkup()
    btn = types.InlineKeyboardButton("☑️ کاربر را آزاد کن (بعد از ریپلای)", callback_data=callback_prefix)
    markup.add(btn)
    
    bot.answer_callback_query(call.id, f"در حال آماده‌سازی فرآیند {action_type}...")
    
    # حذف پنل اصلی و نمایش دکمه تایید
    delete_msg(call.message.chat.id, call.message.message_id)
    
    bot.send_message(
        call.message.chat.id,
        prompt_text,
        parse_mode='Markdown',
        reply_markup=markup
    )
    
# --- هندلر دستور پنل (با اسلش) ---

@bot.message_handler(commands=['panel', 'پنل'])
def cmd_panel(message):
    """نمایش پنل مدیریتی"""
    if not is_admin(message.chat.id, message.from_user.id): return
    
    settings = get_settings(message.chat.id)
    bot.send_message(message.chat.id, "⚙️ **پنل اصلی تنظیمات گروه (امنیت)**", reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    """هندلر دکمه‌های شیشه‌ای (چندسطحی)"""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    
    if not is_admin(chat_id, call.from_user.id):
        return bot.answer_callback_query(call.id, "فقط ادمین می‌تواند تنظیمات را تغییر دهد.")
        
    settings = get_settings(chat_id)
    d = call.data
    
    # --- مدیریت جابجایی بین منوها ---
    if d == 'show_media_panel':
        bot.edit_message_text("📷 **تنظیمات قفل رسانه (عکس، ویدئو و...)**\n\n🟢: مجاز است | 🔴: حذف می‌شود", chat_id, msg_id, 
                              reply_markup=get_media_panel_keyboard(settings), parse_mode='Markdown')
        return bot.answer_callback_query(call.id)
    
    elif d == 'show_advanced_panel':
        bot.edit_message_text("⚙️ **تنظیمات پیشرفته گروه (قوانین، اخطار و گزارش)**", chat_id, msg_id, 
                              reply_markup=get_advanced_panel_keyboard(settings), parse_mode='Markdown')
        return bot.answer_callback_query(call.id)
        
    elif d == 'show_main_panel':
        bot.edit_message_text("⚙️ **پنل اصلی تنظیمات گروه (امنیت)**", chat_id, msg_id, 
                              reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')
        return bot.answer_callback_query(call.id)

    # --- مدیریت ویرایش‌های متنی و عددی ---
    elif d == 'edit_welcome_msg': return send_welcome_editor_prompt(call, settings)
    elif d == 'edit_warn_limit': return send_number_editor_prompt(call, 'warn_limit', "🔢 لطفا حداکثر تعداد اخطار را وارد کنید (مثلا ۳).")
    elif d == 'edit_max_chars': return send_number_editor_prompt(call, 'max_chars', "🔢 لطفا حداکثر کاراکتر مجاز برای پیام‌ها را وارد کنید (مثلا ۱۰۰۰).")
    elif d == 'edit_flood_limit': return send_number_editor_prompt(call, 'anti_flood_limit', "🔢 لطفا سقف تعداد پیام در ۵ ثانیه را وارد کنید (مثلا ۵).")
    elif d == 'edit_welcome_timer': return send_number_editor_prompt(call, 'delete_welcome_after', "🔢 لطفا زمان حذف پیام خوش‌آمدگویی را بر حسب ثانیه وارد کنید (مثلا ۶۰).")
    
    # --- تنظیم کانال گزارش‌دهی ---
    elif d == 'show_log_settings':
        current_log = settings.get('log_channel_id')
        status = "✅ فعال" if current_log else "❌ غیرفعال"
        log_text = (
            f"📡 **وضعیت کانال گزارش‌دهی:** {status}\n\n"
            "برای فعال‌سازی:\n"
            "۱. ربات را در کانال مقصد ادمین کنید.\n"
            "۲. در کانال مقصد، دستور `/setlog` را ارسال کنید.\n\n"
            "برای غیرفعال‌سازی، روی دکمه زیر کلیک کنید."
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ غیرفعال‌سازی کانال گزارش‌دهی", callback_data='unset_log'))
        markup.add(types.InlineKeyboardButton("بازگشت 🔙", callback_data='show_advanced_panel'))
        bot.edit_message_text(log_text, chat_id, msg_id, reply_markup=markup, parse_mode='Markdown')
        return bot.answer_callback_query(call.id)

    elif d == 'unset_log':
        settings['log_channel_id'] = None
        save_settings(chat_id, settings)
        bot.edit_message_text("✅ کانال گزارش‌دهی غیرفعال شد. برای فعال‌سازی مجدد، از دستور `/setlog` در کانال مقصد استفاده کنید.", chat_id, msg_id)
        return bot.answer_callback_query(call.id)
    
    # --- شروع فرآیند مدیریتی از پنل ---
    elif d == 'start_unban_process': return start_management_process(call, 'unban')
    elif d == 'start_unmute_process': return start_management_process(call, 'unmute')

    # --- نهایی کردن فرآیند مدیریتی (پس از ریپلای) ---
    elif d in ['finalize_unban', 'finalize_unmute']:
        replied_message = call.message.reply_to_message
        if not replied_message:
            return bot.answer_callback_query(call.id, "❌ ابتدا باید روی پیام کاربر مورد نظر ریپلای کنید!")

        target_user = replied_message.from_user
        if d == 'finalize_unban':
            unban_user_action(chat_id, target_user, call.from_user.id, call.message.message_id)
        else: # finalize_unmute
            cmd_unmute_finalizer(chat_id, target_user, call.from_user.id, call.message.message_id)
            
        delete_msg(chat_id, call.message.message_id) 
        return bot.answer_callback_query(call.id, "✅ عملیات با موفقیت انجام شد.")

    # --- مدیریت بستن پنل (حذف پیام پنل) ---
    elif d == 'close_panel':
        delete_msg(chat_id, msg_id)
        return bot.answer_callback_query(call.id, "✅ پنل بسته شد. تغییرات ذخیره شده‌اند.")

    # --- مدیریت Toggle های منوی اصلی ---
    elif d == 'toggle_sys': settings['remove_system_msgs'] = not settings['remove_system_msgs']
    elif d == 'toggle_mute_link': settings['mute_on_link'] = not settings['mute_on_link']
    elif d == 'toggle_chat': settings['chat_locked'] = not settings['chat_locked']
    elif d == 'toggle_anti_forward': settings['anti_forward_enabled'] = not settings['anti_forward_enabled']
    elif d == 'toggle_anti_tag': settings['anti_tag_username_enabled'] = not settings['anti_tag_username_enabled']
    elif d == 'toggle_pin_del': settings['remove_pin_service_msgs'] = not settings['remove_pin_service_msgs']
    
    # --- مدیریت Toggle های منوی رسانه ---
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
    bot.edit_message_text("⚙️ **پنل اصلی تنظیمات گروه (امنیت)**", chat_id, msg_id, 
                          reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')
    bot.answer_callback_query(call.id, "✅ تنظیمات با موفقیت ذخیره شد.")


@bot.message_handler(commands=['warn', 'unwarn', 'اخطار', 'حذف_اخطار'])
def cmd_warn_unwarn(message):
    """مدیریت اخطار دادن و حذف اخطار"""
    chat_id = message.chat.id
    admin_id = message.from_user.id
    
    if not is_admin(chat_id, admin_id): return
    delete_msg(chat_id, message.message_id)

    if not message.reply_to_message:
        return bot.send_message(chat_id, "⚠️ لطفا روی پیام کاربر مورد نظر ریپلای کنید.")

    target_user = message.reply_to_message.from_user
    command = message.text.split()[0].lower().replace('/', '')
    user_mention = f"[{target_user.first_name}](tg://user?id={target_user.id})"

    if command in ['warn', 'اخطار']:
        warn_user_action(chat_id, target_user, message.reply_to_message.message_id, reason="توسط ادمین")
        send_log(chat_id, "اعمال اخطار", f"[{message.from_user.first_name}](tg://user?id={admin_id})", target_info=user_mention)
    
    elif command in ['unwarn', 'حذف_اخطار']:
        current_warnings = get_user_warnings(chat_id, target_user.id)
        if current_warnings > 0:
            new_warnings = current_warnings - 1
            set_user_warnings(chat_id, target_user.id, new_warnings)
            bot.send_message(chat_id, f"✅ اخطار کاربر **{target_user.first_name}** حذف شد. اخطارهای فعلی: {new_warnings}", parse_mode='Markdown')
            send_log(chat_id, "حذف اخطار", f"[{message.from_user.first_name}](tg://user?id={admin_id})", target_info=user_mention, details=f"اخطارهای جدید: {new_warnings}")
        else:
            bot.send_message(chat_id, f"⚠️ کاربر **{target_user.first_name}** اخطاری برای حذف ندارد.", parse_mode='Markdown')


@bot.message_handler(commands=['setlog', 'تنظیم_لاگ', 'unsetlog', 'حذف_لاگ'])
def cmd_set_log(message):
    """تنظیم کانال لاگ"""
    chat_id = message.chat.id
    admin_id = message.from_user.id
    
    if not is_admin(chat_id, admin_id): return
    delete_msg(chat_id, message.message_id)
    
    settings = get_settings(chat_id)
    command = message.text.split()[0].lower().replace('/', '')
    
    if command in ['setlog', 'تنظیم_لاگ']:
        settings['log_channel_id'] = chat_id 
        save_settings(chat_id, settings)
        bot.send_message(chat_id, "✅ **کانال گزارش‌دهی (Log Channel) روی این گروه تنظیم شد.** لطفا ربات را در این چت، مدیر کنید.", parse_mode='Markdown')
        
    elif command in ['unsetlog', 'حذف_لاگ']:
        settings['log_channel_id'] = None
        save_settings(chat_id, settings)
        bot.send_message(chat_id, "✅ **کانال گزارش‌دهی غیرفعال شد.**", parse_mode='Markdown')


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

# --- دستورات مدیریتی با اسلش ---
@bot.message_handler(commands=['mute', 'سکوت'])
def cmd_mute(message):
    """سکوت کردن کاربر با ریپلای"""
    if not is_admin(message.chat.id, message.from_user.id) or not message.reply_to_message: return
    delete_msg(message.chat.id, message.message_id)
    target_user = message.reply_to_message.from_user
    mute_duration = 86400 # 24 ساعت
    
    if mute_user(message.chat.id, target_user.id, mute_duration):
        bot.send_message(message.chat.id, f"🚫 کاربر **{target_user.first_name}** با موفقیت ساکت شد.", parse_mode='Markdown', reply_to_message_id=message.reply_to_message.message_id)
        send_log(message.chat.id, "سکوت (Mute)", f"[{target_user.first_name}](tg://user?id={target_user.id})", details=f"توسط ادمین: {message.from_user.id}")
    else:
         bot.send_message(message.chat.id, "❌ خطا: ربات دسترسی محدودسازی ندارد.", reply_to_message_id=message.reply_to_message.message_id)

@bot.message_handler(commands=['unmute', 'آزادسازی'])
def cmd_unmute(message):
    """آزادسازی کاربر با ریپلای (دستور)"""
    if not is_admin(message.chat.id, message.from_user.id) or not message.reply_to_message: return
    delete_msg(message.chat.id, message.message_id)
    target_user = message.reply_to_message.from_user
    cmd_unmute_finalizer(message.chat.id, target_user, message.from_user.id, message.reply_to_message.message_id)

@bot.message_handler(commands=['ban', 'بن'])
def cmd_ban(message):
    """بن کردن دائمی کاربر با ریپلای (دستور)"""
    chat_id = message.chat.id
    if not is_admin(chat_id, message.from_user.id) or not message.reply_to_message: return
    delete_msg(chat_id, message.message_id)
    target_user = message.reply_to_message.from_user
    ban_user_action(chat_id, target_user, message.from_user.id, message.reply_to_message.message_id)

@bot.message_handler(commands=['unban', 'رفع_بن'])
def cmd_unban(message):
    """رفع بن کاربر با ریپلای (دستور)"""
    chat_id = message.chat.id
    if not is_admin(chat_id, message.from_user.id) or not message.reply_to_message: return
    delete_msg(chat_id, message.message_id)
    target_user = message.reply_to_message.from_user
    unban_user_action(chat_id, target_user, message.from_user.id, message.reply_to_message.message_id)


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
    """تنظیم Webhook برای تلگرام"""
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