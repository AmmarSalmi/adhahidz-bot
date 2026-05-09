from __future__ import annotations

import logging
from .db import get_user_language, set_user_language

logger = logging.getLogger(__name__)

_TRANSLATIONS = {
    "ar": {
        "Welcome! I couldn't load the Wilaya list yet (API unavailable). Send /change later to try again.": "مرحباً! لم أتمكن من تحميل قائمة الولايات (الخدمة غير متاحة). أرسل /change لاحقاً.",
        "Welcome! Choose your Wilaya to receive quota notifications:": "مرحباً! اختر ولايتك لتلقي إشعارات الحصص:",
        "Wilaya list not available yet. Please try again later.": "قائمة الولايات غير متاحة حالياً. يرجى المحاولة لاحقاً.",
        "Choose your Wilaya:": "اختر ولايتك:",
        "You have been unsubscribed.": "تم إلغاء اشتراكك.",
        "You are not subscribed.": "أنت غير مشترك.",
        "You are not subscribed. Send /start to subscribe.": "أنت غير مشترك. أرسل /start للاشتراك.",
        "⛔ This command is restricted to the administrator to prevent resource waste.": "⛔ هذا الأمر مخصص للمسؤول لمنع إهدار الموارد.",
        "You have no profiles. Use /addprofile first.": "ليس لديك أي ملفات شخصية. استخدم /addprofile أولاً.",
        "Profile not found.": "الملف الشخصي غير موجود.",
        "📱 *Main Menu*\nSelect an option below:": "📱 *القائمة الرئيسية*\nاختر خياراً من الأسفل:",
        "👤 Account": "👤 الحساب",
        "👥 Profiles": "👥 الملفات الشخصية",
        "⚙️ Settings": "⚙️ الإعدادات",
        "ℹ️ Check Status": "ℹ️ التحقق من الحالة",
        "🔄 Change Wilaya": "🔄 تغيير الولاية",
        "⏹️ Stop Notifications": "⏹️ إيقاف الإشعارات",
        "🔙 Back": "🔙 رجوع",
        "📋 List Profiles": "📋 قائمة الملفات",
        "➕ Add Auto-Profile": "➕ إضافة ملف تلقائي",
        "📝 Manual Register": "📝 تسجيل يدوي",
        "✍️ Edit Profile": "✍️ تعديل الملف",
        "🗑️ Delete Profile": "🗑️ حذف الملف",
        "👁️ View Profile": "👁️ عرض الملف",
        "↕️ Reorder Profiles": "↕️ إعادة ترتيب",
        "✅ Verify OTP": "✅ التحقق من OTP",
        "🔍 Check Profile": "🔍 فحص الملف",
        "📡 Fetch Info": "📡 جلب المعلومات",
        "❓ Help": "❓ مساعدة",
        "🌐 Language / اللغة": "🌐 اللغة / Langue",
        "👤 *Account Menu*\nManage your wilaya subscription:": "👤 *قائمة الحساب*\nإدارة اشتراك ولايتك:",
        "👥 *Profiles Menu*\nManage your registration profiles:": "👥 *قائمة الملفات*\nإدارة ملفات التسجيل:",
        "⚙️ *Settings Menu*\nBot settings and info:": "⚙️ *قائمة الإعدادات*\nإعدادات ومعلومات البوت:",
        "Profile creation cancelled.": "تم إلغاء إنشاء الملف.",
        "No profiles found. Use /addprofile to create one.": "لم يتم العثور على ملفات. استخدم /addprofile لإنشاء واحد.",
        "No profiles to view.": "لا توجد ملفات لعرضها.",
        "Select a profile to *view full details*:": "اختر ملفاً لـ *عرض التفاصيل الكاملة*:",
        "❌ Profile not found.": "❌ الملف غير موجود.",
        "No profiles to delete.": "لا توجد ملفات لحذفها.",
        "Select a profile to *delete*:": "اختر ملفاً لـ *حذفه*:",
        "No profiles to edit.": "لا توجد ملفات لتعديلها.",
        "Select a profile to *edit*:": "اختر ملفاً لـ *تعديله*:",
        "❌ Invalid payment method. Try again.": "❌ طريقة الدفع غير صالحة. حاول مرة أخرى.",
        "❌ Invalid status. Try again.": "❌ حالة غير صالحة. حاول مرة أخرى.",
        "Edit cancelled.": "تم إلغاء التعديل.",
        "You need at least 2 profiles to reorder.": "تحتاج إلى ملفين على الأقل لإعادة الترتيب.",
        "❌ Enter profile IDs as numbers separated by spaces.": "❌ أدخل معرفات الملفات كأرقام مفصولة بمسافات.",
        "Select language / اختر اللغة / Choisissez la langue:": "Select language / اختر اللغة / Choisissez la langue:",
        "📖 *Available Commands*\n": "📖 *الأوامر المتاحة*\n",
        "\n/cancel — Cancel an in-progress registration": "\n/cancel — إلغاء التسجيل الحالي",
        "_No wilayas are currently being watched._": "_لا توجد ولايات مراقبة حالياً._",
        "👁 *Watched wilayas:*\n": "👁 *الولايات المراقبة:*\n",
    },
    "fr": {
        "Welcome! I couldn't load the Wilaya list yet (API unavailable). Send /change later to try again.": "Bienvenue ! Je n'ai pas pu charger la liste des Wilayas. Envoyez /change plus tard.",
        "Welcome! Choose your Wilaya to receive quota notifications:": "Bienvenue ! Choisissez votre Wilaya pour recevoir les notifications :",
        "Wilaya list not available yet. Please try again later.": "Liste des Wilayas indisponible. Réessayez plus tard.",
        "Choose your Wilaya:": "Choisissez votre Wilaya :",
        "You have been unsubscribed.": "Vous avez été désabonné.",
        "You are not subscribed.": "Vous n'êtes pas abonné.",
        "You are not subscribed. Send /start to subscribe.": "Vous n'êtes pas abonné. Envoyez /start pour vous abonner.",
        "⛔ This command is restricted to the administrator to prevent resource waste.": "⛔ Cette commande est réservée à l'administrateur.",
        "You have no profiles. Use /addprofile first.": "Vous n'avez aucun profil. Utilisez d'abord /addprofile.",
        "Profile not found.": "Profil introuvable.",
        "📱 *Main Menu*\nSelect an option below:": "📱 *Menu Principal*\nSélectionnez une option :",
        "👤 Account": "👤 Compte",
        "👥 Profiles": "👥 Profils",
        "⚙️ Settings": "⚙️ Paramètres",
        "ℹ️ Check Status": "ℹ️ Vérifier le statut",
        "🔄 Change Wilaya": "🔄 Changer de Wilaya",
        "⏹️ Stop Notifications": "⏹️ Arrêter les notifications",
        "🔙 Back": "🔙 Retour",
        "📋 List Profiles": "📋 Liste des Profils",
        "➕ Add Auto-Profile": "➕ Ajouter Profil Auto",
        "📝 Manual Register": "📝 Inscription Manuelle",
        "✍️ Edit Profile": "✍️ Modifier le Profil",
        "🗑️ Delete Profile": "🗑️ Supprimer le Profil",
        "👁️ View Profile": "👁️ Voir le Profil",
        "↕️ Reorder Profiles": "↕️ Réorganiser",
        "✅ Verify OTP": "✅ Vérifier OTP",
        "🔍 Check Profile": "🔍 Vérifier le Profil",
        "📡 Fetch Info": "📡 Informations",
        "❓ Help": "❓ Aide",
        "🌐 Language / اللغة": "🌐 Langue / Language",
        "👤 *Account Menu*\nManage your wilaya subscription:": "👤 *Menu Compte*\nGérez votre abonnement wilaya :",
        "👥 *Profiles Menu*\nManage your registration profiles:": "👥 *Menu Profils*\nGérez vos profils d'inscription :",
        "⚙️ *Settings Menu*\nBot settings and info:": "⚙️ *Menu Paramètres*\nParamètres et infos du bot :",
        "Profile creation cancelled.": "Création de profil annulée.",
        "No profiles found. Use /addprofile to create one.": "Aucun profil trouvé. Utilisez /addprofile pour en créer un.",
        "No profiles to view.": "Aucun profil à voir.",
        "Select a profile to *view full details*:": "Sélectionnez un profil pour *voir les détails* :",
        "❌ Profile not found.": "❌ Profil introuvable.",
        "No profiles to delete.": "Aucun profil à supprimer.",
        "Select a profile to *delete*:": "Sélectionnez un profil à *supprimer* :",
        "No profiles to edit.": "Aucun profil à modifier.",
        "Select a profile to *edit*:": "Sélectionnez un profil à *modifier* :",
        "❌ Invalid payment method. Try again.": "❌ Mode de paiement invalide. Réessayez.",
        "❌ Invalid status. Try again.": "❌ Statut invalide. Réessayez.",
        "Edit cancelled.": "Modification annulée.",
        "You need at least 2 profiles to reorder.": "Vous avez besoin d'au moins 2 profils pour réorganiser.",
        "❌ Enter profile IDs as numbers separated by spaces.": "❌ Entrez les ID des profils sous forme de nombres séparés par des espaces.",
        "Select language / اختر اللغة / Choisissez la langue:": "Select language / اختر اللغة / Choisissez la langue:",
        "📖 *Available Commands*\n": "📖 *Commandes Disponibles*\n",
        "\n/cancel — Cancel an in-progress registration": "\n/cancel — Annuler une inscription en cours",
        "_No wilayas are currently being watched._": "_Aucune wilaya n'est actuellement surveillée._",
        "👁 *Watched wilayas:*\n": "👁 *Wilayas surveillées :*\n",
    }
}

async def get_lang(context, user_id: int) -> str:
    if "lang" in context.user_data:
        return context.user_data["lang"]
    db_path = context.application.bot_data["db_path"]
    lang = await get_user_language(db_path, user_id)
    context.user_data["lang"] = lang
    return lang

def t(lang: str, text: str) -> str:
    """Translate a string to the target language."""
    if lang == "en":
        return text
    return _TRANSLATIONS.get(lang, {}).get(text, text)
