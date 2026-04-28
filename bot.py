import os
import json
import base64
import httpx
import asyncio
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OURA_TOKEN = os.environ.get("OURA_TOKEN")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
MODEL = "claude-haiku-4-5-20251001"
MSK = ZoneInfo("Europe/Moscow")

DEFAULT_PROFILE = {
    "name": "Даня", "gender": "female", "weight": "55",
    "height": "167", "age": "31", "activity": "medium",
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
user_food_log = {}   # {user_id: [{date, meal, kcal, p, f, c}]}
registered_users = set()
media_groups = {}

def get_profile(uid): return user_profiles.get(uid, DEFAULT_PROFILE)
def get_meds(uid): return user_meds.get(uid, DEFAULT_MEDS)

def get_system(uid):
    p = get_profile(uid)
    o = user_oura.get(uid, {})
    m = get_meds(uid)
    s = user_supplements.get(uid, {})
    w, h, a = float(p["weight"]), float(p["height"]), float(p["age"])
    bmr = 655 + 9.6*w + 1.8*h - 4.7*a
    tdee = round(bmr * 1.55)
    deficit = tdee - 350
    protein = round(w * 2)
    prompt = "Ты персональный фитнес-коуч и нутрициолог в Telegram. Отвечаешь по-русски, конкретно, с цифрами.\n\n"
    prompt += "=== ПРОФИЛЬ ===\n"
    prompt += f"Имя: {p['name']} | Пол: женщина | Вес: {p['weight']} кг | Рост: {p['height']} см | Возраст: {p['age']} лет\n"
    prompt += f"Цель: {p['goal']}\nОграничения: {p.get('restrictions','минимум лактозы')}\n"
    prompt += f"Норма белка: {protein} г/день\nТDEE: {tdee} ккал | Цель (дефицит): {deficit} ккал/день\n"
    prompt += "\n=== ЛЕКАРСТВА И ДОБАВКИ ===\n"
    for name, time in m.items():
        prompt += f"• {name}: {time}\n"
    for name, info in s.items():
        prompt += f"• {name}: {info}\n"
    if o:
        prompt += "\n=== ДАННЫЕ OURA СЕГОДНЯ ===\n"
        for k, v in o.items():
            prompt += f"{k}: {v}\n"
    prompt += "\n=== ПРАВИЛА ===\n"
    prompt += "• При фото ЕДЫ — дай КБЖУ таблицей: калории / белки / жиры / углеводы. В конце добавь строку: КБЖУ: [калории]ккал / [белки]б / [жиры]ж / [углеводы]у\n"
    prompt += "• При описании ТРЕНИРОВКИ — оцени нагрузку, калории, дай советы\n"
    prompt += "• При фото БАДОВ — прочитай состав и скажи как принимать, совместимо ли с венлафаксином\n"
    prompt += "• Учитывай венлафаксин при рекомендациях\n"
    prompt += "• Давай конкретные советы, не общие фразы\n"
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
                r = await c.get(base + ep + f"?start_date={yesterday}&end_date={today}", headers=headers)
                data = r.json().get("data", [])
                if data: result[ep] = data[-1]
            except: pass
    return result

def parse_oura(raw):
    out = {}
    r, s, a = raw.get("daily_readiness",{}), raw.get("daily_sleep",{}), raw.get("daily_activity",{})
    if r.get("score"): out["Readiness"] = f"{r['score']}/100"
    if s.get("total_sleep_duration"): out["Сон"] = f"{round(s['total_sleep_duration']/3600,1)} ч"
    if s.get("efficiency"): out["Качество сна"] = f"{s['efficiency']}%"
    if s.get("deep_sleep_duration"): out["Глубокий сон"] = f"{round(s['deep_sleep_duration']/60)} мин"
    if s.get("average_hrv"): out["HRV"] = f"{s['average_hrv']} мс"
    if s.get("average_heart_rate"): out["ЧСС ночью"] = f"{s['average_heart_rate']} уд/мин"
    if a.get("active_calories"): out["Активные калории"] = f"{a['active_calories']} ккал"
    if a.get("steps"): out["Шаги"] = str(a["steps"])
    if a.get("score"): out["Активность"] = f"{a['score']}/100"
    return out

def claude(system, text):
    r = client.messages.create(model=MODEL, max_tokens=1000, system=system, messages=[{"role":"user","content":text}])
    return r.content[0].text

def claude_photo(system, images_b64, caption):
    content = []
    for img in images_b64:
        content.append({"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":img}})
    content.append({"type":"text","text":caption})
    r = client.messages.create(model=MODEL, max_tokens=1000, system=system, messages=[{"role":"user","content":content}])
    return r.content[0].text

