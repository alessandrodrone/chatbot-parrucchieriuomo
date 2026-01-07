from __future__ import annotations

import os, re, json, difflib, uuid
import datetime as dt
from typing import Dict, List, Optional, Tuple

from flask import Flask, request, jsonify

from google.oauth2 import service_account
from googleapiclient.discovery import build

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # fallback se non disponibile

# ============================================================
# APP
# ============================================================
app = Flask(__name__)

# ============================================================
# ENV
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")

SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "120"))  # più alto = meglio per WhatsApp
MAX_LOOKAHEAD_DAYS = int(os.getenv("MAX_LOOKAHEAD_DAYS", "14"))
DEFAULT_SLOT_MINUTES = int(os.getenv("DEFAULT_SLOT_MINUTES", "30"))

# parole chiave blocco ferie/chiuso (anche se evento fosse "free")
BLOCK_KEYWORDS = {"chiuso", "ferie", "malattia", "off", "closed", "vacation", "sick"}

# ============================================================
# GOOGLE CLIENTS
# ============================================================
_sheets = None
_calendar = None

def creds():
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

def norm_text(v: str) -> str:
    return (v or "").strip()

def safe_lower(v: str) -> str:
    return norm_text(v).lower()

def parse_bool(v: str) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes", "y", "ok"}

def parse_int(v: str, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default

def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def shop_tz(shop: Dict) -> dt.tzinfo:
    tz_name = shop.get("timezone") or shop.get("time_zone") or "Europe/Rome"
    if ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return dt.timezone(dt.timedelta(hours=1))
    return dt.timezone(dt.timedelta(hours=1))

def local_now(shop: Dict) -> dt.datetime:
    return utc_now().astimezone(shop_tz(shop))

def to_rfc3339(d: dt.datetime) -> str:
    # Google Calendar vuole timezone aware
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.isoformat()

# ============================================================
# DATE / TIME PARSING
# ============================================================
WEEKDAY_IT = {
    "lun": 0, "lunedì": 0, "lunedi": 0,
    "mar": 1, "martedì": 1, "martedi": 1,
    "mer": 2, "mercoledì": 2, "mercoledi": 2,
    "gio": 3, "giovedì": 3, "giovedi": 3,
    "ven": 4, "venerdì": 4, "venerdi": 4,
    "sab": 5, "sabato": 5,
    "dom": 6, "domenica": 6,
}

def parse_date(text: str, shop: Dict) -> Optional[dt.date]:
    t = safe_lower(text)
    today = local_now(shop).date()

    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)

    # "sabato", "lunedì", ecc.
    for k, wd in WEEKDAY_IT.items():
        if re.search(rf"\b{k}\b", t):
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    # formato 12/01 o 12-01 o 12/01/2026
    m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b", t)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        y = m.group(3)
        year = int(y) if y else today.year
        if year < 100:
            year += 2000
        try:
            return dt.date(year, mo, d)
        except Exception:
            return None

    return None

def parse_time(text: str) -> Optional[dt.time]:
    t = safe_lower(text)
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]?([0-5]\d)?\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2) or 0))
    return None

