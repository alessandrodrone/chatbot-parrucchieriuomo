from __future__ import annotations

import os
import re
import json
import datetime as dt
from typing import Dict, List, Optional, Tuple

from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# =====================================================
# Flask (OBBLIGATORIO per gunicorn app:app)
# =====================================================
app = Flask(__name__)

# =====================================================
# ENV
# =====================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "30"))
MAX_LOOKAHEAD_DAYS = 14

# =====================================================
# Google clients
# =====================================================
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


# =====================================================
# Utils
# =====================================================
def norm_phone(p: str) -> str:
    return re.sub(r"\D+", "", (p or "").replace("whatsapp:", ""))


def phone_matches(a: str, b: str) -> bool:
    a, b = norm_phone(a), norm_phone(b)
    return a == b or a.endswith(b) or b.endswith(a)


def tzinfo_for(tz: str):
    return ZoneInfo(tz) if ZoneInfo else None


def now_local(tz: str):
    zi = tzinfo_for(tz)
    return dt.datetime.now(zi) if zi else dt.datetime.now()


# =====================================================
# Parsing data / ora (IT)
# =====================================================
def parse_date(text: str, tz: str) -> Optional[dt.date]:
    t = text.lower()
    today = now_local(tz).date()
    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)

    m = re.search(r"(\d{1,2})[\/\-](\d{1,2})", t)
    if m:
        try:
            return dt.date(today.year, int(m.group(2)), int(m.group(1)))
        except Exception:
            return None
    return None


def parse_time(text: str) -> Optional[dt.time]:
    m = re.search(r"([01]?\d|2[0-3])[:\.]?([0-5]\d)?", text)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2) or 0))
    return None


# =====================================================
# Sheets helpers
# =====================================================
def load_tab(tab: str) -> List[Dict[str, str]]:
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!A:Z"
    ).execute()
    values = res.get("values", [])
    if not values:
        return []
    headers = values[0]
    out = []
    for r in values[1:]:
        out.append({headers[i]: r[i] if i < len(r) else "" for i in range(len(headers))})
    return out


def upsert_row(tab: str, match_fn, data: Dict[str, str]):
    rows = load_tab(tab)
    headers = rows[0].keys() if rows else data.keys()
    for i, r in enumerate(rows):
        if match_fn(r):
            idx = i + 2
            sheets().spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=f"{tab}!A{idx}",
                valueInputOption="RAW",
                body={"values": [[data.get(h, "") for h in headers]]}
            ).execute()
            return
    sheets().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!A:Z",
        valueInputOption="RAW",
        body={"values": [[data.get(h, "") for h in headers]]}
    ).execute()


# =====================================================
# Load config
# =====================================================
def load_shop(phone: str):
    for s in load_tab("shops"):
        if phone_matches(phone, s.get("whatsapp_number")):
            return s
    return None


def load_services(shop_id: str):
    return [s for s in load_tab("services") if s.get("shop_id") == shop_id]


def load_hours(shop_id: str):
    out = {i: [] for i in range(7)}
    for h in load_tab("hours"):
        if h.get("shop_id") == shop_id:
            out[int(h["weekday"])].append(
                (dt.time.fromisoformat(h["start"]), dt.time.fromisoformat(h["end"]))
            )
    return out


# =====================================================
# Session
# =====================================================
def get_session(shop_id: str, phone: str):
    for r in load_tab("sessions"):
        if r.get("shop_id") == shop_id and phone_matches(phone, r.get("phone")):
            return r
    return None


def save_session(shop_id: str, phone: str, state: str, data: dict):
    upsert_row(
        "sessions",
        lambda r: r.get("shop_id") == shop_id and phone_matches(phone, r.get("phone")),
        {
            "shop_id": shop_id,
            "phone": norm_phone(phone),
            "state": state,
            "data": json.dumps(data),
            "updated_at": dt.datetime.utcnow().isoformat()
        }
    )


def reset_session(shop_id: str, phone: str):
    save_session(shop_id, phone, "", {})


# =====================================================
# CORE BOT (FIX DEFINITIVO)
# =====================================================
def handle_message(shop: dict, phone: str, text: str) -> str:
    tz = shop["timezone"]
    shop_id = shop["shop_id"]
    services = load_services(shop_id)

    sess = get_session(shop_id, phone)
    state = sess.get("state") if sess else ""
    data = json.loads(sess["data"]) if sess and sess.get("data") else {}

    # Greeting
    if text.lower() in {"ciao", "salve"} and not state:
        return f"Ciao! 👋 Sei in contatto con *{shop['name']}* 💈\nDimmi quando vuoi prenotare 😊"

    date = data.get("date") or parse_date(text, tz)
    time = data.get("time") or parse_time(text)

    service = data.get("service")
    if not service:
        for s in services:
            if s["name"].lower() in text.lower():
                service = s
                break

    # UPSell FIX
    if state == "upsell_barba":
        if "taglio e barba" in text.lower():
            for s in services:
                if "taglio" in s["name"].lower() and "barba" in s["name"].lower():
                    service = s
        # 🔥 CONTINUA SENZA PERDERE DATA/ORA
        save_session(shop_id, phone, "booking", {
            "service": service,
            "date": date.isoformat() if date else None,
            "time": time.isoformat() if time else None
        })
        return f"Perfetto 👍 Sto cercando disponibilità per {date.strftime('%d/%m')} alle {time.strftime('%H:%M')}"

    # Richiesta upsell
    if service and "taglio" in service["name"].lower() and not data.get("upsell_done"):
        save_session(shop_id, phone, "upsell_barba", {
            "service": service,
            "date": date.isoformat() if date else None,
            "time": time.isoformat() if time else None,
            "upsell_done": True
        })
        return "Vuoi aggiungere anche la *barba* o solo *taglio*?"

    if not date or not time:
        save_session(shop_id, phone, "need_when", data)
        return "Dimmi quando vuoi venire (es. domani alle 18)"

    return f"✅ Prenotazione in corso: {service['name']} il {date.strftime('%d/%m')} alle {time.strftime('%H:%M')}"


# =====================================================
# ROUTES
# =====================================================
@app.route("/test")
def test():
    phone = request.args.get("phone")
    msg = request.args.get("msg", "")
    shop = load_shop(phone)
    if not shop:
        return jsonify({"error": "shop non trovato"}), 404
    return jsonify({
        "shop": shop["name"],
        "phone": phone,
        "message_in": msg,
        "bot_reply": handle_message(shop, phone, msg)
    })


@app.route("/")
def home():
    return "Bot attivo ✅"


@app.route("/wa", methods=["POST"])
def wa_placeholder():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
