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

# --- ساختار منوی اصلی (ساده شده) ---
def get_main_panel_keyboard(settings):
    """ساخت کیبورد پنل اصلی با ساختار ساده و تک صفحه‌ای"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    s = settings
    
    # ردیف ۱: قفل‌های عمومی
    btn_chat_lock = types.InlineKeyboardButton(f"🔒 قفل چت: {'فعال' if s['chat_locked'] else 'غیرفعال'}", callback_data='toggle_chat')
    btn_anti_tabchi = types.InlineKeyboardButton(f"🤖 ضد اسپم/تبچی: {'فعال' if s['anti_tabchi_enabled'] else 'غیرفعال'}", callback_data='toggle_tabchi')
    markup.add(btn_chat_lock, btn_anti_tabchi)

    # ردیف ۲: تنظیمات لینک و پیام سیستمی
    btn_link = types.InlineKeyboardButton(f"🔗 محدودیت لینک: {'سکوت (Mute)' if s['mute_on_link'] else 'فقط حذف'}", callback_data='toggle_mute_link')
    btn_sys = types.InlineKeyboardButton(f"🗑️ حذف ورود/خروج: {'فعال' if s['remove_system_msgs'] else 'غیرفعال'}", callback_data='toggle_sys')
    markup.add(btn_link, btn_sys)
    
    # ردیف ۳: تنظیمات خوش‌آمدگویی
    btn_welcome = types.InlineKeyboardButton("📝 ویرایش متن خوش‌آمدگویی", callback_data='edit_welcome_msg')
    btn_media = types.InlineKeyboardButton("📷 تنظیمات قفل رسانه ⬅️", callback_data='show_media_panel')
    markup.add(btn_welcome, btn_media)

    # ردیف ۴: بستن پنل
    btn_close = types.InlineKeyboardButton("بستن پنل و حذف پیام 🗑️", callback_data='close_panel')
    markup.add(btn_close)
    return markup

# --- ساختار منوی رسانه ---
def get_media_panel_keyboard(settings):
    """ساخت کیبورد پنل تنظیمات رسانه"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    locks = settings['media_locks']
    
    for media_type, name in MEDIA_NAMES.items():
        is_locked = locks.get(media_type, False)
        # 🔴 حذف می‌شود (قفل است) | 🟢 مجاز است (قفل نیست)
        emoji = '🔴 حذف می‌شود' if is_locked else '🟢 مجاز است' 
        
        btn = types.InlineKeyboardButton(f"{name}: {emoji}", callback_data=f'toggle_media_{media_type}')
        markup.add(btn)

    btn_back = types.InlineKeyboardButton("بازگشت به پنل اصلی 🔙", callback_data='show_main_panel')
    markup.add(btn_back)
    return markup

# --- توابع مدیریت ویرایش پیام خوش‌آمدگویی ---
def send_welcome_editor_prompt(call, settings):
    """ ارسال پیام برای دریافت متن خوش‌آمدگویی جدید"""
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
        
        # **تضمین وجود {user_mention} (اجباری)**
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


@bot.message_handler(commands=['panel', 'پنل'])
def cmd_panel(message):
    """نمایش پنل مدیریتی"""
    if not is_admin(message.chat.id, message.from_user.id): return
    
    settings = get_settings(message.chat.id)
    bot.send_message(message.chat.id, "⚙️ **پنل اصلی تنظیمات گروه**", reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')


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
        
    elif d == 'show_main_panel':
        # هنگام بازگشت به پنل اصلی، مطمئن می‌شویم محتوای پیام (تکست) هم به‌روزرسانی شود.
        bot.edit_message_text("⚙️ **پنل اصلی تنظیمات گروه**", chat_id, msg_id, 
                              reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')
        return bot.answer_callback_query(call.id)

    # --- مدیریت ویرایش پیام خوش‌آمدگویی ---
    elif d == 'edit_welcome_msg':
        return send_welcome_editor_prompt(call, settings)
    
    # --- مدیریت بستن پنل (حذف پیام پنل) ---
    elif d == 'close_panel':
        delete_msg(chat_id, msg_id)
        return bot.answer_callback_query(call.id, "✅ پنل بسته شد. تغییرات ذخیره شده‌اند.")

    # --- مدیریت Toggle های منوی اصلی (همه در یک صفحه) ---
    elif d == 'toggle_sys': settings['remove_system_msgs'] = not settings['remove_system_msgs']
    elif d == 'toggle_mute_link': settings['mute_on_link'] = not settings['mute_on_link']
    elif d == 'toggle_chat': settings['chat_locked'] = not settings['chat_locked']
    elif d == 'toggle_tabchi': settings['anti_tabchi_enabled'] = not settings['anti_tabchi_enabled']
    
    # --- مدیریت Toggle های منوی رسانه ---
    elif d.startswith('toggle_media_'):
        media_type = d.split('_')[-1]
        if media_type in settings['media_locks']:
            settings['media_locks'][media_type] = not settings['media_locks'][media_type]
            save_settings(chat_id, settings)
            # فقط ریپلی مارک‌آپ بخش رسانه را آپدیت می‌کنیم
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=get_media_panel_keyboard(settings))
            return bot.answer_callback_query(call.id, "✅ تنظیمات رسانه با موفقیت ذخیره شد.")
        else:
            return bot.answer_callback_query(call.id, "خطا در شناسایی نوع رسانه!")

    # ذخیره و به‌روزرسانی پنل اصلی
    save_settings(chat_id, settings)
    # تغییر نام دکمه‌ها در پنل اصلی، نیاز به ویرایش کل پیام دارد
    bot.edit_message_text("⚙️ **پنل اصلی تنظیمات گروه**", chat_id, msg_id, 
                          reply_markup=get_main_panel_keyboard(settings), parse_mode='Markdown')
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


@bot.message_handler(commands=['ban', 'بن'])
def cmd_ban(message):
    """بن کردن دائمی کاربر با ریپلای (حفاظت از ادمین)"""
    chat_id = message.chat.id
    
    if not is_admin(chat_id, message.from_user.id): return
    
    if not message.reply_to_message: 
        return bot.reply_to(message, "⚠️ **برای استفاده از دستور `/بن`، باید روی پیام کاربر مورد نظر ریپلای کنید.**", parse_mode='Markdown')
        
    target_user = message.reply_to_message.from_user
    
    # حفاظت از ادمین (Admin Protection)
    if is_admin(chat_id, target_user.id):
        return bot.reply_to(message, "❌ **شما نمی‌توانید یک مدیر گروه را بن کنید!**", parse_mode='Markdown')
        
    try:
        # بن کردن دائمی کاربر
        bot.ban_chat_member(chat_id, target_user.id)
        bot.reply_to(message, f"🚫 کاربر **{target_user.first_name}** ({target_user.id}) با موفقیت از گروه **بن (اخراج دائم)** شد.", parse_mode='Markdown')
        delete_msg(chat_id, message.message_id)
    except Exception as e:
         bot.reply_to(message, f"❌ خطا: ربات نتوانست کاربر را بن کند. (ممکن است دسترسی کافی نداشته باشد. {e})")


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