def parse_kbzhu(text):
    match = re.search(r'КБЖУ:\s*(\d+)ккал\s*/\s*(\d+)б\s*/\s*(\d+)ж\s*/\s*(\d+)у', text)
    if match:
        return {"kcal": int(match.group(1)), "p": int(match.group(2)), "f": int(match.group(3)), "c": int(match.group(4))}
    return None

def log_food(uid, meal_name, kbzhu):
    if uid not in user_food_log:
        user_food_log[uid] = []
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    user_food_log[uid].append({"date": today, "meal": meal_name, **kbzhu})

def get_today_totals(uid):
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    logs = [e for e in user_food_log.get(uid, []) if e["date"] == today]
    return {
        "kcal": sum(e["kcal"] for e in logs),
        "p": sum(e["p"] for e in logs),
        "f": sum(e["f"] for e in logs),
        "c": sum(e["c"] for e in logs),
        "meals": len(logs)
    }

def get_period_totals(uid, days):
    cutoff = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d")
    logs = [e for e in user_food_log.get(uid, []) if e["date"] >= cutoff]
    if not logs: return None
    return {
        "kcal": sum(e["kcal"] for e in logs),
        "p": sum(e["p"] for e in logs),
        "f": sum(e["f"] for e in logs),
        "c": sum(e["c"] for e in logs),
        "days": len(set(e["date"] for e in logs)),
        "meals": len(logs)
    }

async def send_daily_report(context):
    p = get_profile(0)
    w = float(p["weight"])
    bmr = 655 + 9.6*w + 1.8*float(p["height"]) - 4.7*float(p["age"])
    tdee = round(bmr * 1.55)
    target = tdee - 350
    protein_norm = round(w * 2)
    for uid in registered_users:
        t = get_today_totals(uid)
        if t["meals"] == 0:
            msg = "📊 Дневной отчёт\n\nСегодня не было записей о еде. Не забывай отправлять фото блюд!"
        else:
            diff = t["kcal"] - target
            status = "✅ В норме!" if abs(diff) < 100 else ("⚠️ Перебор на " + str(diff) + " ккал" if diff > 0 else "⬇️ Дефицит " + str(abs(diff)) + " ккал")
            p_status = "✅" if t["p"] >= protein_norm * 0.8 else "⚠️ Маловато белка"
            o = user_oura.get(uid, {})
            oura_line = ""
            if o.get("Readiness"):
                oura_line = f"\n💍 Readiness: {o['Readiness']} | Шаги: {o.get('Шаги','—')}"
            msg = f"📊 Дневной отчёт — {datetime.now(MSK).strftime('%d.%m.%Y')}\n\n"
            msg += f"🍽️ Приёмов пищи: {t['meals']}\n"
            msg += f"🔥 Калории: {t['kcal']} / {target} ккал — {status}\n"
            msg += f"🥩 Белки: {t['p']} / {protein_norm} г — {p_status}\n"
            msg += f"🧈 Жиры: {t['f']} г | 🍞 Углеводы: {t['c']} г"
            msg += oura_line
            summary_text = (f"Дневной итог: {t['kcal']} ккал, белки {t['p']}г, жиры {t['f']}г, углеводы {t['c']}г. "
                          f"Цель {target} ккал. Дай 2-3 конкретные рекомендации на завтра для похудения и подтяжки тела.")
            try:
                advice = claude(get_system(uid), summary_text)
                msg += "\n\n💡 Рекомендации на завтра:\n" + advice
            except: pass
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except: pass

