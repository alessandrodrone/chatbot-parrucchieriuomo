from __future__ import annotations
import os, json, re, datetime as dt
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dateutil.relativedelta import relativedelta

# =========================
# CONFIG
# =========================
app = Flask(__name__)

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
TZ = "Europe/Rome"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/calendar",
]

# =========================
# GOOGLE CLIENTS
# =========================
def sheets():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)

def calendar():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

# =========================
# HELPERS
# =========================
def normalize_phone(p):
    return re.sub(r"\D", "", p or "")

def now():
    return dt.datetime.now()

def parse_date(text):
    t = text.lower()
    today = now().date()
    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)
    return None

def parse_time(text):
    m = re.search(r"(\d{1,2})([:\.](\d{2}))?", text)
    if not m:
        return None
    return dt.time(int(m.group(1)), int(m.group(3) or 0))

# =========================
# SHEET LOADERS
# =========================
def load_sheet(name):
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=name
    ).execute()
    rows = res.get("values", [])
    headers = rows[0]
    return [dict(zip(headers, r)) for r in rows[1:]]

def load_shop(phone):
    phone = normalize_phone(phone)
    for s in load_sheet("shops"):
        if normalize_phone(s["whatsapp_number"]) == phone:
            return s
    return None

def load_services(shop_id):
    return [s for s in load_sheet("services") if s["shop_id"] == shop_id]

def load_customer(shop_id, phone):
    phone = normalize_phone(phone)
    for c in load_sheet("customers"):
        if c["shop_id"] == shop_id and normalize_phone(c["phone"]) == phone:
            return c
    return None

def load_session(shop_id, phone):
    phone = normalize_phone(phone)
    for s in load_sheet("sessions"):
        if s["shop_id"] == shop_id and normalize_phone(s["phone"]) == phone:
            return s
    return None

# =========================
# CORE LOGIC
# =========================
def handle_message(shop, phone, text):
    phone = normalize_phone(phone)
    text_low = text.lower()

    services = load_services(shop["shop_id"])
    session = load_session(shop["shop_id"], phone)
    customer = load_customer(shop["shop_id"], phone)

    # ========= GREETING =========
    if not session and text_low in {"ciao", "salve", "buongiorno", "buonasera"}:
        if customer:
            return (
                f"Bentornato! 😊\n"
                f"L’ultima volta avevi fatto *{customer['last_service']}*.\n"
                f"Vuoi rifare lo stesso o cambiare?"
            )
        return (
            f"Ciao! 👋\n"
            f"Sei in contatto con *{shop['name']}* 💈\n\n"
            f"Quando ti farebbe comodo venire?"
        )

    # ========= SERVICE SELECTION =========
    if not session:
        for s in services:
            if s["name"].lower() in text_low:
                return (
                    f"Perfetto 👍\n"
                    f"*{s['name']}*\n\n"
                    f"Per che giorno?"
                )

        if len(services) > 1:
            elenco = "\n".join(f"• {s['name']}" for s in services)
            return f"Che servizio desideri?\n{elenco}"

    # ========= DATE =========
    date = parse_date(text)
    if date and not parse_time(text):
        return (
            f"Perfetto 😊\n"
            f"Per che ora?"
        )

    # ========= TIME =========
    time = parse_time(text)
    if date and time:
        return (
            f"Controllo la disponibilità per\n"
            f"📅 {date.strftime('%d/%m')} alle 🕒 {time.strftime('%H:%M')}…"
        )

    # ========= FALLBACK =========
    return (
        "Dimmi quando vuoi venire 😊\n"
        "Es: *domani alle 18*"
    )

# =========================
# ROUTES
# =========================
@app.route("/test")
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "")
    shop = load_shop(phone)
    if not shop:
        return jsonify({"error": "shop non trovato"})
    reply = handle_message(shop, phone, msg)
    return jsonify({
        "shop": shop["name"],
        "phone": phone,
        "message_in": msg,
        "bot_reply": reply
    })

@app.route("/")
def home():
    return "Chatbot Parrucchieri attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
