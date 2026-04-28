import os
import json
import base64
import httpx
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OURA_TOKEN = os.environ.get("OURA_TOKEN")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
MODEL = "claude-haiku-4-5-20251001"

DEFAULT_PROFILE = {
    "name": "Даня",
    "gender": "female",
    "weight": "55",
    "height": "167",
    "age": "31",
    "activity": "medium",
    "goal": "похудеть, подтянуть мышцы, убрать живот",
    "restrictions": "минимум лактозы",
}

DEFAULT_MEDS = {
    "Венлафаксин 37.5": "утром и вечером с едой",
    "Хлорофилл со спирулиной (Medgratr)": "утром натощак",
}

user_profiles = {}
user_oura = {}
user_meds = {}
user_supplements = {}

def get_profile(user_id):
    return user_profiles.get(user_id, DEFAULT_PROFILE)

def get_meds(user_id):
    return user_meds.get(user_id, DEFAULT_MEDS)

def get_system(user_id):
    p = get_profile(user_id)
    o = user_oura.get(user_id, {})
    m = get_meds(user_id)
    s = user_supplements.get(user_id, {})

    w = float(p.get("weight", 55))
    h = float(p.get("height", 167))
    a = float(p.get("age", 31))
    bmr = 655 + 9.6*w + 1.8*h - 4.7*a
    tdee = round(bmr * 1.55)
    deficit = tdee - 350
    protein = round(w * 2)

    prompt = "Ты персональный фитнес-коуч и нутрициолог в Telegram. Отвечаешь по-русски, конкретно, с цифрами.\n\n"
    prompt += "=== ПРОФИЛЬ ===\n"
    prompt += "Имя: " + p["name"] + " | Пол: женщина\n"
    prompt += "Вес: " + p["weight"] + " кг | Рост: " + p["height"] + " см | Возраст: " + p["age"] + " лет\n"
    prompt += "Цель: " + p["goal"] + "\n"
    prompt += "Ограничения: " + p.get("restrictions", "минимум лактозы") + "\n"
    prompt += "Норма белка: " + str(protein) + " г/день\n"
    prompt += "TDEE: " + str(tdee) + " ккал | Цель с дефицитом: " + str(deficit) + " ккал/день\n"

    prompt += "\n=== ЛЕКАРСТВА И ДОБАВКИ ===\n"
    for name, time in m.items():
        prompt += "• " + name + ": " + time + "\n"
    if s:
        for name, info in s.items():
            prompt += "• " + name + ": " + info + "\n"

    if o:
        prompt += "\n=== ДАННЫЕ OURA СЕГОДНЯ ===\n"
        for k, v in o.items():
            prompt += k + ": " + str(v) + "\n"

    prompt += "\n=== ПРАВИЛА ===\n"
    prompt += "• При фото ЕДЫ — дай КБЖУ таблицей: калории / белки / жиры / углеводы\n"
    prompt += "• При описании ТРЕНИРОВКИ — оцени нагрузку, калории, дай советы\n"
    prompt += "• При фото БАДОВ/ДОБАВОК — прочитай состав и скажи когда и как лучше принимать\n"
    prompt += "• Учитывай венлафаксин при рекомендациях по питанию и тренировкам\n"
    prompt += "• Давай конкретные советы, не общие фразы\n"
    prompt += "• Если нужна доп. информация — задай ОДИН уточняющий вопрос\n"
    return prompt

async def fetch_oura():
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    base = "https://api.ouraring.com/v2/usercollection/"
    headers = {"Authorization": "Bearer " + OURA_TOKEN}
    result = {}
    async with httpx.AsyncClient(timeout=15) as c:
        for ep in ["daily_readiness", "daily_sleep", "daily_activity"]:
            try:
                r = await c.get(base + ep + "?start_date=" + yesterday + "&end_date=" + today, headers=headers)
                data = r.json().get("data", [])
                if data:
                    result[ep] = data[-1]
            except:
                pass
    return result

def parse_oura(raw):
    out = {}
    r = raw.get("daily_readiness", {})
    s = raw.get("daily_sleep", {})
    a = raw.get("daily_activity", {})
    if r.get("score"): out["Readiness"] = str(r["score"]) + "/100"
    if s.get("total_sleep_duration"): out["Сон"] = str(round(s["total_sleep_duration"]/3600, 1)) + " ч"
    if s.get("efficiency"): out["Качество сна"] = str(s["efficiency"]) + "%"
    if s.get("deep_sleep_duration"): out["Глубокий сон"] = str(round(s["deep_sleep_duration"]/60)) + " мин"
    if s.get("average_hrv"): out["HRV"] = str(s["average_hrv"]) + " мс"
    if s.get("average_heart_rate"): out["ЧСС ночью"] = str(s["average_heart_rate"]) + " уд/мин"
    if a.get("active_calories"): out["Активные калории"] = str(a["active_calories"]) + " ккал"
    if a.get("steps"): out["Шаги"] = str(a["steps"])
    if a.get("score"): out["Активность"] = str(a["score"]) + "/100"
    return out

def claude(system, text):
    r = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": text}]
    )
    return r.content[0].text

def claude_photo(system, img_b64, caption):
    r = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
            {"type": "text", "text": caption}
        ]}]
    )
    return r.content[0].text

