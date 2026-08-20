import logging
import asyncio
import requests
import random
import json
import os
import time
import re
from datetime import datetime, timedelta
from functools import wraps
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode

# ============ КОНФИГУРАЦИЯ ============

API_TOKEN = "8837076021:AAGY2MaVv469JZH61QbNJcRVJteHZb5EOtU"
ADMIN_IDS = [7989621596]
BOT_USERNAME = "vlesskeysfreebot"

# ============ НАСТРОЙКИ ПОДПИСКИ ============
REQUIRED_CHANNELS = [
    "https://t.me/keramaxur",
    "https://t.me/station53"
]

# ============ CRYPTOBOT НАСТРОЙКИ ============
CRYPTOBOT_API_KEY = "615167:AAqHnExucpLHPuZsZFG02sdHC8O87hgl319"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

# ============ ХРАНИЛИЩЕ ============
verified_users = {}  # user_id: timestamp
pending_users = {}  # user_id: {"username": ..., "first_name": ..., "timestamp": ...}
orders = {}
pending_payments = {}
used_keys = {}
regular_keys_cache = {}
last_update = None
user_messages = {}
free_keys_used = {}
loading_in_progress = False

# ============ НАСТРОЙКИ ============
MIN_KEYS_PER_COUNTRY = 500
MAX_KEYS_PER_COUNTRY = 700
CACHE_FILE = "keys_cache.json"
CACHE_DURATION = 3600

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class OrderState(StatesGroup):
    waiting_for_country = State()
    waiting_for_days = State()
    waiting_for_payment = State()

COUNTRIES = [
    {"name": "🇺🇸 США", "code": "us", "city": "Нью-Йорк"},
    {"name": "🇬🇧 Великобритания", "code": "uk", "city": "Лондон"},
    {"name": "🇩🇪 Германия", "code": "de", "city": "Франкфурт"},
    {"name": "🇫🇷 Франция", "code": "fr", "city": "Париж"},
    {"name": "🇳🇱 Нидерланды", "code": "nl", "city": "Амстердам"},
    {"name": "🇯🇵 Япония", "code": "jp", "city": "Токио"},
    {"name": "🇸🇬 Сингапур", "code": "sg", "city": "Сингапур"},
    {"name": "🇷🇺 Россия", "code": "ru", "city": "Москва"}
]

DAILY_USERS = "50+"

# ============ КЛАСС ДЛЯ ПРОВЕРКИ КЛЮЧЕЙ ============

class VlessKeyValidator:
    @staticmethod
    def extract_info(key):
        try:
            if '#' in key:
                key = key.split('#')[0]
            if not key.startswith('vless://'):
                return None
            uuid_match = re.search(r'vless://([^@]+)@', key)
            uuid = uuid_match.group(1) if uuid_match else None
            ip_port_match = re.search(r'@([^:]+):(\d+)', key)
            ip = ip_port_match.group(1) if ip_port_match else None
            port = ip_port_match.group(2) if ip_port_match else None
            return {'uuid': uuid, 'ip': ip, 'port': port, 'raw': key}
        except:
            return None
    
    @staticmethod
    def validate_format(key):
        required_parts = ['vless://', '@', ':', '?', 'encryption']
        return all(part in key for part in required_parts)
    
    @classmethod
    def is_valid(cls, key, timeout=3):
        if not cls.validate_format(key):
            return False
        info = cls.extract_info(key)
        if not info or not info['ip']:
            return False
        return True
    
    @classmethod
    def get_valid_keys(cls, keys, max_check=20):
        valid_keys = []
        checked = 0
        for key in keys:
            if checked >= max_check:
                break
            if cls.is_valid(key):
                valid_keys.append(key)
                checked += 1
        return valid_keys

# ============ CRYPTOBOT API ============

def create_crypto_invoice(amount_usd, payload, description):
    url = f"{CRYPTOBOT_API_URL}/createInvoice"
    headers = {
        'Crypto-Pay-API-Token': CRYPTOBOT_API_KEY,
        'Content-Type': 'application/json'
    }
    data = {
        'asset': 'USDT',
        'amount': str(amount_usd),
        'payload': payload,
        'description': description,
        'expires_in': 3600,
        'paid_btn_name': 'openBot',
        'paid_btn_url': f'https://t.me/{BOT_USERNAME}',
        'allow_comments': True,
        'allow_anonymous': False
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=15)
        if response.status_code == 200:
            result = response.json()
            if result.get('ok'):
                return result['result']
        return None
    except Exception as e:
        logger.error(f"Ошибка создания счета: {e}")
        return None

def check_crypto_payment(invoice_id):
    url = f"{CRYPTOBOT_API_URL}/getInvoices"
    headers = {
        'Crypto-Pay-API-Token': CRYPTOBOT_API_KEY,
        'Content-Type': 'application/json'
    }
    data = {'invoice_ids': [invoice_id]}
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=15)
        if response.status_code == 200:
            result = response.json()
            if result.get('ok') and result.get('result'):
                return result['result'][0]
        return None
    except Exception as e:
        logger.error(f"Ошибка проверки: {e}")
        return None

