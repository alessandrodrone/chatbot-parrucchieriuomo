from __future__ import annotations

import os, re, json, difflib
import datetime as dt
from typing import Dict, List, Optional, Tuple
from flask import Flask, request, jsonify

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ============================================================
# APP
# ============================================================
app = Flask(__name__)

# ============================================================
# ENV
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "30"))
MAX_LOOKAHEAD_DAYS = 14
SLOT_MINUTES = 30

# ============================================================
# GOOGLE CLIENTS
# ============================================================
_sheets = None
_calendar = None

def creds():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/calendar",
    ]
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)

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

# ============================================================
# UTILS
# ============================================================
def norm_phone(p: str) -> str:
    return re.sub(r"\D+", "", p or "")

def now():
    return dt.datetime.now()

# ============================================================
# DATE / TIME PARSING
# ============================================================
def parse_date(text: str) -> Optional[dt.date]:
    t = text.lower()
    today = dt.date.today()
    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)
    return None

def parse_time(text: str) -> Optional[dt.time]:
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]?([0-5]\d)?\b", text)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2) or 0))
    return None

def parse_fascia(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = text.lower()
    if "mattina" in t:
        return dt.time(9,0), dt.time(12,0)
    if "pomeriggio" in t:
        return dt.time(14,0), dt.time(18,0)
    if "tardo" in t or "sera" in t:
        return dt.time(17,0), dt.time(21,0)
    return None, None

# ============================================================
# FUZZY SERVICE MATCH
# ============================================================
def fuzzy_service(text: str, services: List[Dict]) -> Optional[Dict]:
    if not services:
        return None
    names = [s["name"] for s in services]
    match = difflib.get_close_matches(
        text.lower(),
        [n.lower() for n in names],
        n=1,
        cutoff=0.6
    )
    if match:
        for s in services:
            if s["name"].lower() == match[0]:
                return s
    return None

# ============================================================
# SHEETS LOADERS
# ============================================================
def load_tab(tab: str) -> List[Dict]:
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!A:Z"
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return []
    headers = rows[0]
    return [
        dict(zip(headers, r + [""]*(len(headers)-len(r))))
        for r in rows[1:]
    ]

def load_shop(phone: str) -> Optional[Dict]:
    for s in load_tab("shops"):
        if norm_phone(s.get("whatsapp_number")) == norm_phone(phone):
            return s
    return None

def load_services(shop_id: str) -> List[Dict]:
    return [s for s in load_tab("services") if s.get("shop_id") == shop_id]

def load_hours(shop_id: str) -> Dict[int, List[Tuple[dt.time, dt.time]]]:
    out = {i: [] for i in range(7)}
    for r in load_tab("hours"):
        if r.get("shop_id") == shop_id:
            out[int(r["weekday"])].append(
                (dt.time.fromisoformat(r["start"]),
                 dt.time.fromisoformat(r["end"]))
            )
    return out

# ============================================================
# SESSION (memory breve)
# ============================================================
SESSIONS: Dict[str, Dict] = {}

def get_session(key):
    s = SESSIONS.get(key)
    if not s:
        return {}
    if (now() - s["ts"]).total_seconds()/60 > SESSION_TTL_MINUTES:
        del SESSIONS[key]
        return {}
    return s

def save_session(key, data):
    SESSIONS[key] = {"ts": now(), **data}

# ============================================================
# CALENDAR
# ============================================================
def slot_free(cal_id, start, end):
    evs = calendar().events().list(
        calendarId=cal_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True
    ).execute().get("items", [])
    return len(evs) == 0

# ============================================================
# CORE LOGIC (COPY v4 + SICUREZZA)
# ============================================================
def handle(shop, customer, text):
    key = f"{shop['shop_id']}:{customer}"
    sess = get_session(key)
    services = load_services(shop["shop_id"])
    hours = load_hours(shop["shop_id"])
    t = text.lower().strip()

    # ---- GREETING
    if t in {"ciao","salve","buongiorno","buonasera","hey"}:
        return (
            f"Ciao! 👋 Sono l’assistente di *{shop['name']}*.\n"
            f"Ti aiuto a fissare l’appuntamento in pochi secondi 😊\n\n"
            f"Quando preferisci venire? (es. “domani alle 18”, “sabato pomeriggio”)"
        )

    # ---- FUZZY / SERVICE
    service = fuzzy_service(t, services)
    if service:
        sess["service"] = service
        save_session(key, sess)
    elif "service" not in sess:
        lst = "\n".join(f"• {s['name']}" for s in services)
        return (
            "Nessun problema 😊\n"
            "Dimmi solo che servizio ti serve:\n"
            f"{lst}"
        )

    # ---- DATE / TIME / FASCIA
    d = parse_date(t)
    tm = parse_time(t)
    a,b = parse_fascia(t)

    if d:
        sess["date"] = d
    if tm:
        sess["time"] = tm
    if a:
        sess["after"], sess["before"] = a,b

    save_session(key, sess)

    # ---- GUIDA UMANA
    if "date" not in sess:
        return (
            "Perfetto 👍\n"
            "In che giorno ti è più comodo?\n"
            "(es. “domani”, “sabato”, “martedì”)"
        )

    if "time" not in sess and "after" not in sess:
        return (
            "Preferisci *mattina*, *pomeriggio* o *sera*? 😊"
        )

    # ---- SLOT SEARCH INTELLIGENTE
    dur = int(sess["service"].get("duration",30))
    cal_id = shop["calendar_id"]
    base = sess["date"]
    found = []

    for day in range(MAX_LOOKAHEAD_DAYS):
        dday = base + dt.timedelta(days=day)
        for st,en in hours.get(dday.weekday(),[]):
            cur = dt.datetime.combine(dday, st)
            while cur + dt.timedelta(minutes=dur) <= dt.datetime.combine(dday,en):
                if slot_free(cal_id, cur, cur+dt.timedelta(minutes=dur)):
                    found.append(cur)
                    if len(found) >= 3:
                        break
                cur += dt.timedelta(minutes=SLOT_MINUTES)
        if found:
            break

    if not found:
        return (
            "Al momento non vedo disponibilità 😕\n"
            "Dimmi un altro giorno o una fascia diversa e riproviamo 👍"
        )

    # ---- SPIEGA + PROPOSTA
    if len(found) == 1:
        slot = found[0]
        sess["slot"] = slot
        save_session(key, sess)
        return (
            "Ottimo 😊 Ho trovato questo posto:\n\n"
            f"💈 *{sess['service']['name']}*\n"
            f"🕒 {slot.strftime('%a %d/%m %H:%M')}\n\n"
            "Confermi? Rispondi *OK* oppure scrivi un altro orario."
        )

    # alternative
    lines = []
    for i,s in enumerate(found,1):
        lines.append(f"{i}) {s.strftime('%a %d/%m %H:%M')}")
    sess["options"] = found
    save_session(key, sess)

    return (
        "Capito 😊 L’orario che hai chiesto è occupato.\n"
        "Posso però offrirti queste alternative:\n\n"
        + "\n".join(lines) +
        "\n\nRispondi con il numero oppure scrivi un orario diverso."
    )

# ============================================================
# ROUTE
# ============================================================
@app.route("/test")
def test():
    phone = request.args.get("phone")
    customer = request.args.get("customer")
    msg = request.args.get("msg","")

    shop = load_shop(phone)
    if not shop:
        return jsonify({"error":"shop not found"}),404

    reply = handle(shop, customer, msg)
    return jsonify({
        "shop": shop["name"],
        "shop_number": phone,
        "customer": customer,
        "message_in": msg,
        "bot_reply": reply
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))
