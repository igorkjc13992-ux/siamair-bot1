import os
import re
import json
import logging
import base64
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

def get_last_row(sheet_name):
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet_name}!A:A"
    ).execute()
    values = result.get("values", [])
    return len(values)

def delete_last_row(sheet_name):
    service = get_sheets_service()
    last_row = get_last_row(sheet_name)
    if last_row <= 1:
        return False, "Нет строк для удаления"

    # Получаем данные последней строки для подтверждения
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet_name}!A{last_row}:E{last_row}"
    ).execute()
    values = result.get("values", [[]])
    last_data = values[0] if values else []

    # Получаем spreadsheet id для sheetId
    spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_id = None
    for sheet in spreadsheet["sheets"]:
        if sheet["properties"]["title"] == sheet_name:
            sheet_id = sheet["properties"]["sheetId"]
            break

    if sheet_id is None:
        return False, "Лист не найден"

    body = {
        "requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": last_row - 1,
                    "endIndex": last_row
                }
            }
        }]
    }
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body=body
    ).execute()
    return True, last_data

def append_to_sheet(sheet_name, row_data):
    service = get_sheets_service()
    body = {"values": [row_data]}
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet_name}!A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()

def parse_with_claude(text=None, image_data=None, image_type=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now().strftime("%d.%m.%Y")

    system_prompt = f"""Ты помощник для учёта расходов и доходов компании SiamAir (авиасимулятор Boeing 737 в Пхукете).
Участники: К и Ч (два партнёра).
Сегодня: {today}

Твоя задача — определить тип действия и извлечь данные. Верни ТОЛЬКО JSON без пояснений.

Возможные действия:
1. РАСХОД — кто-то потратил деньги
2. ПРИХОД — пришли деньги от клиента или другой источник  
3. УДАЛИТЬ — пользователь хочет удалить последнюю запись
4. НЕПОНЯТНО — не похоже ни на что из вышеперечисленного

Для РАСХОД верни:
{{"action": "расход", "date": "MM/DD/YYYY", "amount": число, "participant": "К" или "Ч", "category": "категория", "note": "примечание"}}

Для ПРИХОД верни:
{{"action": "приход", "date": "MM/DD/YYYY", "amount": число, "participant": "К" или "Ч", "note": "примечание"}}

Для УДАЛИТЬ верни:
{{"action": "удалить", "sheet": "Расход" или "Приход"}}
Если не указан лист — по умолчанию "Расход".

Для НЕПОНЯТНО верни:
{{"action": "непонятно"}}

Категории расходов:
Аренда помещения, Покупка оборудования, Ремонт оборудования, Ремонт помещения, Коммунальные расходы, PR, ФОТ, Еда, IT, Комиссия, Взаиморасчёты, Прочие расходы, телефон, интернет"""

    content = []

    if image_data:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_type,
                "data": image_data
            }
        })
        content.append({
            "type": "text",
            "text": f"Это фото чека или квитанции. Извлеки дату, сумму и определи категорию расхода. Участник: {text if text else 'неизвестен — оставь пустым'}"
        })
    else:
        content.append({"type": "text", "text": text})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": content}]
    )

    result_text = response.content[0].text.strip()
    result_text = re.sub(r'```json|```', '', result_text).strip()
    return json.loads(result_text)

async def process_message(msg, context, image_data=None, image_type=None):
    text = msg.text or msg.caption or ""

    try:
        parsed = parse_with_claude(text=text, image_data=image_data, image_type=image_type)
        logger.info(f"Parsed: {parsed}")
        action = parsed.get("action", "непонятно")

        if action == "расход":
            row = [parsed["date"], parsed["amount"], parsed["participant"], parsed["category"], parsed.get("note", "")]
            append_to_sheet("Расход", row)
            await msg.reply_text(
                f"✅ Расход записан:\n"
                f"📅 {parsed['date']}\n"
                f"💰 {parsed['amount']} бат\n"
                f"👤 {parsed['participant']}\n"
                f"📂 {parsed['category']}"
                + (f"\n📝 {parsed.get('note', '')}" if parsed.get('note') else "")
            )

        elif action == "приход":
            row = [parsed["date"], parsed["amount"], parsed["participant"], parsed.get("note", "")]
            append_to_sheet("Приход", row)
            await msg.reply_text(
                f"✅ Приход записан:\n"
                f"📅 {parsed['date']}\n"
                f"💰 {parsed['amount']} бат\n"
                f"👤 {parsed['participant']}\n"
                + (f"📝 {parsed.get('note', '')}" if parsed.get('note') else "")
            )

        elif action == "удалить":
            sheet = parsed.get("sheet", "Расход")
            success, data = delete_last_row(sheet)
            if success:
                await msg.reply_text(f"🗑 Удалена последняя строка из «{sheet}»:\n{' | '.join(str(x) for x in data)}")
            else:
                await msg.reply_text(f"❌ {data}")

        elif action == "непонятно":
            pass  # Тихо игнорируем

    except Exception as e:
        logger.error(f"Error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    # Обработка фото
    if msg.photo:
        photo = msg.photo[-1]  # Берём наибольшее разрешение
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image_data = base64.b64encode(file_bytes).decode("utf-8")
        await process_message(msg, context, image_data=image_data, image_type="image/jpeg")
        return

    if not msg.text or len(msg.text) < 3:
        return

    await process_message(msg, context)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if msg:
        await msg.reply_text(
            "👋 Бот учёта расходов SiamAir\n\n"
            "Пишите в свободной форме:\n"
            "• Расход: «02.04 500 К аренда»\n"
            "• Приход: «пришло 5000 от клиента К»\n"
            "• Удалить: «удали последнюю запись»\n"
            "• Чек: отправьте фото чека\n"
        )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