async def send_weekly_report(context):
    for uid in registered_users:
        t = get_period_totals(uid, 7)
        if not t:
            await context.bot.send_message(chat_id=uid, text="📅 Недельный отчёт\n\nНет данных за неделю.")
            continue
        p = get_profile(uid)
        w = float(p["weight"])
        bmr = 655 + 9.6*w + 1.8*float(p["height"]) - 4.7*float(p["age"])
        tdee = round(bmr * 1.55)
        target = tdee - 350
        avg_kcal = round(t["kcal"] / max(t["days"], 1))
        avg_p = round(t["p"] / max(t["days"], 1))
        protein_norm = round(w * 2)
        msg = f"📅 Недельный отчёт\n\n"
        msg += f"Дней с записями: {t['days']} | Приёмов пищи: {t['meals']}\n"
        msg += f"🔥 Среднее калорий/день: {avg_kcal} (цель: {target})\n"
        msg += f"🥩 Среднее белка/день: {avg_p} (норма: {protein_norm} г)\n"
        msg += f"Всего за неделю: {t['kcal']} ккал / {t['p']} б / {t['f']} ж / {t['c']} у\n"
        summary = (f"Итог недели: среднее {avg_kcal} ккал/день при цели {target} ккал. "
                  f"Белок {avg_p}г/день при норме {protein_norm}г. "
                  f"Дай анализ недели и 3 конкретные рекомендации на следующую неделю для похудения и подтяжки тела.")
        try:
            advice = claude(get_system(uid), summary)
            msg += "\n\n💡 Анализ и план на следующую неделю:\n" + advice
        except: pass
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except: pass

async def send_monthly_report(context):
    for uid in registered_users:
        t = get_period_totals(uid, 30)
        if not t:
            await context.bot.send_message(chat_id=uid, text="📆 Месячный отчёт\n\nНет данных за месяц.")
            continue
        p = get_profile(uid)
        w = float(p["weight"])
        bmr = 655 + 9.6*w + 1.8*float(p["height"]) - 4.7*float(p["age"])
        tdee = round(bmr * 1.55)
        target = tdee - 350
        avg_kcal = round(t["kcal"] / max(t["days"], 1))
        avg_p = round(t["p"] / max(t["days"], 1))
        protein_norm = round(w * 2)
        msg = f"📆 Месячный отчёт\n\n"
        msg += f"Дней с записями: {t['days']} | Всего приёмов: {t['meals']}\n"
        msg += f"🔥 Среднее калорий/день: {avg_kcal} (цель: {target})\n"
        msg += f"🥩 Среднее белка/день: {avg_p} (норма: {protein_norm} г)\n"
        msg += f"Всего за месяц: {t['kcal']} ккал / {t['p']} б / {t['f']} ж / {t['c']} у\n"
        summary = (f"Итог месяца: среднее {avg_kcal} ккал/день при цели {target} ккал. "
                  f"Белок {avg_p}г/день при норме {protein_norm}г. {t['days']} дней с записями из 30. "
                  f"Дай развёрнутый анализ месяца и план на следующий месяц для достижения цели похудения и подтяжки тела.")
        try:
            advice = claude(get_system(uid), summary)
            msg += "\n\n💡 Анализ месяца и план:\n" + advice
        except: pass
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except: pass

