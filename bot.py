import os
import json
import base64
import httpx
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue
import anthropic

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OURA_TOKEN = os.environ.get("OURA_TOKEN")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

user_profiles = {}
user_oura = {}
user_meds = {}

SYSTEM_PROMPT = """Ты персональный фитнес и нутриционный коуч в Telegram.
Помогаешь подсушиться, убрать воду, набрать норму белка, заменить один приём пищи протеином.
Пользователь использует протеин Life Isolate: 24г белка, 0.85г углеводов, 1г жиров, 112 ккал, без лактозы, без сахара.
При фото еды — давай КБЖУ таблицей: калории, белки, жиры, углеводы — конкретные цифры.
При фото тела — оцени форму, примерный % жира, рельеф, дай советы для сушки.
Учитывай данные Oura при рекомендациях по тренировкам и питанию.
Отвечай по-русски, конкретно, с цифрами. Используй эмодзи умеренно."""

def get_profile_prompt(user_id):
    p = user_profiles.get(user_id, {})
    o = user_oura.get(user_id, {})
    m = user_meds.get(user_id, {})
    prompt = SYSTEM_PROMPT + "\n\n"
    if p.get("name"): prompt += "Имя: " + p["name"] + "\n"
    if p.get("weight"): prompt += "Вес: " + str(p["weight"]) + " кг\n"
    if p.get("height"): prompt += "Рост: " + str(p["height"]) + " см\n"
    if p.get("age"): prompt += "Возраст: " + str(p["age"]) + " лет\n"
    if p.get("goal"): prompt += "Цель: " + p["goal"] + "\n"
    if p.get("conditions"): prompt += "Особенности здоровья: " + p["conditions"] + "\n"
    if p.get("weight") and p.get("height") and p.get("age"):
        w, h, a = float(p["weight"]), float(p["height"]), float(p["age"])
        bmr = 655+9.6*w+1.8*h-4.7*a if p.get("gender") == "female" else 88+13.7*w+5*h-6.8*a
        mult = 1.2 if p.get("activity") == "low" else 1.725 if p.get("activity") == "high" else 1.55
        prompt += "Норма белка: " + str(round(w*2)) + " г/день\n"
        prompt += "TDEE: " + str(round(bmr*mult)) + " ккал | Цель: " + str(round(bmr*mult)-400) + " ккал\n"
    if m:
        prompt += "\nТаблетки/добавки пользователя:\n"
        for name, info in m.items():
            prompt += "- " + name + ": " + info + "\n"
        prompt += "Учитывай это при рекомендациях!\n"
    if o:
        prompt += "\n=== OURA СЕГОДНЯ ===\n"
        for k, v in o.items():
            prompt += k + ": " + str(v) + "\n"
        prompt += "===================\n"
    return prompt

async def fetch_oura_data(token):
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    base = "https://api.ouraring.com/v2/usercollection/"
    headers = {"Authorization": "Bearer " + token}
    result = {}
    async with httpx.AsyncClient(timeout=15) as client_h:
        for endpoint in ["daily_readiness", "daily_sleep", "daily_activity"]:
            try:
                resp = await client_h.get(base + endpoint + "?start_date=" + yesterday + "&end_date=" + today, headers=headers)
                data = resp.json()
                items = data.get("data", [])
                if items:
                    result[endpoint] = items[-1]
            except:
                pass
    return result