KB = ReplyKeyboardMarkup([
    [KeyboardButton("💍 Синхр. Oura"), KeyboardButton("🌅 План на день")],
    [KeyboardButton("🥗 Что поесть?"), KeyboardButton("💊 Мои добавки")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("💪 Совет по тренировке")],
], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет, Даня! 💪\n\n"
        "Я твой личный FitCoach. Знаю твои данные, добавки и слежу за Oura.\n\n"
        "Что умею прямо сейчас:\n"
        "• 📸 Отправь фото еды — посчитаю КБЖУ\n"
        "• 📸 Фото добавки/бада — расскажу как принимать\n"
        "• 🏋️ Напиши тренировку — разберу нагрузку\n"
        "• 💍 Синхронизирую Oura одной кнопкой\n"
        "• 🌅 Составлю план на день\n\n"
        "Просто напиши или отправь фото!",
        reply_markup=KB
    )

async def oura_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not OURA_TOKEN:
        await update.message.reply_text("Токен Oura не настроен.")
        return
    await update.message.reply_text("⏳ Загружаю данные...")
    try:
        raw = await fetch_oura()
        summary = parse_oura(raw)
        user_oura[user_id] = summary
        score = int(raw.get("daily_readiness", {}).get("score", 0))
        icon = "🟢" if score >= 85 else "🟡" if score >= 70 else "🔴"
        msg = icon + " Oura синхронизирована!\n\n"
        for k, v in summary.items():
            msg += k + ": " + v + "\n"
        msg += "\nНажми 🌅 План на день чтобы получить рекомендации!"
        await update.message.reply_text(msg, reply_markup=KB)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    sys = get_system(user_id)

    if text == "💍 Синхр. Oura":
        await oura_sync(update, context)
        return
    elif text == "🌅 План на день":
        text = "Составь мой план на сегодня: когда принять добавки и лекарства, что поесть на завтрак/обед/ужин (учитывай дефицит калорий и цель), стоит ли тренироваться сегодня (учитывай данные Oura если есть). Будь конкретной с цифрами."
    elif text == "🥗 Что поесть?":
        text = "Предложи мне 3 варианта обеда или ужина — учитывай мою цель (похудение, убрать живот), ограничение по лактозе, норму белка. Дай КБЖУ для каждого варианта."
    elif text == "💊 Мои добавки":
        m = get_meds(user_id)
        s = user_supplements.get(user_id, {})
        msg = "💊 Твои лекарства и добавки:\n\n"
        for name, time in m.items():
            msg += "• " + name + ": " + time + "\n"
        if s:
            msg += "\n🌿 Дополнительные бады:\n"
            for name, info in s.items():
                msg += "• " + name + ": " + info + "\n"
        msg += "\nЧтобы добавить бад — отправь его фото или напиши:\n➕ Название: время приёма"
        await update.message.reply_text(msg, reply_markup=KB)
        return
    elif text == "📊 Статистика":
        p = get_profile(user_id)
        o = user_oura.get(user_id, {})
        w = float(p["weight"])
        h = float(p["height"])
        a = float(p["age"])
        bmi = round(w/((h/100)**2), 1)
        bmr = 655 + 9.6*w + 1.8*h - 4.7*a
        tdee = round(bmr * 1.55)
        msg = "📊 Твои показатели\n\n"
        msg += p["name"] + " | " + p["weight"] + " кг | " + p["height"] + " см | " + p["age"] + " лет\n"
        msg += "ИМТ: " + str(bmi) + "\n"
        msg += "Норма белка: " + str(round(w*2)) + " г/день\n"
        msg += "Калорий для похудения: " + str(tdee-350) + " ккал/день\n"
        if o:
            msg += "\n💍 Oura сегодня:\n"
            for k, v in o.items():
                msg += "  " + k + ": " + str(v) + "\n"
        await update.message.reply_text(msg, reply_markup=KB)
        return
    elif text == "💪 Совет по тренировке":
        o = user_oura.get(user_id, {})
        if o:
            text = "На основе моих данных Oura — стоит ли мне сегодня тренироваться? Если да — какой тип тренировки подойдёт (учитывай цель убрать живот и подтянуть тело)? Дай конкретный план."
        else:
            text = "Составь план тренировки на сегодня для похудения и подтяжки тела, упор на живот. Средний уровень нагрузки. Дай конкретные упражнения с подходами и повторениями."
    elif text.startswith("➕ ") and ":" in text:
        parts = text[2:].split(":", 1)
        if len(parts) == 2:
            if user_id not in user_supplements:
                user_supplements[user_id] = {}
            user_supplements[user_id][parts[0].strip()] = parts[1].strip()
            await update.message.reply_text("✅ Добавлено: " + parts[0].strip(), reply_markup=KB)
            return

    await update.message.chat.send_action("typing")
    try:
        reply = claude(sys, text)
        await update.message.reply_text(reply, reply_markup=KB)
    except Exception as e:
        await update.message.reply_text("Ошибка Claude: " + str(e))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    caption = update.message.caption or ""
    await update.message.chat.send_action("typing")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(img_bytes).decode("utf-8")

    if not caption:
        prompt = "Определи что на фото. Если ЕДА — дай КБЖУ таблицей (калории / белки / жиры / углеводы) и скажи вписывается ли это в мою цель похудения. Если ДОБАВКА или БАД — прочитай состав и скажи когда и как лучше принимать, совместимо ли с венлафаксином и хлорофиллом. Если ТРЕНИРОВКА/УПРАЖНЕНИЯ — оцени нагрузку и дай советы."
    else:
        prompt = caption

    sys = get_system(user_id)
    try:
        reply = claude_photo(sys, img_b64, prompt)
        if not caption:
            parts = reply.split("\n", 1)
            if len(parts[0]) < 50:
                parts[0] += " — добавить в мой список? Напиши: ➕ Название: время приёма"
        await update.message.reply_text(reply, reply_markup=KB)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("FitCoach bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
