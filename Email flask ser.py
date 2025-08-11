import os
import sys
import re
import json
import time
import smtplib
import logging
import requests
from datetime import datetime
from email.mime.text import MIMEText
from flask import Flask, request, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── ENV ────────────────────────────────────────────────────────────────────────
EMAIL_ADDRESS = os.getenv('EMAIL_ADDRESS')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
SHEET_NAME = os.getenv('SHEET_NAME')
SHEET_ID = int(os.getenv('SHEET_ID', '0'))
GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE')  # JSON строкой
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN')

# ── Логи ───────────────────────────────────────────────────────────────────────
sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)

app = Flask(__name__)

# ── Google Sheets ──────────────────────────────────────────────────────────────
def build_sheets_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_FILE)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
    )
    return build('sheets', 'v4', credentials=creds)

# ── Telegram ───────────────────────────────────────────────────────────────────
def tg_send(chat_id: int, text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if resp.status_code != 200:
            logging.info(f"⚠️ Telegram send failed: {resp.text}")
    except Exception as e:
        logging.info(f"⚠️ Ошибка при отправке в Telegram: {e}")

# ── SMTP: классификация ошибок ────────────────────────────────────────────────
def classify_error(error: Exception) -> str:
    s = str(error)
    m = re.search(r'5\.\d+\.\d+', s)
    if m:
        code = m.group()
        if code == "5.5.2":
            return "пустая строка"
        if code == "5.1.3":
            return "неправильный адрес"
    return s  # всё остальное — полный текст

# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(to_email: str, subject: str, html_content: str):
    logging.info(f"📧 Отправка письма на: {to_email}")
    msg = MIMEText(html_content or "", 'html')
    msg['Subject'] = subject or ""
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = to_email or ""
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)
        logging.info("✅ Письмо успешно отправлено.")
        return True, None
    except Exception as e:
        logging.info(f"❌ Ошибка при отправке письма: {e}")
        return False, classify_error(e)

# ── Удаление первой строки ────────────────────────────────────────────────────
def delete_first_row(service):
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            'requests': [{
                'deleteDimension': {
                    'range': {
                        'sheetId': SHEET_ID,
                        'dimension': 'ROWS',
                        'startIndex': 1,   # удаляем A1
                        'endIndex': 2
                    }
                }
            }]
        }
    ).execute()
    logging.info("♻️ Строка №1 успешно удалена.\n")

# ── Одна итерация сценария ────────────────────────────────────────────────────
def process_once_and_report(chat_id: int):
    service = build_sheets_service()
    sheet = service.spreadsheets()

    # Читаем A1:D1 (как ты просил)
    rng = f"{SHEET_NAME}!A2:D2"
    res = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=rng).execute()
    values = res.get('values', [])

    # Если пусто — сообщаем и всё равно удаляем первую строку
    if not values or not values[0] or all(cell == "" for cell in values[0]):
        tg_send(chat_id, "ℹ️ Очередь пуста: в таблице нет строк для отправки.")
        delete_first_row(service)
        return

    row = values[0]
    email   = row[0] if len(row) > 0 else ""
    subject = row[1] if len(row) > 1 else ""
    html    = row[2] if len(row) > 2 else ""
    delay   = row[3] if len(row) > 3 else "0"

    # Задержка (если указана)
    try:
        delay_seconds = int(str(delay).strip())
    except:
        delay_seconds = 0
    if delay_seconds > 0:
        logging.info(f"⏳ Ожидание задержки в {delay_seconds} секунд перед отправкой.")
        time.sleep(delay_seconds)

    # Письмо
    success, err_text = send_email(email, subject, html)

    # Удаляем первую строку в любом случае
    delete_first_row(service)

    # Отчёт в Telegram
    if success:
        report = (
            f"✉️ Письмо отправлено с аккаунта: {EMAIL_ADDRESS}\n"
            f"На адрес: {email}\n"
            f"Была задержка: {delay_seconds} секунд\n"
            f"Результат: ✅ Успешно отправлено!\n"
            f"♻️Строка успешно удалена."
        )
    else:
        report = (
            f"✉️ Письмо отправлено с аккаунта: {EMAIL_ADDRESS}\n"
            f"На адрес: {email}\n"
            f"Была задержка: {delay_seconds} секунд\n"
            f"Результат: ❌ Ошибка: {err_text}\n"
            f"♻️Строка успешно удалена."
        )
    tg_send(chat_id, report)

# ── HTTP endpoints ─────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify(ok=True, time=datetime.now().strftime("%H:%M:%S"))

@app.post("/webhook")
def webhook():
    # простой секрет через query: /webhook?token=XXXX
    token = request.args.get("token")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify(ok=False, error="Forbidden"), 403

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify(ok=True)  # игнор иных типов апдейтов

    chat_id = message["chat"]["id"]
    logging.info(f"🔔 Триггер из Telegram: chat_id={chat_id}")

    try:
        process_once_and_report(chat_id)
    except Exception as e:
        logging.info(f"🚨 Общая ошибка обработки: {e}")
        tg_send(chat_id, f"❌ Общая ошибка обработки: {e}")

    return jsonify(ok=True)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

