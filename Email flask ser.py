import os
import sys
import re
import json
import time
import smtplib
import logging
import requests
import threading
from email.mime.text import MIMEText
from flask import Flask, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ─── Конфиг из ENV ─────────────────────────────────────────────────────────────
EMAIL_ADDRESS          = os.getenv('EMAIL_ADDRESS')                  # Gmail
EMAIL_PASSWORD         = os.getenv('EMAIL_PASSWORD')                 # Пароль приложения
SPREADSHEET_ID         = os.getenv('SPREADSHEET_ID')
SHEET_NAME             = os.getenv('SHEET_NAME')                     # напр. "Тест"
SHEET_ID               = int(os.getenv('SHEET_ID', '0'))             # числовой ID листа
GOOGLE_CREDENTIALS     = os.getenv('GOOGLE_CREDENTIALS_FILE')        # ВЕСЬ JSON-ключ как строка
TELEGRAM_BOT_TOKEN     = os.getenv('TELEGRAM_BOT_TOKEN')             # токен бота
# -----------------------------------------------------------------------------

# Логи в stdout
sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)],
)

app = Flask(__name__)

# ─── Google Sheets ─────────────────────────────────────────────────────────────
def build_sheets_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
    )
    return build('sheets', 'v4', credentials=creds)

# ─── Telegram ──────────────────────────────────────────────────────────────────
TG_API = lambda m: f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{m}"

def tg_send(chat_id: int, text: str):
    try:
        requests.post(TG_API("sendMessage"),
                      data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                      timeout=10)
    except Exception as e:
        logging.info(f"⚠️ Ошибка отправки в Telegram: {e}")

def tg_delete_webhook():
    try:
        r = requests.post(TG_API("deleteWebhook"), timeout=10)
        logging.info(f"🔌 deleteWebhook -> {r.status_code}")
    except Exception as e:
        logging.info(f"⚠️ deleteWebhook error: {e}")

def tg_get_updates(offset=None, timeout=50):
    try:
        params = {"timeout": timeout, "allowed_updates": '["message","edited_message"]'}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(TG_API("getUpdates"), params=params, timeout=timeout+10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        logging.info(f"⚠️ Ошибка getUpdates: {e}")
        return []

def tg_drain_pending():
    """Сбрасываем всю старую очередь апдейтов при старте (обрабатываем только новые)."""
    logging.info("🧹 Сбрасываю старые сообщения Telegram…")
    max_id = None
    while True:
        updates = tg_get_updates(timeout=0)
        if not updates:
            break
        for upd in updates:
            uid = upd.get("update_id")
            if isinstance(uid, int):
                max_id = uid if max_id is None else max(max_id, uid)
    if max_id is not None:
        logging.info(f"✅ Старые апдейты очищены. Пропускаем всё до update_id={max_id}.")
        return max_id + 1
    logging.info("✅ Старых апдейтов нет.")
    return None

# ─── SMTP ошибки: классификация ────────────────────────────────────────────────
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

# ─── Email ─────────────────────────────────────────────────────────────────────
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

# ─── Удаление первой строки ───────────────────────────────────────────────────
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

# ─── Одна итерация сценария ───────────────────────────────────────────────────
def process_once_and_report(chat_id: int):
    service = build_sheets_service()
    sheet = service.spreadsheets()

    # Читаем A1:D1
    rng = f"{SHEET_NAME}!A1:D1"
    res = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=rng).execute()
    values = res.get('values', [])

    # Если очередь пуста — сообщаем И всё равно удаляем строку 1
    if not values or not values[0] or all(cell == "" for cell in values[0]):
        tg_send(chat_id, "ℹ️ Очередь пуста: в таблице нет строк для отправки.")
        delete_first_row(service)
        return

    row = values[0]
    email   = row[0] if len(row) > 0 else ""
    subject = row[1] if len(row) > 1 else ""
    html    = row[2] if len(row) > 2 else ""
    delay   = row[3] if len(row) > 3 else "0"

    # Задержка
    try:
        delay_seconds = int(str(delay).strip())
    except:
        delay_seconds = 0
    if delay_seconds > 0:
        logging.info(f"⏳ Ожидание задержки в {delay_seconds} секунд перед отправкой.")
        time.sleep(delay_seconds)

    # Письмо
    success, err_text = send_email(email, subject, html)

    # Удаляем первую строку всегда
    delete_first_row(service)

    # Отчёт
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

# ─── Фоновой polling-процесс ──────────────────────────────────────────────────
def polling_loop():
    logging.info("🚀 Server mode (long polling). Отключаю webhook и очищаю очередь…")
    tg_delete_webhook()
    offset = tg_drain_pending()
    logging.info("🟢 Жду НОВЫЕ сообщения…")

    while True:
        updates = tg_get_updates(offset=offset, timeout=50)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            logging.info(f"🔔 Триггер из TG chat_id={chat_id}")
            try:
                process_once_and_report(chat_id)
            except Exception as e:
                logging.info(f"🚨 Общая ошибка обработки: {e}")
                tg_send(chat_id, f"❌ Общая ошибка обработки: {e}")

# ─── HTTP endpoints (для Render healthchecks) ──────────────────────────────────
@app.get("/health")
def health():
    return jsonify(ok=True)

# ─── Точка входа ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Стартуем polling в отдельном потоке,
    # а Flask держит открытый порт для Render.
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
