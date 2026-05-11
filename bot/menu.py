from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, KeyboardButton, constants
from telegram.ext import ContextTypes

from .handlers import status, change, stop, fetchinfo, help_command, checkprofile
from .profile_handlers import list_profiles, deleteprofile, viewprofile, editprofile
from .i18n import t, get_lang
from .notifier import safe_query_answer

def get_main_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "👤 Account"), callback_data="menu:nav:account")],
        [InlineKeyboardButton(t(lang, "👥 Profiles"), callback_data="menu:nav:profiles")],
        [InlineKeyboardButton(t(lang, "⚙️ Settings"), callback_data="menu:nav:settings")],
        [InlineKeyboardButton(t(lang, "🌐 Language / اللغة"), callback_data="menu:nav:language")],
    ])

def get_reply_main_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton(t(lang, "👤 Account")), KeyboardButton(t(lang, "👥 Profiles"))],
        [KeyboardButton(t(lang, "⚙️ Settings")), KeyboardButton(t(lang, "🌐 Language / اللغة"))]
    ], resize_keyboard=True)

def get_account_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "ℹ️ Check Status"), callback_data="menu:cmd:status")],
        [InlineKeyboardButton(t(lang, "🔄 Change Wilaya"), callback_data="menu:cmd:change")],
        [InlineKeyboardButton(t(lang, "⏹️ Stop Notifications"), callback_data="menu:cmd:stop")],
        [InlineKeyboardButton(t(lang, "🔙 Back"), callback_data="menu:nav:main")],
    ])

def get_profiles_menu_keyboard(lang: str, is_admin_user: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(t(lang, "📋 List Profiles"), callback_data="menu:cmd:profiles")],
        [InlineKeyboardButton(t(lang, "➕ Add Auto-Profile"), callback_data="menu:cmd:addprofile"),
         InlineKeyboardButton(t(lang, "📝 Manual Register"), callback_data="menu:cmd:register")],
        [InlineKeyboardButton(t(lang, "✍️ Edit Profile"), callback_data="menu:cmd:editprofile"),
         InlineKeyboardButton(t(lang, "🗑️ Delete Profile"), callback_data="menu:cmd:deleteprofile")],
        [InlineKeyboardButton(t(lang, "👁️ View Profile"), callback_data="menu:cmd:viewprofile"),
         InlineKeyboardButton(t(lang, "↕️ Reorder Profiles"), callback_data="menu:cmd:reorder")],
    ]
    
    verify_row = [InlineKeyboardButton(t(lang, "✅ Verify OTP"), callback_data="menu:cmd:verifyotp")]
    if is_admin_user:
        verify_row.append(InlineKeyboardButton(t(lang, "🔍 Check Profile"), callback_data="menu:cmd:checkprofile"))
    
    buttons.append(verify_row)
    buttons.append([InlineKeyboardButton(t(lang, "🔙 Back"), callback_data="menu:nav:main")])
    return InlineKeyboardMarkup(buttons)

def get_settings_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "📡 Fetch Info"), callback_data="menu:cmd:fetchinfo")],
        [InlineKeyboardButton(t(lang, "❓ Help"), callback_data="menu:cmd:help")],
        [InlineKeyboardButton(t(lang, "🔙 Back"), callback_data="menu:nav:main")],
    ])

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    lang = await get_lang(context, update.effective_user.id)
    text = t(lang, "📱 *Main Menu*\nSelect an option below:")
    keyboard = get_reply_main_menu_keyboard(lang)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await get_lang(context, update.effective_user.id)
    text = t(lang, "📱 *Main Menu*\nSelect an option below:")
    keyboard = get_main_menu_keyboard(lang)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def on_menu_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await safe_query_answer(query):
        return
    data = query.data
    lang = await get_lang(context, update.effective_user.id)
    
    if data == "menu:nav:main":
        await show_menu(update, context)
    elif data == "menu:nav:account":
        await query.edit_message_text(t(lang, "👤 *Account Menu*\nManage your wilaya subscription:"), reply_markup=get_account_menu_keyboard(lang), parse_mode="Markdown")
    elif data == "menu:nav:profiles":
        from .admin import is_admin
        await query.edit_message_text(t(lang, "👥 *Profiles Menu*\nManage your registration profiles:"), reply_markup=get_profiles_menu_keyboard(lang, is_admin(update)), parse_mode="Markdown")
    elif data == "menu:nav:settings":
        await query.edit_message_text(t(lang, "⚙️ *Settings Menu*\nBot settings and info:"), reply_markup=get_settings_menu_keyboard(lang), parse_mode="Markdown")
    elif data == "menu:nav:language":
        from .i18n import _TRANSLATIONS
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("العربية 🇩🇿", callback_data="menu:cmd:lang:ar")],
            [InlineKeyboardButton("Français 🇫🇷", callback_data="menu:cmd:lang:fr")],
            [InlineKeyboardButton("English 🇬🇧", callback_data="menu:cmd:lang:en")],
            [InlineKeyboardButton(t(lang, "🔙 Back"), callback_data="menu:nav:main")]
        ])
        await query.edit_message_text(t(lang, "Select language / اختر اللغة / Choisissez la langue:"), reply_markup=kb)

