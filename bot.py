import os
import json
import base64
import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

user_profiles = {}
user_oura = {}

SYSTEM_PROMPT = """Ты персональный фитнес и нутриционный коуч в Telegram.
Помогаешь подсушиться, убрать воду, набрать норму белка, заменить один приём пищи протеином.
Пользователь использует протеин Life Isolate: 24г белка, 0.85г углеводов, 1г жиров, 112 ккал, без лактозы, без сахара.
При JSON от Oura API - распарси и выдай красивую сводку плюс рекомендации на день.
Отвечай по-русски, конкретно, с цифрами. Используй эмодзи умеренно."""

def get_profile_prompt(user_id):
    p = user_profiles.get(user_id, {})
    o = user_oura.get(user_id, {})
    prompt = SYSTEM_PROMPT + "\n\n"
    if p.get("name"): prompt += "Имя: " + p["name"] + "\n"
    if p.get("weight"): prompt += "Вес: " + str(p["weight"]) + " кг\n"
    if p.get("height"): prompt += "Рост: " + str(p["height"]) + " см\n"
    if p.get("age"): prompt += "Возраст: " + str(p["age"]) + " лет\n"
    if p.get("goal"): prompt += "Цель: " + p["goal"] + "\n"
    if p.get("weight") and p.get("height") and p.get("age"):
        w, h, a = float(p["weight"]), float(p["height"]), float(p["age"])
        bmr = 655+9.6*w+1.8*h-4.7*a if p.get("gender") == "female" else 88+13.7*w+5*h-6.8*a
        mult = 1.2 if p.get("activity") == "low" else 1.725 if p.get("activity") == "high" else 1.55
        prompt += "Норма белка: " + str(round(w*2)) + " г/день\n"
        prompt += "TDEE: " + str(round(bmr*mult)) + " ккал | Цель: " + str(round(bmr*mult)-400) + " ккал\n"
    if o:
        prompt += "\n=== OURA СЕГОДНЯ ===\n"
        for k, v in o.items():
            prompt += k + ": " + str(v) + "\n"
        prompt += "===================\n"
    return prompt

