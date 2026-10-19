import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from collections import defaultdict

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import TOKEN

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ В ПАМЯТИ (Для теста. Для продакшена лучше Supabase/SQLite) ---
# Стоп-слова
STOP_WORDS = ["крипта", "скам", "реклама", "казино", "ставки"]
# Матерные корни (упрощенно)
MAT_WORDS = ["хуй", "пизд", "бля", "ебан", "сука", "мразь"]
# Предупреждения {user_id: count}
warnings = defaultdict(int)
# Время последнего сообщения {user_id: time}
last_message_time = defaultdict(float)
# Пользователи, ожидающие капчу {user_id: True}
waiting_for_captcha = {}

# --- НАСТРОЙКИ ---
SPAM_TIME_LIMIT = 2 # секунд между сообщениями
MAX_WARNINGS = 3

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def is_mat(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in MAT_WORDS)

def is_stop_word(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in STOP_WORDS)

def has_links(text: str) -> bool:
    # Ищем http, https, t.me, www.
    pattern = r'(https?://|t\.me/|www\.)'
    return bool(re.search(pattern, text, re.IGNORECASE))

async def punish_user(message: types.Message, reason: str):
    user_id = message.from_user.id
    warnings[user_id] += 1
    
    if warnings[user_id] >= MAX_WARNINGS:
        # Бан
        try:
            await bot.ban_chat_member(message.chat.id, user_id)
            await message.answer(f"⛔️ Пользователь {message.from_user.full_name} забанен. Причина: {reason} (Лимит предупреждений)")
        except Exception as e:
            await message.answer(f"❌ Не могу забанить. Проверьте права бота. Ошибка: {e}")
    else:
        # Мут на 1 час
        mute_until = datetime.now() + timedelta(hours=1)
        try:
            await bot.restrict_chat_member(
                message.chat.id, 
                user_id, 
                permissions=ChatPermissions(can_send_messages=False),
                until_date=mute_until
            )
            await message.answer(f"⚠️ {message.from_user.full_name}, предупреждение {warnings[user_id]}/{MAX_WARNINGS}. Причина: {reason}. Вы замьючены на 1 час.")
        except Exception as e:
             await message.answer(f"❌ Не могу замутить. Проверьте права бота. Ошибка: {e}")

    # Удаляем сообщение-нарушение
    try:
        await message.delete()
    except:
        pass


# --- ОБРАБОТЧИКИ ---

# 1. КАПЧА (Новый участник)
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join(event: types.ChatMemberUpdated):
    user = event.new_chat_member.user
    chat_id = event.chat.id
    
    # Если бот сам зашел, не проверяем
    if user.is_bot:
        return

    waiting_for_captcha[user.id] = True

    # Ограничиваем права до прохождения капчи
    try:
        await bot.restrict_chat_member(
            chat_id, user.id,
            permissions=ChatPermissions(can_send_messages=False)
        )
    except Exception as e:
        print(f"Ошибка ограничения: {e}")

    # Отправляем сообщение с кнопкой
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я не робот", callback_data=f"captcha_{user.id}")]
    ])
    
    msg = await bot.send_message(
        chat_id, 
        f"Привет, {user.full_name}! Чтобы получить доступ к чату, нажми на кнопку ниже.",
        reply_markup=keyboard
    )
    
    # Удаляем сообщение через 2 минуты, если не нажали
    await asyncio.sleep(120)
    if waiting_for_captcha.get(user.id):
        try:
            await msg.delete()
            await bot.ban_chat_member(chat_id, user.id) # Кикаем, если не прошли
        except:
            pass
        waiting_for_captcha.pop(user.id, None)


# 2. ОБРАБОТКА НАЖАТИЯ КНОПКИ КАПЧИ
@dp.callback_query(F.data.startswith("captcha_"))
async def process_captcha(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для вас!", show_alert=True)
        return

    # Снимаем ограничения
    try:
        await bot.restrict_chat_member(
            callback.message.chat.id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        waiting_for_captcha.pop(user_id, None)
        await callback.message.edit_text("✅ Капча пройдена! Добро пожаловать в чат.")
    except Exception as e:
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)
        print(f"Ошибка снятия ограничений: {e}")


# 3. АНТИСПАМ И ФИЛЬТРЫ (Все сообщения)
@dp.message(F.text)
async def check_message(message: types.Message):
    # Игнорируем админов и самого бота
    if message.from_user.is_bot:
        return
    
    # Проверка на капчу (если еще не прошел)
    if waiting_for_captcha.get(message.from_user.id):
        await message.delete()
        return

    text = message.text.lower()
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Антифлуд (слишком часто пишет)
    current_time = time.time()
    if current_time - last_message_time[user_id] < SPAM_TIME_LIMIT:
        await message.delete()
        return
    last_message_time[user_id] = current_time

    # Антимат
    if is_mat(text):
        await punish_user(message, "Мат")
        return

    # Стоп-слова
    if is_stop_word(text):
        await punish_user(message, "Запрещенное слово (Крипта/Скам и т.д.)")
        return

    # Антиспам (ссылки от новых пользователей или всех, кроме админов)
    if has_links(text):
        # Если хотите разрешить ссылки админам, нужна проверка прав. 
        # Пока для простоты удаляем у всех.
        await punish_user(message, "Ссылки запрещены")
        return


# 4. КОМАНДЫ ДЛЯ АДМИНОВ
@dp.message(Command("addstop"))
async def add_stop_word(message: types.Message):
    # Проверка, что команду дал админ
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ["administrator", "creator"]:
        return
        
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /addstop <слово>")
        return
    
    new_word = args[1].lower().strip()
    if new_word not in STOP_WORDS:
        STOP_WORDS.append(new_word)
        await message.answer(f"✅ Слово '{new_word}' добавлено в стоп-лист.")
    else:
        await message.answer("Это слово уже в списке.")


@dp.message(Command("stats"))
async def get_stats(message: types.Message):
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ["administrator", "creator"]:
        return
    
    await message.answer(
        f"📊 **Статистика чата:**\n"
        f"👤 Пользователей в ожидании капчи: {len(waiting_for_captcha)}\n"
        f"📝 Стоп-слов в базе: {len(STOP_WORDS)}\n"
        f"🚫 Матерных корней: {len(MAT_WORDS)}",
        parse_mode="Markdown"
    )


# --- ЗАПУСК ---
async def main():
    print("Бот-защитник запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