def parse_oura(raw):
    summary = {}
    r = raw.get("daily_readiness", {})
    s = raw.get("daily_sleep", {})
    a = raw.get("daily_activity", {})
    if r.get("score"): summary["Readiness Score"] = str(r["score"]) + "/100"
    if s.get("total_sleep_duration"): summary["Сон"] = str(round(s["total_sleep_duration"]/3600, 1)) + " ч"
    if s.get("efficiency"): summary["Эффективность сна"] = str(s["efficiency"]) + "%"
    if s.get("deep_sleep_duration"): summary["Deep sleep"] = str(round(s["deep_sleep_duration"]/60)) + " мин"
    if s.get("rem_sleep_duration"): summary["REM sleep"] = str(round(s["rem_sleep_duration"]/60)) + " мин"
    if s.get("average_hrv"): summary["HRV"] = str(s["average_hrv"]) + " мс"
    if s.get("average_heart_rate"): summary["ЧСС ночью"] = str(s["average_heart_rate"]) + " уд/мин"
    if a.get("active_calories"): summary["Активные калории"] = str(a["active_calories"]) + " ккал"
    if a.get("steps"): summary["Шаги"] = str(a["steps"])
    if a.get("score"): summary["Активность Score"] = str(a["score"]) + "/100"
    return summary

def ask_claude(system, messages):
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system,
        messages=messages
    )
    return response.content[0].text

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📊 Мой профиль"), KeyboardButton("💍 Синхр. Oura")],
    [KeyboardButton("🥗 План питания"), KeyboardButton("💧 Как убрать воду?")],
    [KeyboardButton("💊 Мои таблетки"), KeyboardButton("📈 Моя статистика")],
    [KeyboardButton("🏋️ Тренировка сегодня"), KeyboardButton("🌅 Утренний план")],
], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        "Привет, " + name + "! 💪\n\n"
        "Я твой персональный FitCoach на базе Claude AI.\n\n"
        "Что умею:\n"
        "• 📸 КБЖУ по фото тарелки\n"
        "• 📷 Анализ прогресс-фото\n"
        "• 💍 Автосинхронизация с Oura\n"
        "• 💊 Напоминания о таблетках\n"
        "• 🥗 Советы по питанию на сушке\n"
        "• 🏋️ Анализ тренировок\n\n"
        "Начнём с профиля — /profile",
        reply_markup=MAIN_KEYBOARD
    )

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Заполним профиль!\n\n"
        "Напиши через запятую:\n"
        "Имя, Вес(кг), Рост(см), Возраст, Пол(м/ж), Активность(низкая/средняя/высокая)\n\n"
        "Пример:\nДаша, 58, 165, 27, ж, средняя"
    )
    context.user_data["waiting_profile"] = True

async def meds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    m = user_meds.get(user_id, {})
    if m:
        msg = "💊 Твои таблетки и добавки:\n\n"
        for name, info in m.items():
            msg += "• " + name + ": " + info + "\n"
        msg += "\nЧтобы добавить — напиши:\n💊 Название: время приёма\n\nПример:\n💊 Магний: вечером перед сном"
    else:
        msg = "💊 Таблетки не добавлены.\n\nЧтобы добавить — напиши:\n💊 Название: время приёма\n\nПример:\n💊 Витамин D: утром с едой\n💊 Омега-3: 2 раза в день с едой\n💊 Магний: вечером перед сном"
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