def parse_fascia(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = safe_lower(text)
    if "mattina" in t:
        return dt.time(9, 0), dt.time(12, 0)
    if "pomeriggio" in t:
        return dt.time(14, 0), dt.time(18, 0)
    if "sera" in t or "tardo" in t:
        return dt.time(17, 0), dt.time(21, 0)
    return None, None

def parse_customer_name(text: str) -> Optional[str]:
    # molto semplice: "sono Mario" / "mi chiamo Mario"
    t = norm_text(text)
    m = re.search(r"\b(sono|mi chiamo)\s+([A-Za-zÀ-ÖØ-öø-ÿ'\- ]{2,40})\b", t, re.IGNORECASE)
    if m:
        name = norm_text(m.group(2))
        # taglia eventuali extra
        name = re.split(r"[,.!?\n]", name)[0].strip()
        if 2 <= len(name) <= 40:
            return name
    return None

# ============================================================
# FUZZY SERVICE MATCH
# ============================================================
def fuzzy_service(text: str, services: List[Dict]) -> Optional[Dict]:
    q = safe_lower(text)
    names = [safe_lower(s.get("name", "")) for s in services]
    match = difflib.get_close_matches(q, names, n=1, cutoff=0.6)
    if match:
        target = match[0]
        for s in services:
            if safe_lower(s.get("name", "")) == target:
                return s
    # fallback: match substring
    for s in services:
        if safe_lower(s.get("name", "")) and safe_lower(s["name"]) in q:
            return s
    return None

# ============================================================
# OPERATOR PREFERENCE / REJECTION
# ============================================================
def detect_operator_preference(text: str, operators: List[Dict]) -> Optional[str]:
    t = safe_lower(text)
    # se c'è "no X" non trattarlo come preferenza
    if re.search(r"\b(no|non)\b", t):
        return None

    cleaned = re.sub(r"[^\w\sàèéìòù]", " ", t)
    words = set(cleaned.split())

    for op in operators:
        oid = safe_lower(op.get("operator_id", ""))
        oname = safe_lower(op.get("operator_name", ""))
        if oid and oid in words:
            return op["operator_id"]
        if oname and oname in words:
            return op["operator_id"]
    return None

def detect_operator_rejection(text: str, operators: List[Dict]) -> Optional[str]:
    t = safe_lower(text)
    for op in operators:
        name = safe_lower(op.get("operator_name", "")) or safe_lower(op.get("operator_id", ""))
        if not name:
            continue
        if re.search(rf"\b(no|non)\s+{re.escape(name)}\b", t):
            return op["operator_id"]
    return None

# ============================================================
# SHEETS HELPERS
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
    out = []
    for r in rows[1:]:
        out.append(dict(zip(headers, r + [""] * (len(headers) - len(r)))))
    return out

def sheet_append(tab: str, row: List[str]):
    sheets().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!A:Z",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]}
    ).execute()

def sheet_update_row(tab: str, row_index_1based: int, headers: List[str], data: Dict[str, str]):
    # costruisce una riga completa rispettando gli headers
    values = [data.get(h, "") for h in headers]
    rng = f"{tab}!A{row_index_1based}:{chr(ord('A') + len(headers)-1)}{row_index_1based}"
    sheets().spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=rng,
        valueInputOption="RAW",
        body={"values": [values]}
    ).execute()

def load_shop(shop_phone: str) -> Optional[Dict]:
    shop_phone_n = norm_phone(shop_phone)
    for s in load_tab("shops"):
        # supporta entrambe le colonne: whatsapp_number / whatsapp_numb
        ws = s.get("whatsapp_number") or s.get("whatsapp_numb") or ""
        if norm_phone(ws) == shop_phone_n:
            return s
    return None

def load_services(shop_id: str) -> List[Dict]:
    out = []
    for s in load_tab("services"):
        if s.get("shop_id") != shop_id:
            continue
        if not parse_bool(s.get("active", "TRUE")):
            continue
        out.append({
            **s,
            "duration": parse_int(s.get("duration", "30"), 30),
            "price": parse_int(s.get("price", "0"), 0),
            "active": True,
        })
    return out

def load_hours(shop_id: str) -> Dict[int, List[Tuple[dt.time, dt.time]]]:
    out = {i: [] for i in range(7)}
    for r in load_tab("hours"):
        if r.get("shop_id") != shop_id:
            continue
        try:
            wd = int(r["weekday"])
            out[wd].append((dt.time.fromisoformat(r["start"]), dt.time.fromisoformat(r["end"])))
        except Exception:
            pass
    return out

def load_operators(shop_id: str) -> List[Dict]:
    ops = []
    for r in load_tab("operators"):
        if r.get("shop_id") != shop_id:
            continue
        if not parse_bool(r.get("active", "TRUE")):
            continue
        ops.append({
            "shop_id": r.get("shop_id"),
            "operator_id": r.get("operator_id") or "",
            "operator_name": r.get("operator_name") or r.get("operator_id") or "",
            "calendar_id": r.get("calendar_id") or "",
            "priority": parse_int(r.get("priority", ""), 9999),
            "skills": r.get("skills", ""),
            "gender": r.get("gender", ""),
        })
    ops.sort(key=lambda x: (x["priority"], safe_lower(x["operator_name"])))
    return ops

