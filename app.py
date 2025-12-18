from flask import Flask, request, jsonify
import os, json, re
from datetime import datetime, timedelta, time
from dateutil import tz, parser
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ================= CONFIG =================
TZ_DEFAULT = "Europe/Rome"
SLOT_FALLBACK = 30

GOOGLE_SERVICE_ACCOUNT_JSON = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets.readonly"
]

# ================= GOOGLE =================
creds = service_account.Credentials.from_service_account_info(
    GOOGLE_SERVICE_ACCOUNT_JSON,
    scopes=SCOPES
)
sheets = build("sheets", "v4", credentials=creds)
calendar = build("calendar", "v3", credentials=creds)

# ================= UTIL =================
def normalize_phone(p):
    return re.sub(r"\D", "", p or "")

def load_sheet(name):
    res = sheets.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=name
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return []
    keys = rows[0]
    return [dict(zip(keys, r)) for r in rows[1:]]

def find_shop(phone):
    phone = normalize_phone(phone)
    for s in load_sheet("shops"):
        if normalize_phone(s["whatsapp_number"]) == phone:
            return s
    return None

def get_services(shop_id):
    return [s for s in load_sheet("services") if s["shop_id"] == shop_id]

def get_session(shop_id, phone):
    for s in load_sheet("sessions"):
        if s["shop_id"] == shop_id and s["phone"] == phone:
            return s
    return None

def save_session(shop_id, phone, state, data):
    # simulazione: in prod diventa DB
    pass

def parse_natural_date(text):
    text = text.lower()
    today = datetime.now()

    if "domani" in text:
        return today + timedelta(days=1)
    if "dopodomani" in text:
        return today + timedelta(days=2)
    if "stasera" in text:
        return today.replace(hour=18, minute=0)

    try:
        return parser.parse(text, dayfirst=True, fuzzy=True)
    except:
        return None

def parse_time_from_text(text):
    m = re.search(r"(\d{1,2})([:\.](\d{2}))?", text)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(3) or 0)
    return time(h, mnt)

# ================= SLOTS =================
def generate_slots(date, start, end, minutes):
    slots = []
    cur = datetime.combine(date.date(), start)
    stop = datetime.combine(date.date(), end)
    while cur + timedelta(minutes=minutes) <= stop:
        slots.append(cur)
        cur += timedelta(minutes=minutes)
    return slots

# ================= CORE =================
def handle_message(shop, phone, text):
    services = get_services(shop["shop_id"])
    session = get_session(shop["shop_id"], phone)
    text_l = text.lower()

    # GREETING
    if not session:
        if len(services) > 1:
            names = "\n".join(f"- {s['name']}" for s in services)
            return f"Ciao! 💈 Che servizio desideri?\n{names}"
        return "Ciao! 💈 Quando vuoi venire?"

    # SERVICE
    if session["state"] == "need_service":
        for s in services:
            if s["name"].lower() in text_l:
                save_session(shop["shop_id"], phone, "need_date", {"service": s})
                return "Perfetto 👍 Per che giorno?"
        return "Dimmi che servizio desideri 😊"

    # DATE
    if session["state"] == "need_date":
        d = parse_natural_date(text)
        if not d:
            return "Dimmi una data valida (es. domani, 17/12)"
        save_session(shop["shop_id"], phone, "need_time", {"date": d.isoformat()})
        return "A che ora preferisci?"

    # TIME
    if session["state"] == "need_time":
        t = parse_time_from_text(text)
        if not t:
            return "Dimmi un orario valido (es. 17:30)"

        return f"✅ Prenotazione confermata alle {t.strftime('%H:%M')}"

    return "Dimmi quando vuoi venire 😊"

# ================= ROUTES =================
@app.route("/test")
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "")
    shop = find_shop(phone)
    if not shop:
        return jsonify({"error": "shop non trovato"})

    reply = handle_message(shop, normalize_phone(phone), msg)
    return jsonify({
        "bot_reply": reply,
        "message_in": msg,
        "phone": phone,
        "shop": shop["name"]
    })

@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