async def ask_groq(system, user_text):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + GROQ_API_KEY, "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text}
                ],
                "max_tokens": 1000,
            }
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"]

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📊 Мой профиль"), KeyboardButton("💍 Ввести Oura")],
    [KeyboardButton("🥗 План питания"), KeyboardButton("💧 Как убрать воду?")],
    [KeyboardButton("🏋️ Тренировка сегодня"), KeyboardButton("📈 Моя статистика")],
], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        "Привет, " + name + "! 💪\n\n"
        "Я твой персональный FitCoach.\n\n"
        "Что умею:\n"
        "• 📸 Посчитать КБЖУ по фото тарелки\n"
        "• 💍 Анализировать данные Oura\n"
        "• 🥗 Советы по питанию на сушке\n"
        "• 🏋️ Анализ тренировок\n\n"
        "Сначала заполни профиль — напиши /profile",
        reply_markup=MAIN_KEYBOARD
    )

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Заполним профиль!\n\n"
        "Напиши в одном сообщении через запятую:\n"
        "Имя, Вес(кг), Рост(см), Возраст, Пол(м/ж), Активность(низкая/средняя/высокая)\n\n"
        "Пример:\nДаша, 58, 165, 27, ж, средняя"
    )
    context.user_data["waiting_profile"] = True

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    if context.user_data.get("waiting_profile"):
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 6:
            activity_map = {"низкая": "low", "средняя": "medium", "высокая": "high"}
            gender_map = {"м": "male", "ж": "female"}
            user_profiles[user_id] = {
                "name": parts[0],
                "weight": parts[1],
                "height": parts[2],
                "age": parts[3],
                "gender": gender_map.get(parts[4].lower(), "female"),
                "activity": activity_map.get(parts[5].lower(), "medium"),
            }
            context.user_data["waiting_profile"] = False
            p = user_profiles[user_id]
            w = float(p["weight"])
            await update.message.reply_text(
                "Профиль сохранён! \n\n"
                "Имя: " + p["name"] + " | Вес: " + p["weight"] + " кг\n"
                "Норма белка: " + str(round(w*2)) + " г/день\n\n"
                "Теперь можешь отправить фото еды или данные Oura!",
                reply_markup=MAIN_KEYBOARD
            )
        else:
            await update.message.reply_text("Напиши через запятую: Имя, Вес, Рост, Возраст, Пол(м/ж), Активность")
        return

    if context.user_data.get("waiting_oura"):
        try:
            data = json.loads(text)
            items = data.get("data", [])
            if items:
                last = items[-1]
                oura_summary = {}
                if "score" in last: oura_summary["Readiness Score"] = str(last["score"]) + "/100"
                if "total_sleep_duration" in last: oura_summary["Сон"] = str(round(last["total_sleep_duration"]/3600, 1)) + " ч"
                if "average_hrv" in last: oura_summary["HRV"] = str(last["average_hrv"]) + " мс"
                if "average_heart_rate" in last: oura_summary["ЧСС ночью"] = str(last["average_heart_rate"]) + " уд/мин"
                if "active_calories" in last: oura_summary["Активные калории"] = str(last["active_calories"]) + " ккал"
                if "steps" in last: oura_summary["Шаги"] = str(last["steps"])
                if "efficiency" in last: oura_summary["Эффективность сна"] = str(last["efficiency"]) + "%"
                user_oura[user_id] = oura_summary
                context.user_data["waiting_oura"] = False
                msg = "Данные Oura сохранены!\n\n"
                for k, v in oura_summary.items():
                    msg += k + ": " + v + "\n"
                msg += "\nХочешь рекомендации на сегодня?"
                await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
            else:
                await update.message.reply_text("Не нашла данные в JSON. Попробуй ещё раз.")
        except:
            await update.message.reply_text("Не смогла разобрать JSON. Вставь данные из шортката Oura.")
        return

    if text == "📊 Мой профиль":
        await profile_cmd(update, context)
        return
    elif text == "💍 Ввести Oura":
        await update.message.reply_text("Вставь JSON данные из шортката Oura:")
        context.user_data["waiting_oura"] = True
        return
    elif text == "📈 Моя статистика":
        p = user_profiles.get(user_id, {})
        o = user_oura.get(user_id, {})
        if not p:
            await update.message.reply_text("Сначала заполни профиль — /profile")
            return
        w = float(p.get("weight", 0))
        h = float(p.get("height", 0))
        bmi = round(w/((h/100)**2), 1) if w and h else "?"
        protein = round(w*2) if w else "?"
        msg = "📊 Твоя статистика\n\n"
        msg += p.get("name","?") + " | " + str(p.get("weight","?")) + " кг | " + str(p.get("height","?")) + " см\n"
        msg += "ИМТ: " + str(bmi) + "\n"
        msg += "Норма белка: " + str(protein) + " г/день\n"
        if o:
            msg += "\nOura сегодня:\n"
            for k, v in o.items():
                msg += "  " + k + ": " + str(v) + "\n"
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
        return

    await update.message.chat.send_action("typing")
    try:
        reply = await ask_groq(get_profile_prompt(user_id), text)
        await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    caption = update.message.caption or "Оцени это фото. Если еда - дай КБЖУ таблицей. Если тело - оцени форму и дай советы для сушки."
    await update.message.chat.send_action("typing")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()
    img_base64 = base64.b64encode(img_bytes).decode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + GROQ_API_KEY, "Content-Type": "application/json"},
                json={
                    "model": "llama-4-scout-17b-16e-instruct",
                    "messages": [
                        {"role": "system", "content": get_profile_prompt(user_id)},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img_base64}},
                            {"type": "text", "text": caption}
                        ]}
                    ],
                    "max_tokens": 1000,
                }
            )
            data = resp.json()
            choices = data.get("choices", [])
            if choices and choices[0].get("message", {}).get("content"):
                reply = choices[0]["message"]["content"]
            else:
                reply = str(data)
        await update.message.reply_text(reply, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text("Ошибка при анализе фото: " + str(e))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("FitCoach bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