# ============================================================
# PERSISTENT SESSION (Sheets: tab sessions)
# columns expected: shop_id | phone | state | data | updated_at
# ============================================================
def session_key(shop_id: str, customer_phone: str) -> Tuple[str, str]:
    return shop_id, norm_phone(customer_phone)

def load_session_from_sheet(shop_id: str, customer_phone: str) -> Dict:
    phone_n = norm_phone(customer_phone)
    rows = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="sessions!A:Z"
    ).execute().get("values", [])

    if not rows or len(rows) < 2:
        return {}

    headers = rows[0]
    idx_shop = headers.index("shop_id") if "shop_id" in headers else 0
    idx_phone = headers.index("phone") if "phone" in headers else 1
    idx_data = headers.index("data") if "data" in headers else 3
    idx_updated = headers.index("updated_at") if "updated_at" in headers else 4

    for i in range(1, len(rows)):
        r = rows[i] + [""] * (len(headers) - len(rows[i]))
        if r[idx_shop] == shop_id and norm_phone(r[idx_phone]) == phone_n:
            updated_at = r[idx_updated] or ""
            if updated_at:
                try:
                    updated_dt = dt.datetime.fromisoformat(updated_at)
                    # TTL
                    if (utc_now() - updated_dt).total_seconds() / 60 > SESSION_TTL_MINUTES:
                        return {}
                except Exception:
                    pass
            payload = r[idx_data] or ""
            if not payload:
                return {}
            try:
                return json.loads(payload)
            except Exception:
                return {}
    return {}

def save_session_to_sheet(shop_id: str, customer_phone: str, state: str, sess: Dict):
    phone_n = norm_phone(customer_phone)
    rows = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="sessions!A:Z"
    ).execute().get("values", [])

    if not rows:
        # se tab vuota, non gestiamo qui
        return

    headers = rows[0]
    # cerchiamo riga esistente
    row_index = None
    for i in range(1, len(rows)):
        r = rows[i] + [""] * (len(headers) - len(rows[i]))
        d = dict(zip(headers, r))
        if d.get("shop_id") == shop_id and norm_phone(d.get("phone", "")) == phone_n:
            row_index = i + 1  # 1-based
            break

    payload = json.dumps(sess, ensure_ascii=False)
    updated_at = utc_now().isoformat()

    data_row = {
        "shop_id": shop_id,
        "phone": phone_n,
        "state": state,
        "data": payload,
        "updated_at": updated_at,
    }

    if row_index is None:
        # append nel giusto ordine colonne
        row = [data_row.get(h, "") for h in headers]
        sheet_append("sessions", row)
    else:
        sheet_update_row("sessions", row_index, headers, data_row)

def clear_session_sheet(shop_id: str, customer_phone: str):
    # per semplicità: sovrascrive con data vuota (lascia la riga)
    phone_n = norm_phone(customer_phone)
    rows = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="sessions!A:Z"
    ).execute().get("values", [])
    if not rows or len(rows) < 2:
        return

    headers = rows[0]
    for i in range(1, len(rows)):
        r = rows[i] + [""] * (len(headers) - len(rows[i]))
        d = dict(zip(headers, r))
        if d.get("shop_id") == shop_id and norm_phone(d.get("phone", "")) == phone_n:
            row_index = i + 1
            data_row = {
                "shop_id": shop_id,
                "phone": phone_n,
                "state": "",
                "data": "",
                "updated_at": utc_now().isoformat(),
            }
            sheet_update_row("sessions", row_index, headers, data_row)
            return