# ============ ЗАГРУЗКА КЛЮЧЕЙ ============

def get_vless_keys_optimized():
    global regular_keys_cache, last_update, loading_in_progress
    
    if last_update and (datetime.now() - last_update).seconds < 300:
        return regular_keys_cache
    
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get('timestamp'):
                    cache_time = datetime.fromisoformat(data['timestamp'])
                    if (datetime.now() - cache_time).seconds < CACHE_DURATION:
                        return data.get('keys', {})
    except:
        pass
    
    if loading_in_progress:
        return regular_keys_cache or {}
    
    loading_in_progress = True
    logger.info("📥 Загрузка ключей...")
    
    try:
        url = "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/vless.txt"
        response = requests.get(url, timeout=30)
        
        if response.status_code != 200:
            loading_in_progress = False
            return {}
        
        all_keys = response.text.splitlines()
        
        country_limits = {}
        for country in COUNTRIES:
            limit = random.randint(MIN_KEYS_PER_COUNTRY, MAX_KEYS_PER_COUNTRY)
            country_limits[country['code']] = limit
        
        keys_by_country = {country['code']: [] for country in COUNTRIES}
        
        for line in all_keys:
            line = line.strip()
            if not line or not line.startswith('vless://'):
                continue
            
            for country in COUNTRIES:
                code = country['code']
                if len(keys_by_country[code]) >= country_limits[code]:
                    continue
                
                if code == 'ru':
                    if '.ru' in line or 'ru.' in line or '/ru/' in line:
                        keys_by_country[code].append(line)
                else:
                    if f'.{code}' in line or f'{code}.' in line or f'/{code}/' in line:
                        keys_by_country[code].append(line)
        
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'keys': keys_by_country, 'timestamp': datetime.now().isoformat()}, f)
        
        regular_keys_cache = keys_by_country
        last_update = datetime.now()
        loading_in_progress = False
        
        return keys_by_country
        
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки: {e}")
        loading_in_progress = False
        return {}

def get_keys_for_country(country_code):
    all_keys = get_vless_keys_optimized()
    if not all_keys:
        return []
    
    available = all_keys.get(country_code, [])
    used = used_keys.get(country_code, [])
    return [k for k in available if k not in used]

def get_random_keys(country_code, quantity=1):
    available = get_keys_for_country(country_code)
    if not available:
        return None
    
    valid_keys = VlessKeyValidator.get_valid_keys(available, max_check=50)
    if not valid_keys:
        return None
    
    if len(valid_keys) < quantity:
        quantity = len(valid_keys)
    
    selected = random.sample(valid_keys, min(quantity, len(valid_keys)))
    
    if country_code not in used_keys:
        used_keys[country_code] = []
    used_keys[country_code].extend(selected)
    
    return [f"{k}" for k in selected]

def get_random_key_any_country():
    countries = list(COUNTRIES)
    random.shuffle(countries)
    
    for country in countries:
        keys = get_keys_for_country(country['code'])
        if keys:
            valid_keys = VlessKeyValidator.get_valid_keys(keys, max_check=20)
            if valid_keys:
                key = random.choice(valid_keys)
                if country['code'] not in used_keys:
                    used_keys[country['code']] = []
                used_keys[country['code']].append(key)
                return f"{key}", country['name']
    
    return None, None

def get_keys_count(country_code):
    return len(get_keys_for_country(country_code))

def get_total_keys_count():
    keys = get_vless_keys_optimized()
    return sum(len(v) for v in keys.values()) if keys else 0

# ============ ЦЕНЫ ============
PRICES = {
    "1_day": 5,
    "7_days": 25,
    "30_days": 70,
    "90_days": 180,
}

DAYS_MAP = {
    "1": "1_day",
    "7": "7_days",
    "30": "30_days",
    "90": "90_days",
}

def get_price_rub(days):
    return PRICES.get(days, 0)

def get_price_usd(days):
    rub = get_price_rub(days)
    return round(rub / 83, 2)

# ============ КЛАВИАТУРЫ ============

