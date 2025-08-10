import os
import sys
import re
import json
import time
import smtplib
import logging
import requests
from email.mime.text import MIMEText
from flask import Flask, request, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ─── ENV ─────────────────────────────────────────────────────────────
EMAIL_ADDRESS          = os.getenv('EMAIL_ADDRESS')                  # Gmail
EMAIL_PASSWORD         = os.getenv('EMAIL_PASSWORD')                 # Пароль приложения
SPREADSHEET_ID         = os.getenv('SPREADSHEET_ID')
SHEET_NAME             = os.getenv('SHEET_NAME')                     # например: "Тест"
SHEET_ID               = int(os.getenv('SHEET_ID', '0'))             # числовой ID листа
GOOGLE_CREDENTIALS     = os.getenv('GOOGLE_CREDENTIALS_FILE')        # ВЕСЬ JSON-ключ как строка
TELEGRAM_BOT_TOKEN     = os.getenv('TELEGRAM_BOT_TOKEN')             # токен бота
WEBHOOK_TOKEN          = os.getenv('WEBHOOK_TOKEN')                  # секрет ?token=...
# --------------------------------------------------------------------

# Логи в stdout
sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)

app = Flask(__name__)

# ─── Google Sheets ──────────────────────────────────────────────────
def build_sheets_service():
    if not GOOGLE_CREDENTIALS:
        raise RuntimeError("GOOGLE_CREDENTIALS_FILE is empty")
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
    )
    return build('sheets', 'v4', credentials=creds)

# ─── Telegram ───────────────────────────────────────────────────────
def tg_send(chat_id: int, text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logging.info(f"⚠️ Ошибка отправки в Telegram: {e}")

# ─── SMTP ошибки: классификация ─────────────────────────────────────
def classify_error(error: Exception) -> str:
    """
    Если тип ошибки SMTPRecipientsRefused и код:
      - 5.5.2 → 'пустая строка'
      - 5.1.3 → 'неправильный адрес'
    Иначе — вернуть полный текст ошибки.
    """
    s = str(error)
    is_recipients_refused = isinstance(error, smtplib.SMTPRecipientsRefused) or "SMTPRecipientsRefused" in s
    if is_recipients_refused:
        m = re.search(r'5\.\d+\.\d+', s)
        if m:
            code = m.group()
            if code == "5.5.2":
                return "пустая строка"
            if code == "5.1.3":
                return "неправильный адрес"
    return s

# ─── Email ─────────────────────────────────────────────────────────
def send_email(to_email: str, subject: str, html_content: str):
    logging.info(f"📧 Отправка письма на: {to_email}")
    msg = MIMEText(html_content or "", 'html', 'utf-8')  # явная кодировка
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

# ─── Удаление первой строки ────────────────────────────────────────
def delete_first_row(service):
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            'requests': [{
                'deleteDimension': {
                    'range': {
                        'sheetId': SHEET_ID,
                        'dimension': 'ROWS',
                        'startIndex': 0,   # первая строка (A1)
                        'endIndex': 1
                    }
                }
            }]
        }
    ).execute()
    logging.info("♻️ Строка №1 успешно удалена.\n")

# ─── Одна итерация сценария ────────────────────────────────────────
def process_once_and_report(chat_id: int):
    service = build_sheets_service()
    sheet = service.spreadsheets()

    # Читаем A1:D1
    rng = f"{SHEET_NAME}!A1:D1"
    res = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=rng).execute()
    values = res.get('values', [])

    # Если очередь пуста — сообщаем И все равно удаляем строку 1
    if not values or not values[0] or all(cell == "" for cell in values[0]):
        tg_send(chat_id, "ℹ️ Очередь пуста: в таблице нет строк для отправки.")
        delete_first_row(service)
        return

    row = values[0]
    email   = row[0] if len(row) > 0 else ""
    subject = row[1] if len(row) > 1 else ""
    html    = row[2] if len(row) > 2 else ""
    delay   = row[3] if len(row) > 3 else "0"

    # Задержка перед отправкой
    try:
        delay_seconds = int(str(delay).strip())
    except:
        delay_seconds = 0
    if delay_seconds > 0:
        logging.info(f"⏳ Ожидание задержки в {delay_seconds} секунд перед отправкой.")
        time.sleep(delay_seconds)

    # Отправка письма
    success, err_text = send_email(email, subject, html)

    # Удаляем первую строку всегда
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

# ─── HTTP endpoints ─────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify(ok=True)

@app.post("/webhook")
def webhook():
    # простой секрет через query: /webhook?token=XXXX
    token = request.args.get("token")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify(ok=False, error="Forbidden"), 403

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    logging.info(f"🔔 Триггер из TG: chat_id={chat_id}")

    try:
        process_once_and_report(chat_id)
    except Exception as e:
        logging.info(f"🚨 Общая ошибка обработки: {e}")
        tg_send(chat_id, f"❌ Общая ошибка обработки: {e}")

    return jsonify(ok=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)