# ============================================================
# CUSTOMERS (Sheets: tab customers)
# columns expected: shop_id | phone | last_service | total_visits | last_visit
# ============================================================
def upsert_customer_after_booking(shop_id: str, customer_phone: str, service_name: str, when_local: dt.datetime):
    phone_n = norm_phone(customer_phone)
    rows = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="customers!A:Z"
    ).execute().get("values", [])

    if not rows:
        return

    headers = rows[0]
    def hidx(name: str, default: int) -> int:
        return headers.index(name) if name in headers else default

    idx_shop = hidx("shop_id", 0)
    idx_phone = hidx("phone", 1)
    idx_last_service = hidx("last_service", 2)
    idx_total = hidx("total_visits", 3)
    idx_last_visit = hidx("last_visit", 4)

    row_index = None
    existing = None
    for i in range(1, len(rows)):
        r = rows[i] + [""] * (len(headers) - len(rows[i]))
        if r[idx_shop] == shop_id and norm_phone(r[idx_phone]) == phone_n:
            row_index = i + 1
            existing = r
            break

    last_visit_str = when_local.strftime("%Y-%m-%d %H:%M")

    if row_index is None:
        data_row = {h: "" for h in headers}
        data_row[headers[idx_shop]] = shop_id
        data_row[headers[idx_phone]] = phone_n
        if idx_last_service < len(headers):
            data_row[headers[idx_last_service]] = service_name
        if idx_total < len(headers):
            data_row[headers[idx_total]] = "1"
        if idx_last_visit < len(headers):
            data_row[headers[idx_last_visit]] = last_visit_str
        sheet_append("customers", [data_row.get(h, "") for h in headers])
    else:
        total = 0
        try:
            total = int(existing[idx_total] or "0")
        except Exception:
            total = 0
        total += 1

        data_row = dict(zip(headers, existing + [""] * (len(headers) - len(existing))))
        if idx_last_service < len(headers):
            data_row[headers[idx_last_service]] = service_name
        if idx_total < len(headers):
            data_row[headers[idx_total]] = str(total)
        if idx_last_visit < len(headers):
            data_row[headers[idx_last_visit]] = last_visit_str

        sheet_update_row("customers", row_index, headers, data_row)

# ============================================================
# CALENDAR HELPERS
# ============================================================
def _has_block_keyword(summary: str) -> bool:
    s = safe_lower(summary)
    return any(k in s for k in BLOCK_KEYWORDS)

def event_to_datetime_range(ev: Dict, tz: dt.tzinfo) -> Tuple[dt.datetime, dt.datetime]:
    """
    Normalizza eventi calendar:
    - se all-day: start/end hanno "date"
    - altrimenti: "dateTime"
    """
    start = ev.get("start", {})
    end = ev.get("end", {})

    if "dateTime" in start:
        sdt = dt.datetime.fromisoformat(start["dateTime"])
    else:
        # all-day start date: 2026-01-07 -> 00:00
        sdt = dt.datetime.fromisoformat(start.get("date")).replace(hour=0, minute=0, second=0)

    if "dateTime" in end:
        edt = dt.datetime.fromisoformat(end["dateTime"])
    else:
        # all-day end date è esclusivo: metti 00:00 di quel giorno
        edt = dt.datetime.fromisoformat(end.get("date")).replace(hour=0, minute=0, second=0)

    if sdt.tzinfo is None:
        sdt = sdt.replace(tzinfo=tz)
    if edt.tzinfo is None:
        edt = edt.replace(tzinfo=tz)

    return sdt, edt

def slot_is_free(calendar_id: str, start: dt.datetime, end: dt.datetime, tz: dt.tzinfo) -> bool:
    """
    Libero se NON ci sono eventi che bloccano.
    Bloccante = transparency != 'transparent' (busy) oppure keyword nel summary.
    """
    evs = calendar().events().list(
        calendarId=calendar_id,
        timeMin=to_rfc3339(start),
        timeMax=to_rfc3339(end),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50
    ).execute().get("items", [])

    for ev in evs:
        summary = ev.get("summary", "") or ""
        transparency = ev.get("transparency", "")  # 'transparent' se non blocca

        if _has_block_keyword(summary):
            return False

        # se non è esplicitamente "transparent", lo consideriamo busy
        if transparency != "transparent":
            # in più, controlla overlap robusto (soprattutto per all-day)
            sdt, edt = event_to_datetime_range(ev, tz)
            if sdt < end and edt > start:
                return False

    return True

