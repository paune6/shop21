import logging
import re
import ast
import operator
import json
import os
import time
import textwrap
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from tavily import TavilyClient
from g4f.client import Client as G4FClient
import requests
from langdetect import detect, DetectorFactory

# Фиксируем seed для langdetect
DetectorFactory.seed = 0

# -------------------- КОНФИГУРАЦИЯ --------------------
TAVILY_API_KEY = "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"
BOT_TOKEN = "8541046578:AAHcYxpX12EMIDXl8c5ZyigQnIutuvOIe7I"
ADMIN_CHAT_ID = 5078387190  # Замените на ваш реальный ID

USERS_FILE = "users.json"
ADMIN_REPLIES_FILE = "admin_replies.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- РАБОТА С ФАЙЛАМИ --------------------
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(users), f, ensure_ascii=False)

def add_user(user_id):
    users = load_users()
    if user_id not in users:
        users.add(user_id)
        save_users(users)

def load_admin_replies():
    if os.path.exists(ADMIN_REPLIES_FILE):
        with open(ADMIN_REPLIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_admin_replies(replies):
    with open(ADMIN_REPLIES_FILE, "w", encoding="utf-8") as f:
        json.dump(replies, f, ensure_ascii=False)

def add_admin_reply(admin_msg_id, user_id):
    replies = load_admin_replies()
    replies[str(admin_msg_id)] = user_id
    save_admin_replies(replies)

def pop_admin_reply(admin_msg_id):
    replies = load_admin_replies()
    user_id = replies.pop(str(admin_msg_id), None)
    save_admin_replies(replies)
    return user_id

# -------------------- КНОПКИ --------------------
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Помощь"), KeyboardButton("О боте"), KeyboardButton("Сообщить об ошибке")],
        [KeyboardButton("Начать разговор")],
    ],
    resize_keyboard=True,
)

FUNCTIONS_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔍 Поиск", callback_data="mode_search")],
    [InlineKeyboardButton("💬 Общение", callback_data="mode_chat")],
    [InlineKeyboardButton("🌤 Погода", callback_data="func_weather")],
    [InlineKeyboardButton("🌐 Перевод", callback_data="func_translate")],
    [InlineKeyboardButton("😂 Шутка", callback_data="func_joke")],
    [InlineKeyboardButton("🧮 Калькулятор", callback_data="func_calc")],
    [InlineKeyboardButton("ℹ️ О боте", callback_data="info_about")],
    [InlineKeyboardButton("❓ Помощь", callback_data="info_help")],
    [InlineKeyboardButton("⏹ Закончить", callback_data="end_session")],
])

# -------------------- ФИЛЬТР МАТА --------------------
BAD_WORDS = {
    "бля", "хуй", "пизда", "ебать", "сука", "нахер",
    "залупа", "пидор", "гандон", "мудак", "дерьмо", "жопа",
    "шлюха", "уебан", "долбоёб", "член", "хер",
}
BAD_PATTERN = re.compile(r"\b(" + "|".join(re.escape(w) for w in BAD_WORDS) + r")\b", re.IGNORECASE)

# -------------------- ПОИСК (Tavily) --------------------
class SearchEngine:
    def __init__(self, api_key):
        self.client = TavilyClient(api_key=api_key)

    def search(self, query: str, max_results=5) -> dict:
        try:
            return self.client.search(
                query=query,
                search_depth="advanced",
                max_results=max_results,
                include_answer=True,
                include_images=False,
            )
        except Exception as e:
            logger.error(f"Tavily error: {e}")
            return {"answer": None, "results": [], "error": str(e)}

    def extract_direct_answer(self, data: dict) -> str | None:
        if data.get("error"):
            return None
        return data.get("answer")

