import os
import re
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheets_service():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)

def parse_with_claude(text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now().strftime("%d.%m.%Y")

    prompt = f"""Ты помощник для учёта расходов. Извлеки данные из сообщения и верни ТОЛЬКО JSON без пояснений.

Сегодня: {today}

Участники: К или Ч (регистр не важен)

Сообщение: "{text}"

Верни JSON:
{{
  "date": "MM/DD/YYYY",
  "amount": число,
  "participant": "К" или "Ч",
  "category": "категория",
  "note": "примечание или пустая строка"
}}

Категории (выбери подходящую):
- Аренда помещения
- Покупка оборудования
- Ремонт оборудования
- Ремонт помещения
- Коммунальные расходы
- PR
- ФОТ
- Еда
- IT
- Комиссия
- Взаиморасчёты
- Прочие расходы
- телефон
- интернет

Если не можешь распознать данные — верни: {{"error": "не удалось распознать"}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )

    result_text = response.content[0].text.strip()
    result_text = re.sub(r'```json|```', '', result_text).strip()
    return json.loads(result_text)

def append_to_sheet(data):
    service = get_sheets_service()
    values = [[data["date"], data["amount"], data["participant"], data["category"], data["note"]]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Расход!A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()

async def handle_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"UPDATE RECEIVED: {update}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"MESSAGE RECEIVED: {update}")
    if not update.message:
        logger.info("No message in update")
        return
    if not update.message.text:
        logger.info("No text in message")
        return
    text = update.message.text
    logger.info(f"Text: {text}")
    if len(text) < 5:
        return

    try:
        parsed = parse_with_claude(text)
        logger.info(f"Parsed: {parsed}")

        if "error" in parsed:
            return

        append_to_sheet(parsed)

        await update.message.reply_text(
            f"✅ Записано в таблицу:\n"
            f"📅 {parsed['date']}\n"
            f"💰 {parsed['amount']} бат\n"
            f"👤 {parsed['participant']}\n"
            f"📂 {parsed['category']}"
            + (f"\n📝 {parsed['note']}" if parsed['note'] else "")
        )

    except Exception as e:
        logger.error(f"Error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает! Пишите расходы.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.add_handler(MessageHandler(filters.ALL, handle_any_update))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