def create_booking_event(
    calendar_id: str,
    start: dt.datetime,
    end: dt.datetime,
    service_name: str,
    customer_name: str,
    customer_phone: str,
    shop_name: str,
    operator_name: str,
    booking_id: str,
    notes: str = ""
) -> str:
    summary = f"{service_name} – {customer_name}".strip(" –")

    description_lines = [
        f"Salone: {shop_name}",
        f"Operatore: {operator_name}",
        "",
        f"Cliente: {customer_name}",
        f"Telefono: {customer_phone}",
        f"Servizio: {service_name}",
    ]
    if notes:
        description_lines.append(f"Note: {notes}")
    description_lines += [
        "",
        f"Booking ID: {booking_id}",
    ]

    body = {
        "summary": summary,
        "description": "\n".join(description_lines),
        "start": {"dateTime": to_rfc3339(start)},
        "end": {"dateTime": to_rfc3339(end)},
        "transparency": "opaque",     # occupato
        "visibility": "private",      # lo staff con permessi vede i dettagli
        "extendedProperties": {
            "private": {
                "booking_id": booking_id,
                "customer_phone": norm_phone(customer_phone),
                "customer_name": customer_name,
                "service": service_name,
                "shop": shop_name,
                "operator": operator_name,
            }
        }
    }

    ev = calendar().events().insert(calendarId=calendar_id, body=body).execute()
    return ev.get("id", "")

