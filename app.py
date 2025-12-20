from __future__ import annotations
import os, re, json, difflib
import datetime as dt
from typing import Dict, Any, List, Optional, Tuple
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =====================================================
# FLASK / GUNICORN
# =====================================================
app = Flask(__name__)

# =====================================================
# ENV
# =====================================================
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SESSION_TTL_MIN = 30
MAX_LOOKAHEAD_DAYS = 14

# =====================================================
# GOOGLE CLIENTS
# =====================================================
def creds():
    return service_account.Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/calendar"
        ]
    )

_sheets = _calendar = None

def sheets():
    global _sheets
    if not _sheets:
        _sheets = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    return _sheets

def calendar():
    global _calendar
    if not _calendar:
        _calendar = build("calendar", "v3", credentials=creds(), cache_discovery=False)
    return _calendar

# =====================================================
# HELPERS
# =====================================================
def norm_phone(p: str) -> str:
    return re.sub(r"\D+", "", p or "")

def fuzzy_match(text: str, choices: List[str]) -> Optional[str]:
    matches = difflib.get_close_matches(text.lower(), choices, n=1, cutoff=0.6)
    return matches[0] if matches else None

def now():
    return dt.datetime.utcnow()

# =====================================================
# SHEETS
# =====================================================
def load_tab(tab: str) -> List[Dict[str, str]]:
    res = sheets().spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{tab}!A:Z"
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return []
    headers = rows[0]
    out = []
    for r in rows[1:]:
        obj = {}
        for i, h in enumerate(headers):
            obj[h] = r[i] if i < len(r) else ""
        out.append(obj)
    return out

# =====================================================
# SHOP RESOLUTION
# =====================================================
def get_shop(shop_number: str) -> Optional[Dict[str, str]]:
    for s in load_tab("shops"):
        if norm_phone(s.get("whatsapp_number")) == norm_phone(shop_number):
            return s
    return None

# =====================================================
# SESSION (SHORT MEMORY)
# =====================================================
_SESS: Dict[str, Dict[str, Any]] = {}

def get_session(key: str) -> Dict[str, Any]:
    s = _SESS.get(key)
    if not s or (now() - s["ts"]).total_seconds() > SESSION_TTL_MIN * 60:
        _SESS[key] = {"ts": now(), "data": {}}
    return _SESS[key]["data"]

def save_session(key: str, data: Dict[str, Any]):
    _SESS[key] = {"ts": now(), "data": data}

def reset_session(key: str):
    _SESS.pop(key, None)

# =====================================================
# TIME PARSING (IT)
# =====================================================
def parse_date(text: str) -> Optional[dt.date]:
    t = text.lower()
    today = now().date()
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "oggi" in t:
        return today
    return None

def parse_time(text: str) -> Optional[dt.time]:
    m = re.search(r"(\d{1,2})[:\.]?(\d{2})?", text)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2)) if m.group(2) else 0
    if 0 <= h <= 23:
        return dt.time(h, mnt)
    return None

# =====================================================
# CORE LOGIC
# =====================================================
def handle_message(shop: Dict[str, str], customer: str, text: str) -> str:
    key = f"{shop['shop_id']}:{customer}"
    sess = get_session(key)
    text_l = text.lower()

    # --- cancel
    if any(x in text_l for x in ["annulla", "cancella", "stop"]):
        reset_session(key)
        return "Va bene 👍 Se vuoi riprenotare dimmi pure quando."

    # --- greeting
    if text_l in ["ciao", "salve", "buongiorno", "buonasera"] and not sess:
        return f"Ciao! 👋 Sei in contatto con *{shop['name']}* 💈\nDimmi quando vuoi prenotare 😊"

    # --- parse intent
    date = parse_date(text)
    time = parse_time(text)

    # --- services
    services = [s["name"] for s in load_tab("services") if s["shop_id"] == shop["shop_id"]]
    chosen = fuzzy_match(text_l, [s.lower() for s in services])

    if chosen:
        sess["service"] = chosen

    if date:
        sess["date"] = date.isoformat()
    if time:
        sess["time"] = time.strftime("%H:%M")

    save_session(key, sess)

    # --- missing service
    if "service" not in sess:
        return (
            f"Perfetto 😊 Per che servizio vuoi prenotare da *{shop['name']}*?\n" +
            "\n".join(f"• {s}" for s in services)
        )

    # --- missing date/time
    if "date" not in sess or "time" not in sess:
        return "Quando preferisci venire? (es. “domani alle 18”, “sabato pomeriggio”)."

    # --- CONFIRM
    d = dt.date.fromisoformat(sess["date"])
    t = dt.time.fromisoformat(sess["time"])
    return (
        "Confermi questo appuntamento?\n"
        f"💈 *{sess['service']}*\n"
        f"🕒 {d.strftime('%a %d/%m')} {t.strftime('%H:%M')}\n\n"
        "Rispondi *OK* per confermare oppure *annulla*."
    )

# =====================================================
# ROUTE
# =====================================================
@app.route("/test")
def test():
    shop_number = request.args.get("phone")
    customer = request.args.get("customer")
    msg = request.args.get("msg", "")

    shop = get_shop(shop_number)
    if not shop:
        return jsonify({"error": "shop not found"}), 404

    reply = handle_message(shop, customer, msg)
    return jsonify({
        "shop": shop["name"],
        "shop_number": shop_number,
        "customer": customer,
        "message_in": msg,
        "bot_reply": reply
    })

@app.route("/")
def home():
    return "RispondiTu attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