# -------------------- ИИ-МОДУЛЬ С УЛУЧШЕННЫМ ПЕРЕВОДОМ --------------------
class AIChat:
    SYSTEM_PROMPT = (
        "Ты полезный ассистент. Отвечай кратко, по делу и ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. "
        "Если вопрос задан на другом языке, всё равно отвечай на русском. "
        "Если переданы результаты поиска, используй их для построения ответа."
    )

    def __init__(self):
        self.g4f_client = G4FClient()
        self.max_retries = 2

    def _is_likely_russian(self, text: str) -> bool:
        if not text:
            return False
        try:
            lang = detect(text)
            return lang == 'ru'
        except:
            russian_letters = len(re.findall(r'[а-яё]', text.lower()))
            total_letters = len(re.findall(r'[a-zа-яё]', text.lower()))
            if total_letters == 0:
                return False
            return (russian_letters / total_letters) >= 0.3

    def _call_g4f(self, messages, **kwargs):
        for attempt in range(self.max_retries):
            try:
                response = self.g4f_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    **kwargs
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"G4F attempt {attempt+1} failed: {e}")
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(1)

    def translate_to_russian(self, text: str) -> str:
        if not text or self._is_likely_russian(text):
            return text

        MAX_CHUNK_SIZE = 1800
        if len(text) > MAX_CHUNK_SIZE:
            chunks = textwrap.wrap(text, MAX_CHUNK_SIZE, break_long_words=False)
            translated_chunks = [self._translate_single_chunk(chunk) for chunk in chunks]
            return " ".join(translated_chunks)
        else:
            return self._translate_single_chunk(text)

    def _translate_single_chunk(self, chunk: str) -> str:
        system_prompt = (
            "Ты профессиональный лингвист и переводчик. Твоя задача — перевести ЛЮБОЙ текст на русский язык "
            "максимально точно, естественно и с сохранением стиля. Учитывай идиомы, сленг, культурные отсылки, "
            "терминологию. Если в тексте есть аббревиатуры, расшифровывай их. Отвечай ТОЛЬКО переводом, без "
            "пояснений, без кавычек, без дополнительных слов."
        )
        user_prompt = f"Переведи на русский язык:\n{chunk}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        try:
            translated = self._call_g4f(messages, max_tokens=2000, temperature=0.0)
            if translated and len(translated) > len(chunk) * 0.3:
                return self._postprocess_translation(translated)
            else:
                return chunk
        except Exception as e:
            logger.warning(f"Translation failed: {e}")
            return chunk

    def _postprocess_translation(self, text: str) -> str:
        text = re.sub(r'^(Перевод|Ответ|Результат|Текст)\s*[:;]\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^[\'"“”«»]+|[\'"“”«»]+$', '', text).strip()
        text = re.sub(r'\s+', ' ', text)
        return text

    def ask_with_context(self, history: list[dict], user_msg: str, snippets: list[str] | None = None) -> str:
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        if snippets:
            context_text = "Факты из поиска:\n" + "\n".join(snippets[:5])
            messages.append({"role": "system", "content": context_text})
        messages += history
        messages.append({"role": "user", "content": user_msg})

        try:
            text = self._call_g4f(messages, max_tokens=800, temperature=0.3 if snippets else 0.7)
            if text and not self._is_likely_russian(text):
                text = self.translate_to_russian(text)
            return text
        except Exception as e:
            logger.error(f"G4F error after retries: {e}")
            return "Извините, сейчас ИИ недоступен."

# -------------------- ПОГОДА --------------------
def get_weather(city: str) -> str:
    try:
        url = f"https://wttr.in/{city}?format=%t+%w&lang=ru"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.text.strip()
            if not data or "Извините" in data:
                return "Не удалось получить данные о погоде. Проверьте название города."
            return f"Погода в городе {city}:\n{data}"
        return "Не удалось получить данные о погоде."
    except Exception as e:
        logger.error(f"Weather error: {e}")
        return "Ошибка при запросе погоды."

# -------------------- КАЛЬКУЛЯТОР --------------------
def safe_eval(expr: str) -> str:
    allowed_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
    }
    try:
        tree = ast.parse(expr, mode='eval')
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and type(node.op) not in allowed_ops:
                return "Недопустимая операция."
            if isinstance(node, ast.UnaryOp) and type(node.op) not in allowed_ops:
                return "Недопустимая операция."
            if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
                return "Только числа."
        result = eval(compile(tree, '<string>', 'eval'), {"__builtins__": {}}, allowed_ops)
        return str(result)
    except Exception:
        return "Ошибка в выражении. Пример: 2+2*3"

# -------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ --------------------
def is_greeting(text: str) -> bool:
    greeting_words = r"\b(привет|здравствуй|добрый день|доброе утро|добрый вечер|здарова|хай|хелло|hi|hello)\b"
    if re.search(greeting_words, text, re.IGNORECASE):
        if len(text.split()) <= 5 and "?" not in text:
            return True
    return False