# ============================================================
# CORE BOT LOGIC (multi-operatore + preferenze)
# ============================================================
def handle(shop: Dict, customer_phone: str, text: str) -> str:
    shop_id = shop.get("shop_id", "")
    tz = shop_tz(shop)
    customer_phone_n = norm_phone(customer_phone)

    # carica sessione persistente
    sess = load_session_from_sheet(shop_id, customer_phone_n) or {}
    state = sess.get("state", "")

    services = load_services(shop_id)
    hours = load_hours(shop_id)
    operators = load_operators(shop_id)

    slot_minutes = parse_int(shop.get("slot_minutes", ""), DEFAULT_SLOT_MINUTES)

    low = safe_lower(text)

    # 0) reset rapido
    if low in {"reset", "annulla", "cancella"}:
        clear_session_sheet(shop_id, customer_phone_n)
        return "Ok 👍 Ho azzerato la richiesta. Dimmi pure che servizio ti serve."

    # 1) greeting
    if low in {"ciao", "salve", "buongiorno", "buonasera"} and not sess:
        return (
            f"Ciao! 👋 Sono l’assistente di *{shop.get('name','il salone')}*.\n"
            "Dimmi pure che servizio ti serve 😊"
        )

    # 2) salva nome cliente se lo dice
    nm = parse_customer_name(text)
    if nm:
        sess["customer_name"] = nm

    # 3) se sta confermando slot proposto
    if state == "await_ok" and sess.get("slot") and sess.get("operator"):
        if low in {"ok", "va bene", "confermo", "si", "sì"}:
            service = sess["service"]
            start = dt.datetime.fromisoformat(sess["slot"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)

            dur = int(service.get("duration", 30))
            end = start + dt.timedelta(minutes=dur)

            op = sess["operator"]
            booking_id = sess.get("booking_id") or uuid.uuid4().hex[:10]
            customer_name = sess.get("customer_name") or "Cliente"

            create_booking_event(
                calendar_id=op["calendar_id"],
                start=start,
                end=end,
                service_name=service["name"],
                customer_name=customer_name,
                customer_phone=customer_phone_n,
                shop_name=shop.get("name", ""),
                operator_name=op.get("operator_name", ""),
                booking_id=booking_id,
                notes=sess.get("notes", "")
            )

            # aggiorna customer stats
            upsert_customer_after_booking(shop_id, customer_phone_n, service["name"], start)

            # pulisci sessione
            clear_session_sheet(shop_id, customer_phone_n)

            return (
                "Perfetto! ✅ Appuntamento confermato.\n\n"
                f"💈 *{service['name']}*\n"
                f"👤 Operatore: *{op.get('operator_name','') }*\n"
                f"🕒 {start.strftime('%a %d/%m %H:%M')}\n"
                f"🔖 Booking ID: {booking_id}\n\n"
                "A presto 😊"
            )

        if low in {"no", "non va", "cambia", "altro"}:
            # proponi alternative (passa a searching)
            sess["state"] = "searching"
            sess.pop("slot", None)
            sess.pop("operator", None)
            save_session_to_sheet(shop_id, customer_phone_n, "searching", sess)
            state = "searching"

    # 4) SERVIZIO
    if "service" not in sess:
        service = fuzzy_service(text, services)
        if service:
            sess["service"] = service
        else:
            lst = "\n".join(f"• {s['name']}" for s in services) if services else "• (nessun servizio configurato)"
            save_session_to_sheet(shop_id, customer_phone_n, "need_service", sess)
            return "Dimmi solo che servizio ti serve:\n" + lst

    # 5) DATA / ORARIO
    d = parse_date(text, shop)
    t = parse_time(text)
    a, b = parse_fascia(text)

    if d:
        sess["date"] = d.isoformat()
    if t:
        sess["time"] = t.strftime("%H:%M")
        # se mette un orario preciso, togli fascia
        sess.pop("after", None); sess.pop("before", None)
    if a and b and "time" not in sess:
        sess["after"] = a.strftime("%H:%M")
        sess["before"] = b.strftime("%H:%M")

    # 6) preferenza / rifiuto operatore
    if operators:
        pref = detect_operator_preference(text, operators)
        rej = detect_operator_rejection(text, operators)

        if pref:
            sess["preferred_operator_id"] = pref
            # se preferisco uno, reset esclusioni per evitare conflitti
            sess.pop("excluded_operator_ids", None)

        if rej:
            sess.setdefault("excluded_operator_ids", [])
            if rej not in sess["excluded_operator_ids"]:
                sess["excluded_operator_ids"].append(rej)
            # se stava preferendo proprio quello escluso, toglilo
            if sess.get("preferred_operator_id") == rej:
                sess.pop("preferred_operator_id", None)

    # salva stato parziale
    save_session_to_sheet(shop_id, customer_phone_n, "collecting", sess)

    if "date" not in sess:
        return (
            "Perfetto 👍\n"
            "Quando preferisci venire?\n"
            "(es. *domani*, *sabato*, *12/01*)"
        )

    if "time" not in sess and "after" not in sess:
        return "Preferisci *mattina*, *pomeriggio* o *sera*? 😊"

    if not operators:
        return (
            "Mi manca la configurazione degli operatori 😕\n"
            "Nel foglio Google, tab *operators*, aggiungi almeno un operatore con calendar_id."
        )

    # 7) Filtra operatori (preferenza / esclusioni)
    filtered_ops = operators[:]

    if sess.get("preferred_operator_id"):
        filtered_ops = [o for o in filtered_ops if o.get("operator_id") == sess["preferred_operator_id"]]

    if not sess.get("preferred_operator_id"):
        excluded = set(sess.get("excluded_operator_ids", []))
        if excluded:
            filtered_ops = [o for o in filtered_ops if o.get("operator_id") not in excluded]

    # se ha escluso tutti, resetta esclusioni
    if not filtered_ops:
        sess.pop("excluded_operator_ids", None)
        filtered_ops = operators[:]

    # 8) CERCA SLOT MIGLIORE
    service = sess["service"]
    dur = int(service.get("duration", 30))
    base = dt.date.fromisoformat(sess["date"])

    preferred_time = dt.time.fromisoformat(sess["time"]) if sess.get("time") else None
    after = dt.time.fromisoformat(sess["after"]) if sess.get("after") else None
    before = dt.time.fromisoformat(sess["before"]) if sess.get("before") else None

    def candidate_slots_for_day(day: dt.date) -> List[dt.datetime]:
        slots: List[dt.datetime] = []
        for st, en in hours.get(day.weekday(), []):
            sst = st
            een = en
            if after and sst < after:
                sst = after
            if before and een > before:
                een = before
            if sst >= een:
                continue

            if preferred_time:
                cand = dt.datetime.combine(day, preferred_time).replace(tzinfo=tz)
                if cand.time() >= sst and (cand + dt.timedelta(minutes=dur)).time() <= een:
                    return [cand]
                return []

            cur = dt.datetime.combine(day, sst).replace(tzinfo=tz)
            limit = dt.datetime.combine(day, een).replace(tzinfo=tz)
            while cur + dt.timedelta(minutes=dur) <= limit:
                slots.append(cur)
                cur += dt.timedelta(minutes=slot_minutes)
        return slots

    found: Optional[Tuple[dt.datetime, Dict]] = None

    for day_offset in range(MAX_LOOKAHEAD_DAYS):
        day = base + dt.timedelta(days=day_offset)
        day_slots = candidate_slots_for_day(day)
        if not day_slots:
            continue

        for slot_dt in day_slots:
            end_dt = slot_dt + dt.timedelta(minutes=dur)

            # se non ha preferenza: prova operatori in ordine priority
            for op in filtered_ops:
                cal_id = op.get("calendar_id")
                if not cal_id:
                    continue
                if slot_is_free(cal_id, slot_dt, end_dt, tz):
                    found = (slot_dt, op)
                    break
            if found:
                break
        if found:
            break

    if not found:
        # se preferiva uno specifico, proponi alternative (se esistono)
        if sess.get("preferred_operator_id"):
            preferred_name = ""
            for o in operators:
                if o.get("operator_id") == sess["preferred_operator_id"]:
                    preferred_name = o.get("operator_name", "")
                    break
            sess.pop("preferred_operator_id", None)
            save_session_to_sheet(shop_id, customer_phone_n, "collecting", sess)
            return (
                f"{preferred_name or 'L’operatore scelto'} non è disponibile in quella fascia 😕\n"
                "Vuoi che ti proponga il primo slot libero con un altro operatore?"
            )

        return (
            "Al momento non vedo disponibilità nei prossimi giorni 😕\n"
            "Vuoi provare un altro giorno o un’altra fascia?"
        )

    best_dt, best_op = found
    booking_id = uuid.uuid4().hex[:10]

    sess["slot"] = best_dt.isoformat()
    sess["operator"] = best_op
    sess["booking_id"] = booking_id
    sess["state"] = "await_ok"
    save_session_to_sheet(shop_id, customer_phone_n, "await_ok", sess)

    # messaggio diverso se preferiva/ha escluso qualcuno (più umano)
    extra = ""
    if sess.get("preferred_operator_id"):
        extra = f"Con *{best_op.get('operator_name','')}* 👍"
    elif sess.get("excluded_operator_ids"):
        extra = f"Ok, evito *{', '.join(sess['excluded_operator_ids'])}* 👍"

    return (
        "Ti propongo questo orario 👇\n\n"
        f"💈 *{service['name']}*\n"
        f"👤 Operatore: *{best_op.get('operator_name','') }*\n"
        f"🕒 {best_dt.strftime('%a %d/%m %H:%M')}\n"
        f"{extra}\n\n"
        "Va bene per te? Rispondi *OK* oppure dimmi un’altra preferenza 😊"
    )

# ============================================================
# ROUTE (test)
# ============================================================
@app.route("/test")
def test():
    phone = request.args.get("phone")          # numero del salone (shops.whatsapp_number / whatsapp_numb)
    customer = request.args.get("customer")    # numero cliente
    msg = request.args.get("msg", "")

    if not phone or not customer:
        return jsonify({"error": "Missing phone or customer"}), 400

    shop = load_shop(phone)
    if not shop:
        return jsonify({"error": "shop not found"}), 404

    reply = handle(shop, customer, msg)
    return jsonify({
        "shop": shop.get("name"),
        "shop_number": phone,
        "customer": customer,
        "message_in": msg,
        "bot_reply": reply
    })

@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