async def on_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    # Acknowledge the button press
    if not await safe_query_answer(query):
        return
    data = query.data
    
    cmd = data.split(":", 2)[2]
    
    # We map directly to the existing simple handlers.
    # The ConversationHandlers (addprofile, register, reorder, verifyotp, editprofile? wait, editprofile is a simple handler that sends inline kb!)
    if cmd == "status":
        await status(update, context)
    elif cmd == "change":
        await change(update, context)
    elif cmd == "stop":
        await stop(update, context)
    elif cmd == "profiles":
        await list_profiles(update, context)
    elif cmd == "deleteprofile":
        await deleteprofile(update, context)
    elif cmd == "viewprofile":
        await viewprofile(update, context)
    elif cmd == "checkprofile":
        await checkprofile(update, context)
    elif cmd == "fetchinfo":
        await fetchinfo(update, context)
    elif cmd == "help":
        await help_command(update, context)
    elif cmd == "editprofile":
        await editprofile(update, context)
    elif cmd.startswith("lang:"):
        new_lang = cmd.split(":")[1]
        db_path = context.application.bot_data["db_path"]
        from .db import set_user_language
        await set_user_language(db_path, update.effective_user.id, new_lang)
        context.user_data["lang"] = new_lang
        # send the new reply keyboard
        await update.effective_message.reply_text(t(new_lang, "📱 *Main Menu*"), reply_markup=get_reply_main_menu_keyboard(new_lang), parse_mode="Markdown")
        await show_menu(update, context)
    # The ConversationHandlers are caught by their respective CallbackQueryHandler entry_points

async def handle_reply_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if not text:
        return
    lang = await get_lang(context, update.effective_user.id)
    
    if text == t(lang, "👤 Account"):
        await update.message.reply_text(t(lang, "👤 *Account Menu*\nManage your wilaya subscription:"), reply_markup=get_account_menu_keyboard(lang), parse_mode="Markdown")
    elif text == t(lang, "👥 Profiles"):
        from .admin import is_admin
        await update.message.reply_text(t(lang, "👥 *Profiles Menu*\nManage your registration profiles:"), reply_markup=get_profiles_menu_keyboard(lang, is_admin(update)), parse_mode="Markdown")
    elif text == t(lang, "⚙️ Settings"):
        await update.message.reply_text(t(lang, "⚙️ *Settings Menu*\nBot settings and info:"), reply_markup=get_settings_menu_keyboard(lang), parse_mode="Markdown")
    elif text == t(lang, "🌐 Language / اللغة"):
        from .i18n import _TRANSLATIONS
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("العربية 🇩🇿", callback_data="menu:cmd:lang:ar")],
            [InlineKeyboardButton("Français 🇫🇷", callback_data="menu:cmd:lang:fr")],
            [InlineKeyboardButton("English 🇬🇧", callback_data="menu:cmd:lang:en")],
        ])
        await update.message.reply_text(t(lang, "Select language / اختر اللغة / Choisissez la langue:"), reply_markup=kb)