def contains_bad_words(text: str) -> bool:
    return bool(BAD_PATTERN.search(text))

def get_bot_name(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    return context.user_data.get("bot_name")

def is_waiting_for_name(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("waiting_for_name", False)

def get_chat_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("chat_mode", False)

# -------------------- ОБРАБОТКА ПОИСКОВОГО ЗАПРОСА --------------------
async def process_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, message=None):
    msg = message or update.message
    await msg.chat.send_action("typing")
    engine = SearchEngine(TAVILY_API_KEY)
    data = engine.search(query)

    direct = engine.extract_direct_answer(data)
    if direct and len(direct) > 10:
        ai = AIChat()
        if not ai._is_likely_russian(direct):
            direct = ai.translate_to_russian(direct)
        await msg.reply_text(direct)
        return

    snippets = [r.get("content", "") for r in data.get("results", []) if r.get("content")]
    if not snippets:
        await msg.reply_text("❌ Ничего не найдено по вашему запросу. Попробуйте переформулировать.")
        return

    ai = AIChat()
    history = context.user_data.get("chat_history", [])
    answer = ai.ask_with_context(history, query, snippets)

    await msg.reply_text(answer)
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": answer})
    context.user_data["chat_history"] = history[-40:]

# -------------------- КОМАНДЫ --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user(user_id)

    bot_name = get_bot_name(context)
    if not bot_name:
        context.user_data["waiting_for_name"] = True
        await update.message.reply_text(
            "👋 Добро пожаловать! Я **Simul BM 100**.\n"
            "Придумайте мне имя (без мата), чтобы я знал, как ко мне обращаться.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"🤖 **Simul BM 100**\nЯ {bot_name}, ваш ассистент. Используйте кнопки или просто задайте вопрос.",
            reply_markup=MAIN_KEYBOARD,
            parse_mode="Markdown",
        )

async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите новое имя: /rename Василий")
        return
    new_name = " ".join(context.args)
    if len(new_name) > 20:
        await update.message.reply_text("Имя слишком длинное. До 20 символов.")
        return
    if contains_bad_words(new_name):
        await update.message.reply_text("Имя не должно содержать нецензурных слов.")
        return
    context.user_data["bot_name"] = new_name
    context.user_data.pop("waiting_for_name", None)
    await update.message.reply_text(f"🤖 Теперь меня зовут **{new_name}**.", parse_mode="Markdown")

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите город: /weather Москва")
        return
    city = " ".join(context.args)
    await update.message.chat.send_action("typing")
    result = get_weather(city)
    await update.message.reply_text(result)

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Введите текст для перевода: /translate Hello")
        return
    text = " ".join(context.args)
    await update.message.chat.send_action("typing")
    ai = AIChat()
    translated = ai.translate_to_russian(text)
    await update.message.reply_text(f"Перевод на русский:\n{translated}")

async def joke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    ai = AIChat()
    joke_text = ai.ask_with_context([], "Расскажи короткую смешную шутку на русском языке")
    if not ai._is_likely_russian(joke_text):
        joke_text = ai.translate_to_russian(joke_text)
    await update.message.reply_text(joke_text)

async def calc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Введите выражение: /calc 2+2*3")
        return
    expr = " ".join(context.args)
    result = safe_eval(expr)
    await update.message.reply_text(f"Результат: {result}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 **Команды Simul BM 100:**\n"
        "/start — настройка имени или приветствие\n"
        "/rename <имя> — сменить имя бота\n"
        "/search <запрос> — поиск информации\n"
        "/weather <город> — погода\n"
        "/translate <текст> — перевод на русский\n"
        "/joke — случайная шутка\n"
        "/calc <выражение> — калькулятор\n"
        "/reset — очистить историю диалога\n"
        "/help — эта справка\n\n"
        "Кнопки внизу помогут быстро воспользоваться функциями.",
        parse_mode="Markdown",
    )

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Укажите запрос: /search курс доллара")
        return
    await process_search(update, context, query)

async def reset_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("chat_history", None)
    await update.message.reply_text("🧠 История диалога очищена.")

# -------------------- АДМИНИСТРАТОР: РАССЫЛКА --------------------
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return

    if not context.args:
        await update.message.reply_text("📢 Использование: /broadcast <текст сообщения>")
        return

    broadcast_text = " ".join(context.args)
    users = load_users()
    sent = 0
    failed = 0

    await update.message.reply_text(f"Начинаю рассылку для {len(users)} пользователей...")

    for user_id in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📣 **Рассылка от администратора:**\n{broadcast_text}", parse_mode="Markdown")
            sent += 1
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
            failed += 1

    await update.message.reply_text(f"✅ Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}")

# -------------------- ОБРАБОТЧИК INLINE-КНОПОК --------------------
async def handle_inline_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "end_session":
        await query.edit_message_text("👋 Сеанс завершён. Продолжайте пользоваться ботом через основные кнопки или текстом.")
        context.user_data.pop("expecting_weather", None)
        context.user_data.pop("expecting_translate", None)
        context.user_data.pop("expecting_calc", None)
        context.user_data.pop("waiting_for_bug_report", None)
        return

    if data == "mode_search":
        context.user_data["chat_mode"] = False
        context.user_data.pop("chat_history", None)
        await query.edit_message_text("🔍 Режим поиска включён. Задайте вопрос текстом или нажмите «Начать разговор» снова.")
    elif data == "mode_chat":
        context.user_data["chat_mode"] = True
        context.user_data.pop("chat_history", None)
        await query.edit_message_text("💬 Режим общения включён. Пишите, о чём хотите поговорить.")
    elif data == "func_weather":
        await query.edit_message_text("Введите название города, чтобы узнать погоду. Например: Москва")
        context.user_data["expecting_weather"] = True
    elif data == "func_translate":
        await query.edit_message_text("Введите текст для перевода на русский язык.")
        context.user_data["expecting_translate"] = True
    elif data == "func_joke":
        await query.edit_message_text("Сейчас придумаю шутку...")
        ai = AIChat()
        joke_text = ai.ask_with_context([], "Расскажи короткую смешную шутку на русском языке")
        if not ai._is_likely_russian(joke_text):
            joke_text = ai.translate_to_russian(joke_text)
        await query.edit_message_text(joke_text)
    elif data == "func_calc":
        await query.edit_message_text("Введите математическое выражение (например, 2+2*3)")
        context.user_data["expecting_calc"] = True
    elif data == "info_about":
        await query.edit_message_text(
            "🤖 **Simul BM 100** — первая модель поискового ассистента.\n"
            "🔹 Быстрый поиск в интернете\n"
            "🔹 Погода, переводчик, калькулятор\n"
            "🔹 Шутки и простой диалог\n"
            "🔹 Отвечаю только на русском языке\n\n"
            "Версия: BM 100 (MVP)",
            parse_mode="Markdown",
        )
    elif data == "info_help":
        await query.edit_message_text(
            "📌 **Команды Simul BM 100:**\n"
            "/start — настройка имени\n"
            "/rename <имя> — сменить имя\n"
            "/search <запрос> — поиск\n"
            "/weather <город> — погода\n"
            "/translate <текст> — перевод\n"
            "/joke — шутка\n"
            "/calc <выражение> — калькулятор\n"
            "/reset — очистить историю\n"
            "/help — эта справка\n\n"
            "Кнопка «Сообщить об ошибке» отправит ваше сообщение администратору.",
            parse_mode="Markdown",
        )

# -------------------- ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ --------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user(user_id)

    text = update.message.text.strip()
    if not text:
        return

    if context.user_data.get("expecting_weather"):
        context.user_data.pop("expecting_weather", None)
        city = text
        await update.message.chat.send_action("typing")
        result = get_weather(city)
        await update.message.reply_text(result)
        return

    if context.user_data.get("expecting_translate"):
        context.user_data.pop("expecting_translate", None)
        await update.message.chat.send_action("typing")
        ai = AIChat()
        translated = ai.translate_to_russian(text)
        await update.message.reply_text(f"Перевод на русский:\n{translated}")
        return

    if context.user_data.get("expecting_calc"):
        context.user_data.pop("expecting_calc", None)
        result = safe_eval(text)
        await update.message.reply_text(f"Результат: {result}")
        return

    if context.user_data.get("waiting_for_bug_report"):
        context.user_data.pop("waiting_for_bug_report", None)
        user = update.effective_user
        user_info = f"От: {user.full_name} (@{user.username}, id: {user.id})"
        bug_text = f"📩 Сообщение об ошибке\n{user_info}\n\n{text}"
        try:
            admin_msg = await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=bug_text)
            add_admin_reply(admin_msg.message_id, user_id)
            await update.message.reply_text("✅ Спасибо! Ваше сообщение отправлено администратору. Когда он ответит, вы получите уведомление.")
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение админу: {e}")
            await update.message.reply_text("❌ Не удалось отправить сообщение. Попробуйте позже.")
        return

    if is_waiting_for_name(context):
        if len(text) > 20:
            await update.message.reply_text("Имя слишком длинное. До 20 символов.")
            return
        if contains_bad_words(text):
            await update.message.reply_text("Имя не должно содержать нецензурных слов.")
            return
        context.user_data["bot_name"] = text
        context.user_data.pop("waiting_for_name", None)
        await update.message.reply_text(
            f"🤖 **Simul BM 100**\nТеперь меня зовут **{text}**.\nИспользуйте кнопки для управления.",
            reply_markup=MAIN_KEYBOARD,
            parse_mode="Markdown",
        )
        return

    bot_name = get_bot_name(context)

    if text == "Начать разговор":
        if not bot_name:
            await update.message.reply_text("Сначала дайте мне имя через /start.")
            return
        await update.message.reply_text(
            f"🤖 **{bot_name}** к вашим услугам. Выберите действие:",
            reply_markup=FUNCTIONS_MENU,
            parse_mode="Markdown",
        )
        return

    if text == "О боте":
        await update.message.reply_text(
            "🤖 **Simul BM 100** — первая модель поискового ассистента.\n"
            "🔹 Быстрый поиск в интернете\n"
            "🔹 Погода, переводчик, калькулятор\n"
            "🔹 Шутки и простой диалог\n"
            "🔹 Отвечаю только на русском языке\n\n"
            "Версия: BM 100 (MVP)",
            parse_mode="Markdown",
        )
        return

    if text == "Помощь":
        await help_command(update, context)
        return

    if text == "Сообщить об ошибке":
        context.user_data["waiting_for_bug_report"] = True
        await update.message.reply_text(
            "📝 Опишите проблему или ошибку, которую вы заметили. Ваше сообщение будет отправлено администратору."
        )
        return

    if is_greeting(text):
        if bot_name:
            await update.message.reply_text(
                f"🤖 **Simul BM 100**\nПривет! Я {bot_name}. Чем могу помочь?",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "Привет! Мы ещё не знакомы. Напишите /start, чтобы дать мне имя.",
            )
        return

    if not bot_name:
        context.user_data["waiting_for_name"] = True
        await update.message.reply_text(
            "👋 Я **Simul BM 100**. Придумайте мне имя, чтобы мы могли начать."
        )
        return

    if get_chat_mode(context):
        await update.message.chat.send_action("typing")
        ai = AIChat()
        history = context.user_data.get("chat_history", [])
        answer = ai.ask_with_context(history, text)
        await update.message.reply_text(answer)
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": answer})
        context.user_data["chat_history"] = history[-40:]
    else:
        await process_search(update, context, text)

# -------------------- ОБРАБОТЧИК ОТВЕТОВ АДМИНИСТРАТОРА --------------------
async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if not update.message.reply_to_message:
        return

    replied_msg = update.message.reply_to_message
    if replied_msg.from_user.id != context.bot.id:
        return

    user_id = pop_admin_reply(replied_msg.message_id)
    if user_id is None:
        await update.message.reply_text("❌ Не удалось определить пользователя для ответа. Возможно, ответ уже был отправлен.")
        return

    reply_text = update.message.text
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"📨 **Ответ от администратора:**\n{reply_text}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(f"✅ Ответ отправлен пользователю (id: {user_id}).")
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю {user_id}: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить ответ пользователю {user_id}.")

# -------------------- ЗАПУСК --------------------
def main():
    app = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rename", rename_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("translate", translate_command))
    app.add_handler(CommandHandler("joke", joke_command))
    app.add_handler(CommandHandler("calc", calc_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("reset", reset_dialog))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    app.add_handler(CallbackQueryHandler(handle_inline_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_CHAT_ID) & filters.TEXT & ~filters.COMMAND, handle_admin_reply), group=1)

    logger.info("Simul BM 100 запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()