KB = ReplyKeyboardMarkup([
    [KeyboardButton("💍 Синхр. Oura"), KeyboardButton("🌅 План на день")],
    [KeyboardButton("🥗 Что поесть?"), KeyboardButton("💊 Мои добавки")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("💪 Совет по тренировке")],
    [KeyboardButton("📈 Сегодня съела")],
], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    await update.message.reply_text(
        "Привет, Даня! 💪\n\n"
        "Я твой FitCoach на базе Claude AI.\n\n"
        "• 📸 Фото еды (1-3 штуки) — КБЖУ + учёт в дневнике\n"
        "• 📸 Фото добавки — состав и как принимать\n"
        "• 🏋️ Опиши тренировку — разберу нагрузку\n"
        "• 💍 Oura — автосинхронизация\n\n"
        "📊 Отчёты приходят автоматически:\n"
        "• Каждый день в 23:00 МСК\n"
        "• По воскресеньям в 22:00 МСК\n"
        "• 1-го числа каждого месяца",
        reply_markup=KB
    )

async def oura_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    if not OURA_TOKEN:
        await update.message.reply_text("Токен Oura не настроен.")
        return
    await update.message.reply_text("⏳ Загружаю данные...")
    try:
        raw = await fetch_oura()
        summary = parse_oura(raw)
        user_oura[uid] = summary
        score = int(raw.get("daily_readiness", {}).get("score", 0))
        icon = "🟢" if score >= 85 else "🟡" if score >= 70 else "🔴"
        msg = icon + " Oura синхронизирована!\n\n"
        for k, v in summary.items():
            msg += k + ": " + v + "\n"
        msg += "\nНажми 🌅 План на день!"
        await update.message.reply_text(msg, reply_markup=KB)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

async def process_media_group(context, chat_id, uid, group_id, caption):
    await asyncio.sleep(1.5)
    group = media_groups.pop(group_id, [])
    if not group: return
    sys = get_system(uid)
    prompt = caption if caption else "Это приём пищи. Проанализируй все фото и дай суммарное КБЖУ таблицей: калории / белки / жиры / углеводы. В конце добавь строку: КБЖУ: [калории]ккал / [белки]б / [жиры]ж / [углеводы]у"
    try:
        reply = claude_photo(sys, group, prompt)
        kbzhu = parse_kbzhu(reply)
        if kbzhu:
            log_food(uid, caption or "Приём пищи", kbzhu)
            reply += "\n\n✅ Добавлено в дневник питания"
        await context.bot.send_message(chat_id=chat_id, text=reply, reply_markup=KB)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text="Ошибка: " + str(e))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    caption = update.message.caption or ""
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(img_bytes).decode("utf-8")
    group_id = update.message.media_group_id
    if group_id:
        if group_id not in media_groups:
            media_groups[group_id] = []
            asyncio.create_task(process_media_group(context, update.message.chat_id, uid, group_id, caption))
        media_groups[group_id].append(img_b64)
        return
    await update.message.chat.send_action("typing")
    prompt = caption if caption else "Определи что на фото. Если ЕДА — дай КБЖУ таблицей и в конце добавь строку: КБЖУ: [калории]ккал / [белки]б / [жиры]ж / [углеводы]у. Если БАД/ДОБАВКА — прочитай состав и скажи как принимать. Если ТРЕНИРОВКА — оцени нагрузку."
    sys = get_system(uid)
    try:
        reply = claude_photo(sys, [img_b64], prompt)
        kbzhu = parse_kbzhu(reply)
        if kbzhu and not caption:
            log_food(uid, "Приём пищи", kbzhu)
            reply += "\n\n✅ Добавлено в дневник"
        await update.message.reply_text(reply, reply_markup=KB)
    except Exception as e:
        await update.message.reply_text("Ошибка: " + str(e))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    registered_users.add(uid)
    text = update.message.text or ""
    sys = get_system(uid)

    if text == "💍 Синхр. Oura":
        await oura_sync(update, context); return
    elif text == "🌅 План на день":
        text = "Составь мой план на сегодня: когда принять добавки/лекарства, что поесть (с КБЖУ), стоит ли тренироваться (учти Oura). Конкретно с цифрами."
    elif text == "🥗 Что поесть?":
        text = "Предложи 3 варианта обеда/ужина — учитывай цель похудения, минимум лактозы, норму белка. Дай КБЖУ для каждого."
    elif text == "💊 Мои добавки":
        m = get_meds(uid); s = user_supplements.get(uid, {})
        msg = "💊 Лекарства и добавки:\n\n"
        for name, time in m.items(): msg += f"• {name}: {time}\n"
        if s:
            msg += "\n🌿 Дополнительные бады:\n"
            for name, info in s.items(): msg += f"• {name}: {info}\n"
        msg += "\nДобавить бад: ➕ Название: время"
        await update.message.reply_text(msg, reply_markup=KB); return
    elif text == "📊 Статистика":
        p = get_profile(uid); o = user_oura.get(uid, {})
        w, h, a = float(p["weight"]), float(p["height"]), float(p["age"])
        bmi = round(w/((h/100)**2), 1)
        bmr = 655 + 9.6*w + 1.8*h - 4.7*a; tdee = round(bmr*1.55)
        msg = f"📊 Статистика\n\n{p['name']} | {p['weight']} кг | {p['height']} см | {p['age']} лет\nИМТ: {bmi}\nНорма белка: {round(w*2)} г/день\nЦелевые калории: {tdee-350} ккал/день\n"
        t = get_today_totals(uid)
        if t["meals"] > 0:
            msg += f"\n📅 Сегодня: {t['kcal']} ккал / {t['p']}б / {t['f']}ж / {t['c']}у"
        if o:
            msg += "\n\n💍 Oura:\n"
            for k, v in o.items(): msg += f"  {k}: {v}\n"
        await update.message.reply_text(msg, reply_markup=KB); return
    elif text == "💪 Совет по тренировке":
        o = user_oura.get(uid, {})
        text = "На основе моих данных Oura — стоит ли тренироваться сегодня? Если да — дай конкретный план тренировки для похудения и подтяжки живота." if o else "Дай план тренировки для похудения и подтяжки живота. Средний уровень. Конкретные упражнения с подходами."
    elif text == "📈 Сегодня съела":
        t = get_today_totals(uid)
        p = get_profile(uid); w = float(p["weight"])
        bmr = 655 + 9.6*w + 1.8*float(p["height"]) - 4.7*float(p["age"])
        target = round(bmr*1.55) - 350; protein_norm = round(w*2)
        if t["meals"] == 0:
            await update.message.reply_text("Сегодня нет записей о еде. Отправь фото блюда!", reply_markup=KB); return
        diff = t["kcal"] - target
        status = "✅" if abs(diff) < 100 else ("⚠️ +" + str(diff) if diff > 0 else "⬇️ " + str(abs(diff)))
        msg = f"📈 Сегодня ({t['meals']} приёмов):\n\n🔥 {t['kcal']} / {target} ккал {status}\n🥩 Белки: {t['p']} / {protein_norm} г\n🧈 Жиры: {t['f']} г\n🍞 Углеводы: {t['c']} г"
        await update.message.reply_text(msg, reply_markup=KB); return
    elif text.startswith("➕ ") and ":" in text:
        parts = text[2:].split(":", 1)
        if len(parts) == 2:
            if uid not in user_supplements: user_supplements[uid] = {}
            user_supplements[uid][parts[0].strip()] = parts[1].strip()
            await update.message.reply_text("✅ Добавлено: " + parts[0].strip(), reply_markup=KB); return

    await update.message.chat.send_action("typing")
    try:
        reply = claude(sys, text)
        await update.message.reply_text(reply, reply_markup=KB)
    except Exception as e:
        await update.message.reply_text("Ошибка Claude: " + str(e))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    jq = app.job_queue

    # Daily report 23:00 MSK = 20:00 UTC
    jq.run_daily(send_daily_report, time=datetime.strptime("20:00", "%H:%M").time().replace(tzinfo=MSK))

    # Weekly report Sunday 22:00 MSK = 19:00 UTC
    from telegram.ext import filters as f
    jq.run_daily(send_weekly_report, time=datetime.strptime("19:00", "%H:%M").time().replace(tzinfo=MSK), days=(6,))

    # Monthly report — run daily, check if 1st
    async def monthly_check(context):
        if datetime.now(MSK).day == 1:
            await send_monthly_report(context)
    jq.run_daily(monthly_check, time=datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=MSK))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("FitCoach bot started with scheduled reports!")
    app.run_polling()

if __name__ == "__main__":
    main()
