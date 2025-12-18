from __future__ import annotations

import os
import re
import json
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# ============================================================
# FLASK APP (gunicorn app:app)
# ============================================================
app = Flask(__name__)


# ============================================================
# ENV
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "30"))
MAX_LOOKAHEAD_DAYS = int(os.getenv("MAX_LOOKAHEAD_DAYS", "14"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "20"))


# ============================================================
# GOOGLE CLIENTS (lazy)
# ============================================================
_sheets = None
_calendar = None


def _creds():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/calendar",
    ]
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


def sheets():
    global _sheets
    if _sheets is None:
        _sheets = build("sheets", "v4", credentials=_creds(), cache_discovery=False)
    return _sheets


def calendar():
    global _calendar
    if _calendar is None:
        _calendar = build("calendar", "v3", credentials=_creds(), cache_discovery=False)
    return _calendar


# ============================================================
# CACHE
# ============================================================
_CACHE: Dict[str, Dict[str, Any]] = {}


def cache_get(key: str):
    item = _CACHE.get(key)
    if not item:
        return None
    if (dt.datetime.utcnow() - item["ts"]).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return item["data"]


def cache_set(key: str, data: Any):
    _CACHE[key] = {"ts": dt.datetime.utcnow(), "data": data}


def cache_del(key: str):
    if key in _CACHE:
        del _CACHE[key]


# ============================================================
# PHONE HELPERS
# ============================================================
def norm_phone(p: str) -> str:
    if not p:
        return ""
    p = p.replace("whatsapp:", "")
    digits = re.sub(r"\D+", "", p)
    return digits.lstrip("0") or digits


def phone_matches(a: str, b: str) -> bool:
    da, db = norm_phone(a), norm_phone(b)
    if da == db:
        return True
    if da.startswith("39") and da[2:] == db:
        return True
    if db.startswith("39") and db[2:] == da:
        return True
    return False


# ============================================================
# DATE / TIME PARSING (IT)
# ============================================================
WEEKDAYS_IT = {
    "lunedi": 0, "lunedì": 0, "lun": 0,
    "martedi": 1, "martedì": 1, "mar": 1,
    "mercoledi": 2, "mercoledì": 2, "mer": 2,
    "giovedi": 3, "giovedì": 3, "gio": 3,
    "venerdi": 4, "venerdì": 4, "ven": 4,
    "sabato": 5, "sab": 5,
    "domenica": 6, "dom": 6,
}


def tzinfo_for(tz: str):
    return ZoneInfo(tz) if ZoneInfo else None


def now_local(tz: str):
    zi = tzinfo_for(tz)
    return dt.datetime.now(zi) if zi else dt.datetime.now()


def parse_date(text: str, tz: str) -> Optional[dt.date]:
    t = text.lower()
    today = now_local(tz).date()

    if "oggi" in t:
        return today
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "stasera" in t:
        return today

    m = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)
    if m:
        d, mth = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
        try:
            return dt.date(y, mth, d)
        except Exception:
            return None

    for k, wd in WEEKDAYS_IT.items():
        if k in t:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    return None


def parse_time(text: str) -> Optional[dt.time]:
    t = text.lower()

    m = re.search(r"([01]?\d|2[0-3])[:\.]([0-5]\d)", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    m = re.search(r"\b(alle|ore)\s*([01]?\d|2[0-3])\b", t)
    if m:
        return dt.time(int(m.group(2)), 0)

    return None


def parse_window(text: str):
    t = text.lower()
    after = before = None

    if "mattina" in t:
        after, before = dt.time(9, 0), dt.time(12, 0)
    if "pomeriggio" in t:
        after, before = dt.time(14, 0), dt.time(19, 0)
    if "sera" in t:
        after, before = dt.time(17, 30), dt.time(22, 0)

    return after, before


# ============================================================
# GOOGLE SHEETS
# ============================================================
def load_tab(tab: str) -> List[Dict[str, str]]:
    key = f"tab:{tab}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!A:Z"
    ).execute()

    values = res.get("values", [])
    if not values:
        return []

    headers = values[0]
    rows = []
    for r in values[1:]:
        obj = {}
        for i, h in enumerate(headers):
            obj[h] = r[i] if i < len(r) else ""
        rows.append(obj)

    cache_set(key, rows)
    return rows


# ============================================================
# LOAD CONFIG
# ============================================================
def load_shop_by_phone(phone: str):
    for s in load_tab("shops"):
        if phone_matches(phone, s.get("whatsapp_number", "")):
            return s
    return None


def load_hours(shop_id: str):
    out = {i: [] for i in range(7)}
    for r in load_tab("hours"):
        if r.get("shop_id") != shop_id:
            continue
        out[int(r["weekday"])].append(
            (dt.time.fromisoformat(r["start"]), dt.time.fromisoformat(r["end"]))
        )
    return out


def load_services(shop_id: str):
    out = []
    for r in load_tab("services"):
        if r.get("shop_id") == shop_id and r.get("active", "TRUE") != "FALSE":
            out.append(r)
    return out


# ============================================================
# CORE LOGIC (SEMPLIFICATA MA ROBUSTA)
# ============================================================
def handle_message(shop: dict, phone: str, text: str) -> str:
    name = shop.get("name", "il salone")
    tz = shop.get("timezone", "Europe/Rome")

    if text.lower() in {"ciao", "salve", "buongiorno", "buonasera"}:
        return (
            f"Ciao! 👋 Sei in contatto con *{name}* 💈\n"
            f"Dimmi quando vuoi prenotare 😊"
        )

    date_ = parse_date(text, tz)
    time_ = parse_time(text)

    if date_ and time_:
        return (
            f"Perfetto 👍 Sto controllando la disponibilità per "
            f"{date_.strftime('%d/%m')} alle {time_.strftime('%H:%M')}.\n"
            f"Un attimo ⏳"
        )

    return (
        "Dimmi pure quando vuoi venire "
        "(es. *domani alle 18*, *venerdì pomeriggio*)."
    )


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"


@app.route("/test")
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "ciao")

    try:
        shop = load_shop_by_phone(phone)
        if not shop:
            return jsonify({"error": "shop non trovato"}), 404

        reply = handle_message(shop, phone, msg)
        return jsonify({
            "shop": shop.get("name"),
            "phone": phone,
            "message_in": msg,
            "bot_reply": reply
        })

    except HttpError as e:
        return jsonify({"error": "google_api_error", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "server_error", "details": str(e)}), 500


@app.route("/wa", methods=["POST"])
def wa_placeholder():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