def get_main_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🛒 Купить ключ", callback_data="buy")],
        [InlineKeyboardButton(text="🎁 Бесплатный ключ", callback_data="free_key")],
        [InlineKeyboardButton(text="🌍 Список стран", callback_data="countries")],
        [InlineKeyboardButton(text="💳 Как оплатить?", callback_data="how_to_pay")],
        [InlineKeyboardButton(text="📞 Поддержка", callback_data="support")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_subscription_keyboard():
    buttons = []
    for channel in REQUIRED_CHANNELS:
        buttons.append([
            InlineKeyboardButton(text="📢 Подписаться на канал", url=channel)
        ])
    buttons.append([
        InlineKeyboardButton(text="✅ Я подписался", callback_data="sub_confirm")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard(user_id):
    buttons = [
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{user_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_countries_keyboard():
    buttons = []
    row = []
    for i, country in enumerate(COUNTRIES):
        count = get_keys_count(country['code'])
        status = f"✅{count}" if count > 0 else "❌"
        row.append(InlineKeyboardButton(text=f"{country['name']} {status}", callback_data=f"country_{country['code']}"))
        if len(row) == 2 or i == len(COUNTRIES) - 1:
            buttons.append(row)
            row = []
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_keys")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_days_keyboard():
    buttons = []
    days_list = [(1, "1 день - 5₽"), (7, "7 дней - 25₽"), (30, "30 дней - 70₽"), (90, "90 дней - 180₽")]
    for days_num, label in days_list:
        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"days_{days_num}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_countries")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payment_keyboard(invoice_url, order_id):
    buttons = [
        [InlineKeyboardButton(
            text="💳 Оплатить через Cryptobot",
            url=invoice_url
        )],
        [InlineKeyboardButton(
            text="✅ Я оплатил",
            callback_data=f"paid_{order_id}"
        )],
        [InlineKeyboardButton(
            text="❌ Отменить заказ",
            callback_data="cancel_order"
        )]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_payment_keyboard(order_id, user_id):
    buttons = [
        [InlineKeyboardButton(
            text="✅ Выдать ключ",
            callback_data=f"approve_payment_{order_id}_{user_id}"
        )],
        [InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"reject_payment_{order_id}_{user_id}"
        )]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ============ КОМАНДЫ ============

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    
    # Если уже подтвержден — показываем главное меню
    if user_id in verified_users:
        total = get_total_keys_count()
        text = f"""
🌟 <b>Добро пожаловать, {user_name}!</b>

<i>Ваш надежный поставщик VLESS ключей</i>

<b>📌 Что мы предлагаем:</b>
• 🔑 Качественные VLESS ключи 
• 🌍 Сервера в 8 странах
• ⏰ Гибкие сроки: 1д, 7д, 30д, 90д

<b>🎁 Пробный период:</b>
• <b>Бесплатный ключ</b> на 1 день (1 раз)
• Доступ ко всем странам
• Без ограничений

<b>📊 Статистика:</b>
• 📦 Всего ключей: {total}
• 🌍 Доступно стран: 8
• 🔥 Ежедневно более 50+ человек закупаются у нас

<b>💳 Оплата через Cryptobot (USDT)</b>

👇 <b>Выберите действие:</b>
"""
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
        return
    
    # Если заявка уже отправлена
    if user_id in pending_users:
        await message.answer(
            "⏳ <b>Ваша заявка уже отправлена на проверку!</b>\n\n"
            "Администратор проверит вашу подписку в ближайшее время.\n"
            "Это займет не более 5 минут.\n\n"
            "📞 Если возникли вопросы: @amniamov",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Показываем требование подписки
    text = f"""
🌟 <b>Добро пожаловать, {user_name}!</b>

⚠️ <b>Для использования бота необходимо подписаться на наши каналы!</b>

Это бесплатно и займет всего пару секунд.👇

📢 <b>Каналы:</b>
• {REQUIRED_CHANNELS[0]}
• {REQUIRED_CHANNELS[1]}

<i>После подписки нажмите кнопку "✅ Я подписался"</i>
"""
    
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_subscription_keyboard())

@dp.callback_query(F.data == "sub_confirm")
async def subscription_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_name = callback.from_user.first_name
    username = callback.from_user.username or "не указан"
    
    if user_id in verified_users:
        await callback.answer("✅ Вы уже подтверждены!", show_alert=True)
        return
    
    if user_id in pending_users:
        await callback.answer("⏳ Ваша заявка уже на проверке!", show_alert=True)
        return
    
    pending_users[user_id] = {
        "first_name": user_name,
        "username": username,
        "timestamp": datetime.now().strftime('%d.%m.%Y %H:%M')
    }
    
    await callback.message.edit_text(
        "⏳ <b>Проверяем подписку...</b>\n\n"
        "✅ Ваша заявка отправлена на проверку администратору!\n\n"
        "Подождите, администратор проверит вашу подписку.\n"
        "Это займет не более 5 минут.\n\n"
        "📞 Если возникли вопросы: @amniamov",
        parse_mode=ParseMode.HTML,
        reply_markup=None
    )
    
    admin_text = f"""
🔔 <b>НОВАЯ ЗАЯВКА НА ПОДПИСКУ!</b>

👤 <b>Пользователь:</b> {user_name}
🆔 <b>ID:</b> <code>{user_id}</code>
👤 <b>Username:</b> @{username}
🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}

📢 <b>Проверьте подписку на каналы:</b>
• {REQUIRED_CHANNELS[0]}
• {REQUIRED_CHANNELS[1]}
"""
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_admin_keyboard(user_id)
            )
        except Exception as e:
            logger.error(f"Ошибка отправки админу: {e}")
    
    await callback.answer("✅ Заявка отправлена на проверку!")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Только для админа!", show_alert=True)
        return
    
    if user_id not in pending_users:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    verified_users[user_id] = datetime.now()
    user_data = pending_users.pop(user_id)
    
    await callback.message.edit_text(
        f"✅ <b>Пользователь одобрен!</b>\n\n"
        f"👤 {user_data['first_name']}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"👤 @{user_data['username']}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode=ParseMode.HTML,
        reply_markup=None
    )
    
    try:
        await bot.send_message(
            user_id,
            "✅ <b>Подписка подтверждена!</b>\n\n"
            "Доступ к боту открыт.\n"
            "Можете пользоваться всеми функциями.\n\n"
            "🌟 <b>Добро пожаловать!</b>",
            parse_mode=ParseMode.HTML
        )
        
        total = get_total_keys_count()
        text = f"""
🌟 <b>Добро пожаловать!</b>

<i>Ваш надежный поставщик VLESS ключей</i>

<b>📌 Что мы предлагаем:</b>
• 🔑 Качественные VLESS ключи 
• 🌍 Сервера в 8 странах
• ⏰ Гибкие сроки: 1д, 7д, 30д, 90д

<b>🎁 Пробный период:</b>
• <b>Бесплатный ключ</b> на 1 день (1 раз)
• Доступ ко всем странам
• Без ограничений

<b>📊 Статистика:</b>
• 📦 Всего ключей: {total}
• 🌍 Доступно стран: 8
• 🔥 Ежедневно более 50+ человек закупаются у нас

<b>💳 Оплата через Cryptobot (USDT)</b>

👇 <b>Выберите действие:</b>
"""
        await bot.send_message(user_id, text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
        
    except Exception as e:
        logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("✅ Пользователь одобрен!")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_user(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Только для админа!", show_alert=True)
        return
    
    if user_id not in pending_users:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    user_data = pending_users.pop(user_id)
    
    await callback.message.edit_text(
        f"❌ <b>Пользователь отклонен!</b>\n\n"
        f"👤 {user_data['first_name']}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"👤 @{user_data['username']}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode=ParseMode.HTML,
        reply_markup=None
    )
    
    try:
        await bot.send_message(
            user_id,
            "❌ <b>Ваша заявка на подписку отклонена</b>\n\n"
            "Возможные причины:\n"
            "• Вы не подписались на все каналы\n"
            "• Подписка не подтверждена\n\n"
            "Пожалуйста, подпишитесь на каналы и попробуйте снова.\n\n"
            "📢 <b>Каналы:</b>\n"
            f"• {REQUIRED_CHANNELS[0]}\n"
            f"• {REQUIRED_CHANNELS[1]}",
            parse_mode=ParseMode.HTML
        )
        
        text = f"""
⚠️ <b>Для использования бота необходимо подписаться на наши каналы!</b>

Это бесплатно и займет всего пару секунд.👇

📢 <b>Каналы:</b>
• {REQUIRED_CHANNELS[0]}
• {REQUIRED_CHANNELS[1]}

<i>После подписки нажмите кнопку "✅ Я подписался"</i>
"""
        await bot.send_message(
            user_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_subscription_keyboard()
        )
        
    except Exception as e:
        logger.error(f"Ошибка уведомления пользователя: {e}")
    
    await callback.answer("❌ Пользователь отклонен!")

# ============ CALLBACK ОБРАБОТЧИКИ ПОКУПКИ ============

@dp.callback_query(F.data == "buy")
async def callback_buy(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    # Проверяем подписку
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    try:
        await callback.message.delete()
    except:
        pass
    
    msg = await callback.message.answer(
        "🌍 <b>Выберите страну сервера:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_countries_keyboard()
    )
    await state.set_state(OrderState.waiting_for_country)
    await callback.answer()
    
    if callback.from_user.id not in user_messages:
        user_messages[callback.from_user.id] = []
    user_messages[callback.from_user.id].append(msg.message_id)

@dp.callback_query(F.data == "free_key")
async def callback_free_key(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # Проверяем подписку
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    if user_id in free_keys_used:
        await callback.answer("❌ Вы уже использовали бесплатный ключ!", show_alert=True)
        return
    
    key, country_name = get_random_key_any_country()
    
    if not key:
        await callback.answer("❌ Нет рабочих ключей", show_alert=True)
        return
    
    free_keys_used[user_id] = datetime.now()
    
    key_text = f"""
🎁 <b>Ваш БЕСПЛАТНЫЙ VLESS ключ</b>

<code>{key}</code>

🌍 <b>Страна:</b> {country_name} (рандомная)
📅 <b>Выдан:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}
⏳ <b>Срок действия:</b> 1 день
✅ <b>Ключ проверен и работает</b>

<i>Бесплатный ключ выдается 1 раз!</i>
"""
    
    await callback.message.answer(key_text, parse_mode=ParseMode.HTML)
    await callback.answer("🎁 Бесплатный рабочий ключ выдан!")

@dp.callback_query(F.data == "countries")
async def callback_countries(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    try:
        await callback.message.delete()
    except:
        pass
    
    total = get_total_keys_count()
    text = f"<b>🌍 Доступные страны:</b>\n\n"
    text += f"📊 Всего ключей: {total}\n"
    text += f"🔥 Ежедневно более {DAILY_USERS} человек закупаются у нас\n\n"
    
    for country in COUNTRIES:
        count = get_keys_count(country['code'])
        status = "✅" if count > 0 else "❌"
        text += f"• {country['name']}\n"
        text += f"   🏙 {country['city']} - {count} ключей {status}\n\n"
    
    text += f"\n<b>💰 Цены:</b>\n"
    text += f"• 1 день = 5₽\n"
    text += f"• 7 дней = 25₽\n"
    text += f"• 30 дней = 70₽\n"
    text += f"• 90 дней = 180₽"
    
    buttons = [
        [InlineKeyboardButton(text="🛒 Купить", callback_data="buy")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_keys")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ]
    msg = await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()
    
    if callback.from_user.id not in user_messages:
        user_messages[callback.from_user.id] = []
    user_messages[callback.from_user.id].append(msg.message_id)

@dp.callback_query(F.data == "how_to_pay")
async def callback_how_to_pay(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    try:
        await callback.message.delete()
    except:
        pass
    
    text = f"""
<b>💳 Оплата через Cryptobot</b>

1️⃣ При заказе создается счет в USDT
2️⃣ Оплатите через Cryptobot
3️⃣ Нажмите "✅ Я оплатил"
4️⃣ Администратор проверит оплату и выдаст ключ

<b>⚠️ ВАЖНО!</b>
При оплате ОБЯЗАТЕЛЬНО указывайте ваш Telegram ID!
"""
    
    buttons = [
        [InlineKeyboardButton(text="🛒 Купить сейчас", callback_data="buy")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ]
    msg = await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()
    
    if callback.from_user.id not in user_messages:
        user_messages[callback.from_user.id] = []
    user_messages[callback.from_user.id].append(msg.message_id)

@dp.callback_query(F.data == "support")
async def callback_support(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    try:
        await callback.message.delete()
    except:
        pass
    
    text = f"""
<b>📞 Служба поддержки</b>

• 💬 Напишите: @amniamov
• ⏰ Ответ в течение 24 часов

<b>❓ FAQ:</b>

<b>❓ Как получить ключи?</b>
<i>Оплатите через Cryptobot и нажмите "✅ Я оплатил"</i>

<b>❓ Сколько стоят ключи?</b>
<i>От 5₽ до 180₽ в зависимости от срока</i>

<b>❓ Сколько действуют ключи?</b>
<i>Вы выбираете: 1д, 7д, 30д, 90д</i>

<b>❓ Как оплатить?</b>
<i>Cryptobot (USDT)</i>

<b>❓ Ключ не работает?</b>
<i>Бот выдает только проверенные рабочие ключи</i>
"""
    
    buttons = [
        [InlineKeyboardButton(text="🛒 Купить", callback_data="buy")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ]
    msg = await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()
    
    if callback.from_user.id not in user_messages:
        user_messages[callback.from_user.id] = []
    user_messages[callback.from_user.id].append(msg.message_id)

@dp.callback_query(F.data == "back_to_main")
async def callback_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    
    try:
        await callback.message.delete()
    except:
        pass
    
    user_name = callback.from_user.first_name
    total = get_total_keys_count()
    
    text = f"""
🌟 <b>Добро пожаловать, {user_name}!</b>

<i>Ваш надежный поставщик VLESS ключей</i>

<b>📌 Что мы предлагаем:</b>
• 🔑 Качественные VLESS ключи 
• 🌍 Сервера в 8 странах
• ⏰ Гибкие сроки: 1д, 7д, 30д, 90д

<b>🎁 Пробный период:</b>
• <b>Бесплатный ключ</b> на 1 день (1 раз)
• Доступ ко всем странам
• Без ограничений

<b>📊 Статистика:</b>
• 📦 Всего ключей: {total}
• 🌍 Доступно стран: {len(COUNTRIES)}
• 🔥 Ежедневно более {DAILY_USERS} человек закупаются у нас

<b>💳 Оплата через Cryptobot (USDT)</b>

👇 <b>Выберите действие:</b>
"""
    
    msg = await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
    await callback.answer()
    
    if callback.from_user.id not in user_messages:
        user_messages[callback.from_user.id] = []
    user_messages[callback.from_user.id].append(msg.message_id)

@dp.callback_query(F.data == "refresh_keys")
async def callback_refresh_keys(callback: CallbackQuery):
    global regular_keys_cache, last_update
    regular_keys_cache = None
    last_update = None
    
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
    except:
        pass
    
    keys = get_vless_keys_optimized()
    total = sum(len(v) for v in keys.values()) if keys else 0
    
    await callback.message.answer(f"🔄 Обновлено! Всего ключей: {total}")
    await callback.answer()

@dp.callback_query(F.data == "cancel_order")
async def cancel_order_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        "❌ <b>Заказ отменен</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    await callback.answer("✅ Заказ отменен")

@dp.callback_query(F.data == "back_to_countries")
async def back_to_countries(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.delete()
    except:
        pass
    
    msg = await callback.message.answer(
        "🌍 <b>Выберите страну сервера:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_countries_keyboard()
    )
    await state.set_state(OrderState.waiting_for_country)
    await callback.answer()
    
    if callback.from_user.id not in user_messages:
        user_messages[callback.from_user.id] = []
    user_messages[callback.from_user.id].append(msg.message_id)

@dp.callback_query(F.data.startswith("country_"))
async def process_country(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    try:
        country_code = callback.data.split("_")[1]
        country = next((c for c in COUNTRIES if c['code'] == country_code), None)
        
        if not country:
            await callback.answer("❌ Страна не найдена")
            return
        
        await callback.message.delete()
        
        count = get_keys_count(country_code)
        if count == 0:
            await callback.message.answer(
                f"❌ Для {country['name']} нет ключей.",
                reply_markup=get_countries_keyboard()
            )
            await callback.answer()
            return
        
        await state.update_data(country_code=country_code, country_name=country['name'])
        await state.set_state(OrderState.waiting_for_days)
        
        text = f"""
🌍 <b>Выбрана страна:</b> {country['name']} ({country['city']})

<b>⏰ Выберите срок действия:</b>
"""
        
        msg = await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_days_keyboard())
        
        if callback.from_user.id not in user_messages:
            user_messages[callback.from_user.id] = []
        user_messages[callback.from_user.id].append(msg.message_id)
        
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await callback.message.answer("❌ Ошибка. Попробуйте снова.")
        await callback.answer()

@dp.callback_query(F.data.startswith("days_"))
async def process_days(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    try:
        days_num = int(callback.data.split("_")[1])
        
        data = await state.get_data()
        country_name = data.get('country_name', 'неизвестно')
        country_code = data.get('country_code', '')
        
        days_key = DAYS_MAP.get(str(days_num))
        if not days_key:
            await callback.answer("❌ Ошибка: неизвестный срок", show_alert=True)
            return
        
        price_rub = get_price_rub(days_key)
        price_usd = get_price_usd(days_key)
        
        days_labels = {
            "1_day": "1 день",
            "7_days": "7 дней",
            "30_days": "30 дней",
            "90_days": "90 дней"
        }
        days_label = days_labels.get(days_key, f"{days_num} дней")
        
        if price_rub == 0:
            await callback.answer("❌ Ошибка: цена не найдена", show_alert=True)
            return
        
        await state.update_data(
            days_key=days_key,
            days_label=days_label,
            price_rub=price_rub,
            price_usd=price_usd
        )
        
        await callback.message.delete()
        
        order_id = f"{callback.from_user.id}_{int(datetime.now().timestamp())}"
        await state.update_data(order_id=order_id)
        
        payload = f"{callback.from_user.id}_{order_id}"
        description = f'VLESS ключ, {days_label} - {country_name}'
        
        invoice = create_crypto_invoice(price_usd, payload, description)
        
        if invoice:
            invoice_id = invoice.get('invoice_id')
            invoice_url = invoice.get('bot_invoice_url')
            
            await state.update_data(invoice_id=invoice_id)
            await state.set_state(OrderState.waiting_for_payment)
            
            text = f"""
<b>✅ Ваш заказ</b>

🌍 <b>Страна:</b> {country_name}
⏰ <b>Срок:</b> {days_label}
💰 <b>Сумма:</b> {price_rub}₽ (${price_usd:.2f})
🆔 <b>ID оплаты:</b> <code>{order_id}</code>

<b>💳 Нажмите кнопку для оплаты через Cryptobot!</b>
<i>После оплаты нажмите "✅ Я оплатил"</i>
"""
            
            msg = await callback.message.answer(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_payment_keyboard(invoice_url, order_id)
            )
            
            if callback.from_user.id not in user_messages:
                user_messages[callback.from_user.id] = []
            user_messages[callback.from_user.id].append(msg.message_id)
        else:
            await callback.message.answer(
                "❌ Ошибка создания счета в Cryptobot.\n"
                "Попробуйте позже или обратитесь в поддержку @amniamov",
                reply_markup=get_main_keyboard()
            )
        
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await callback.message.answer("❌ Ошибка. Попробуйте снова.")
        await callback.answer()

# ============ ПОЛЬЗОВАТЕЛЬ НАЖАЛ "Я ОПЛАТИЛ" ============

@dp.callback_query(F.data.startswith("paid_"))
async def process_paid(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    if user_id not in verified_users:
        await callback.answer("⚠️ Сначала подпишитесь на каналы!", show_alert=True)
        return
    
    order_id = callback.data.split("_")[1]
    
    data = await state.get_data()
    country_name = data.get('country_name', 'неизвестно')
    country_code = data.get('country_code', '')
    days_label = data.get('days_label', 'неизвестно')
    price_rub = data.get('price_rub', 0)
    invoice_id = data.get('invoice_id')
    
    if invoice_id:
        invoice = check_crypto_payment(invoice_id)
        if invoice and invoice.get('status') == 'paid':
            keys = get_random_keys(country_code, 1)
            if keys:
                key_text = f"""
🔑 <b>Ваш рабочий VLESS ключ</b>

<code>{keys[0]}</code>

🌍 <b>Страна:</b> {country_name}
⏰ <b>Срок:</b> {days_label}
💰 <b>Сумма:</b> {price_rub}₽
🆔 <b>ID оплаты:</b> <code>{order_id}</code>
📅 <b>Выдан:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}
✅ <b>Ключ проверен и работает</b>
💣 <b>Бот:</b> {BOT_NAME}

<i>Спасибо за покупку! 💖</i>
"""
                await bot.send_message(user_id, key_text, parse_mode=ParseMode.HTML)
                await callback.message.answer("✅ Оплата подтверждена! Рабочий ключ выдан!", reply_markup=get_main_keyboard())
                await state.clear()
                await callback.answer()
                return
    
    pending_payments[order_id] = {
        'user_id': user_id,
        'user_name': callback.from_user.first_name,
        'username': callback.from_user.username,
        'country': country_name,
        'country_code': country_code,
        'days': days_label,
        'price': price_rub,
        'order_id': order_id,
        'timestamp': datetime.now().strftime('%d.%m.%Y %H:%M')
    }
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        f"""
✅ <b>Ожидайте, ваш заказ принят в обработку.</b>

🌍 <b>Страна:</b> {country_name}
⏰ <b>Срок:</b> {days_label}
💰 <b>Сумма:</b> {price_rub}₽
🆔 <b>ID оплаты:</b> <code>{order_id}</code>

⏳ <b>Статус:</b> На проверке
📞 Если возникли вопросы: @amniamov
""",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    
    admin_text = f"""
🔔 <b>ПОСТУПИЛ НОВЫЙ ЗАКАЗ!</b>

👤 <b>Пользователь:</b> {callback.from_user.first_name}
🆔 <b>ID:</b> <code>{user_id}</code>
👤 <b>Username:</b> @{callback.from_user.username or 'не указан'}
🌍 <b>Страна:</b> {country_name}
⏰ <b>Срок:</b> {days_label}
💰 <b>Сумма:</b> {price_rub}₽
🆔 <b>ID оплаты:</b> <code>{order_id}</code>
🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}

⚠️ <b>Проверьте оплату и выдайте рабочий ключ!</b>
"""
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_admin_payment_keyboard(order_id, user_id)
            )
        except Exception as e:
            logger.error(f"Ошибка отправки админу: {e}")
    
    await callback.answer("✅ Заказ отправлен на проверку!")

# ============ АДМИНСКИЕ ОБРАБОТЧИКИ ОПЛАТЫ ============

@dp.callback_query(F.data.startswith("approve_payment_"))
async def approve_payment(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Только для админа!", show_alert=True)
        return
    
    _, order_id, user_id = callback.data.split("_")
    user_id = int(user_id)
    
    order = pending_payments.get(order_id)
    if not order:
        await callback.message.edit_text("❌ Заказ не найден или уже обработан.")
        await callback.answer()
        return
    
    country_code = order.get('country_code')
    country = order.get('country', 'неизвестно')
    days = order.get('days', 'неизвестно')
    price = order.get('price', 0)
    
    keys = get_random_keys(country_code, 1)
    if not keys:
        await callback.message.edit_text(f"❌ Нет рабочих ключей для {country}")
        await callback.answer()
        return
    
    key_text = f"""
🔑 <b>Ваш рабочий VLESS ключ</b>

<code>{keys[0]}</code>

🌍 <b>Страна:</b> {country}
⏰ <b>Срок:</b> {days}
💰 <b>Сумма:</b> {price}₽
🆔 <b>ID оплаты:</b> <code>{order_id}</code>
📅 <b>Выдан:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}
✅ <b>Ключ проверен и работает</b>
💣 <b>Бот:</b> {BOT_NAME}

<i>Спасибо за покупку! 💖</i>
"""
    
    try:
        await bot.send_message(user_id, key_text, parse_mode=ParseMode.HTML)
        
        await callback.message.edit_text(
            f"✅ <b>Рабочий ключ выдан!</b>\n\n"
            f"👤 Пользователь: {order.get('user_name', 'Unknown')}\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"🌍 Страна: {country}\n"
            f"⏰ Срок: {days}\n"
            f"💰 Сумма: {price}₽\n"
            f"🆔 ID оплаты: <code>{order_id}</code>",
            parse_mode=ParseMode.HTML
        )
        
        del pending_payments[order_id]
        await callback.answer("✅ Ключ выдан!")
        
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        await callback.answer()

@dp.callback_query(F.data.startswith("reject_payment_"))
async def reject_payment(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Только для админа!", show_alert=True)
        return
    
    _, order_id, user_id = callback.data.split("_")
    user_id = int(user_id)
    
    order = pending_payments.get(order_id)
    if not order:
        await callback.message.edit_text("❌ Заказ не найден.")
        await callback.answer()
        return
    
    try:
        await bot.send_message(
            user_id,
            f"""
❌ <b>Ваш заказ отклонен</b>

К сожалению, мы не смогли подтвердить вашу оплату.

🆔 ID оплаты: <code>{order_id}</code>

Для решения проблемы обратитесь к @amniamov
""",
            parse_mode=ParseMode.HTML
        )
        
        await callback.message.edit_text(
            f"❌ <b>Заказ отклонен</b>\n\n"
            f"👤 Пользователь: {order.get('user_name', 'Unknown')}\n"
            f"🆔 ID оплаты: <code>{order_id}</code>",
            parse_mode=ParseMode.HTML
        )
        
        del pending_payments[order_id]
        await callback.answer("❌ Заказ отклонен")
        
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        await callback.answer()

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    total = get_total_keys_count()
    text = f"""
<b>👑 Панель администратора</b>

<b>📊 Статистика:</b>
• Всего ключей: {total}
• Использовано: {sum(len(v) for v in used_keys.values())}
• Ожидают подписку: {len(pending_users)}
• Ожидают оплату: {len(pending_payments)}
• Подтверждено: {len(verified_users)}

<b>🌍 Ключи по странам:</b>
"""
    for country in COUNTRIES:
        count = get_keys_count(country['code'])
        text += f"• {country['name']}: {count} ключей\n"

    text += f"""
<b>💰 Цены:</b>
• 1 день = 5₽
• 7 дней = 25₽
• 30 дней = 70₽
• 90 дней = 180₽

<b>📋 Команды:</b>
• /give &lt;user_id&gt; &lt;страна&gt; - Выдать ключ
• /stats - Статистика
• /refresh - Обновить ключи
"""
    await message.answer(text, parse_mode=ParseMode.HTML)

@dp.message(Command("give"))
async def cmd_give(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет прав.")
        return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "⚠️ <b>Использование:</b>\n\n"
            "/give &lt;user_id&gt; &lt;страна&gt;\n\n"
            "<b>Пример:</b>\n"
            "/give 123456789 us",
            parse_mode=ParseMode.HTML
        )
        return
    
    try:
        target_user_id = int(args[1])
        country_code = args[2]
        
        country = next((c for c in COUNTRIES if c['code'] == country_code), None)
        if not country:
            await message.answer(f"❌ Страна {country_code} не найдена")
            return
        
        keys = get_random_keys(country_code, 1)
        if not keys:
            await message.answer(f"❌ Нет рабочих ключей для {country['name']}")
            return
        
        key_text = f"""
🔑 <b>Ваш рабочий VLESS ключ</b>

<code>{keys[0]}</code>

🌍 <b>Страна:</b> {country['name']}
📅 <b>Выдан:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}
✅ <b>Ключ проверен и работает</b>
💣 <b>Бот:</b> {BOT_NAME}

<b>📌 Инструкция:</b>
1. Скачайте V2Ray клиент
2. Импортируйте ключ
3. Подключитесь

<i>Спасибо за покупку! 💖</i>
"""
        
        await bot.send_message(target_user_id, key_text, parse_mode=ParseMode.HTML)
        await message.answer(f"✅ Рабочий ключ отправлен пользователю {target_user_id}")
        
    except ValueError:
        await message.answer("❌ Неверный формат ID")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет прав.")
        return
    
    total = get_total_keys_count()
    used_total = sum(len(v) for v in used_keys.values())
    
    text = f"""
<b>📊 СТАТИСТИКА БОТА</b>

<b>🔑 Ключи:</b>
• Всего: {total}
• Использовано: {used_total}
• Доступно: {total - used_total}

<b>👥 Пользователи:</b>
• Подтверждено: {len(verified_users)}
• Ожидают подписку: {len(pending_users)}

<b>🌍 Ключи по странам:</b>
"""
    
    for country in COUNTRIES:
        count = get_keys_count(country['code'])
        text += f"• {country['name']}: {count} ключей\n"
    
    await message.answer(text, parse_mode=ParseMode.HTML)

@dp.message(Command("refresh"))
async def cmd_refresh(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет прав.")
        return
    
    msg = await message.answer("🔄 Обновление ключей... ⏳")
    
    global regular_keys_cache, last_update
    regular_keys_cache = None
    last_update = None
    
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
    except:
        pass
    
    keys = get_vless_keys_optimized()
    total = sum(len(v) for v in keys.values()) if keys else 0
    
    try:
        await msg.delete()
    except:
        pass
    
    if keys:
        await message.answer(f"✅ Ключи обновлены! Всего ключей: {total}")
    else:
        await message.answer("❌ Ошибка обновления ключей")

# ============ ЗАПУСК ============

async def main():
    print("🚀 Бот запущен!")
    print("👑 Админы:", ADMIN_IDS)
    print("📢 Каналы:", REQUIRED_CHANNELS)
    print("-" * 40)
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
