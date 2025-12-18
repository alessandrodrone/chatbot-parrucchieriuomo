from flask import Flask, request, jsonify
import os, json, re, datetime as dt
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
TZ = "Europe/Rome"

# -------- GOOGLE SHEETS ----------
def sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")),
        scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)

def load_sheet(range_):
    svc = sheets_service()
    res = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=range_
    ).execute()
    rows = res.get("values", [])
    header, data = rows[0], rows[1:]
    return [dict(zip(header, r)) for r in data]

# -------- LOAD DATA ----------
def load_shop(to_number):
    for s in load_sheet("shops"):
        if s["whatsapp_number"] == to_number:
            return s
    return None

def load_services(shop_id):
    return [s for s in load_sheet("services") if s["shop_id"] == shop_id]

def load_hours(shop_id):
    return [h for h in load_sheet("hours") if h["shop_id"] == shop_id]

# -------- UTILS ----------
def parse_time(txt):
    m = re.search(r"(\d{1,2})[:\.]?(\d{2})?", txt)
    if not m: return None
    return dt.time(int(m.group(1)), int(m.group(2) or 0))

# -------- CORE ----------
def handle_message(shop, phone, text):
    services = load_services(shop["shop_id"])
    text = text.lower()

    if "ciao" in text or "prenot" in text:
        if len(services) > 1:
            s = "\n".join(f"- {x['name']}" for x in services)
            return f"Ciao! 💈 Che servizio desideri?\n{s}"
        return "Perfetto 👍 Quando vuoi venire?"

    for s in services:
        if s["name"].lower() in text:
            return f"Perfetto 👍 Per che giorno vuoi prenotare il {s['name']}?"

    t = parse_time(text)
    if t:
        return f"✅ Ottimo! Provo a prenotarti alle {t.strftime('%H:%M')}"

    return "Dimmi giorno e ora 😊"

# -------- TEST ROUTE ----------
@app.route("/test")
def test():
    phone = request.args.get("phone")
    msg = request.args.get("msg")
    shop = load_shop(phone)
    if not shop:
        return jsonify(error="shop non trovato")
    return jsonify(reply=handle_message(shop, phone, msg))

@app.route("/")
def home():
    return "Chatbot Parrucchieri attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