async def oura_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    token = OURA_TOKEN
    if not token:
        await update.message.reply_text("Токен Oura не настроен.")
        return
    await update.message.reply_text("⏳ Загружаю данные с Oura...")
    try:
        raw = await fetch_oura_data(token)
        if not raw:
            await update.message.reply_text("Не удалось получить данные.")
            return
        summary = parse_oura(raw)
        user_oura[user_id] = summary
        r_score = int(raw.get("daily_readiness", {}).get("score", 0))
        icon = "🟢" if r_score >= 85 else "🟡" if r_score >= 70 else "🔴"
        msg = icon + " Данные Oura синхронизированы!\n\n"
        for k, v in summary.items():
            msg += k + ": " + v + "\n"
        msg += "\nХочешь рекомендации на сегодня?"
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    if context.user_data.get("waiting_profile"):
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 6:
            activity_map = {"низкая": "low", "средняя": "medium", "высокая": "high"}
            gender_map = {"м": "male", "ж": "female"}
            user_profiles[user_id] = {
                "name": parts[0], "weight": parts[1], "height": parts[2],
                "age": parts[3], "gender": gender_map.get(parts[4].lower(), "female"),
                "activity": activity_map.get(parts[5].lower(), "medium"),
                "goal": parts[6].strip() if len(parts) > 6 else "сушка, убрать воду, набрать белок",
            }
            context.user_data["waiting_profile"] = False
            p = user_profiles[user_id]
            w = float(p["weight"])
            await update.message.reply_text(
                "✅ Профиль сохранён!\n\n"
                + p["name"] + " | " + p["weight"] + " кг | " + p["height"] + " см\n"
                "Норма белка: " + str(round(w*2)) + " г/день\n\n"
                "Теперь добавь таблетки — нажми 💊 Мои таблетки\n"
                "И синхронизируй Oura — нажми 💍 Синхр. Oura",
                reply_markup=MAIN_KEYBOARD
            )
        else:
            await update.message.reply_text("Напиши: Имя, Вес, Рост, Возраст, Пол(м/ж), Активность")
        return

    if text.startswith("💊 ") and ":" in text:
        med_text = text[2:].strip()
        parts = med_text.split(":", 1)
        if len(parts) == 2:
            med_name = parts[0].strip()
            med_time = parts[1].strip()
            if user_id not in user_meds:
                user_meds[user_id] = {}
            user_meds[user_id][med_name] = med_time
            await update.message.reply_text(
                "✅ Добавлено: " + med_name + "\nВремя приёма: " + med_time + "\n\nДобавить ещё? Пиши:\n💊 Название: время",
                reply_markup=MAIN_KEYBOARD
            )
            return

    if text == "📊 Мой профиль":
        await profile_cmd(update, context)
        return
    elif text == "💍 Синхр. Oura":
        await oura_cmd(update, context)
        return
    elif text == "💊 Мои таблетки":
        await meds_cmd(update, context)
        return
    elif text == "📈 Моя статистика":
        p = user_profiles.get(user_id, {})
        o = user_oura.get(user_id, {})
        m = user_meds.get(user_id, {})
        if not p:
            await update.message.reply_text("Сначала заполни профиль — /profile")
            return
        w = float(p.get("weight", 0))
        h = float(p.get("height", 0))
        bmi = round(w/((h/100)**2), 1) if w and h else "?"
        msg = "📊 Статистика\n\n"
        msg += p.get("name","?") + " | " + str(p.get("weight","?")) + " кг | " + str(p.get("height","?")) + " см\n"
        msg += "ИМТ: " + str(bmi) + "\n"
        msg += "Норма белка: " + str(round(w*2)) + " г/день\n"
        if m:
            msg += "\n💊 Таблетки:\n"
            for name, info in m.items():
                msg += "  • " + name + ": " + info + "\n"
        if o:
            msg += "\n💍 Oura:\n"
            for k, v in o.items():
                msg += "  " + k + ": " + str(v) + "\n"
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
        return
    elif text == "🌅 Утренний план":
        text = "Составь мой утренний план на сегодня: когда принять таблетки, что съесть на завтрак, стоит ли тренироваться — учитывай данные Oura и мой профиль."

    await update.message.chat.send_action("typing")
    try:
        reply = ask_claude(get_profile_prompt(user_id), [{"role": "user", "content": text}])
        await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    caption = update.message.caption or "Оцени это фото. Если еда — дай КБЖУ таблицей с цифрами. Если тело — оцени форму, % жира, дай советы для сушки."
    await update.message.chat.send_action("typing")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()
    img_base64 = base64.b64encode(img_bytes).decode("utf-8")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=get_profile_prompt(user_id),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_base64}},
                    {"type": "text", "text": caption}
                ]
            }]
        )
        reply = response.content[0].text
        await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("oura", oura_cmd))
    app.add_handler(CommandHandler("meds", meds_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("FitCoach Claude bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
