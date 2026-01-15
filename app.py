from __future__ import annotations

import os, re, json, difflib, uuid, hmac, hashlib
import datetime as dt
from typing import Dict, List, Optional, Tuple, Set

import requests
from flask import Flask, request, jsonify

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

# ============================================================
# APP
# ============================================================
app = Flask(__name__)

# ============================================================
# ENV - GOOGLE
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")

# ============================================================
# ENV - META WHATSAPP CLOUD
# ============================================================
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN") or os.getenv("VERIFY_TOKEN", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")

# fallback (di solito NON viene usato, perché rispondiamo con il phone_number_id che arriva nel webhook)
META_PHONE_NUMBER_ID = (
    os.getenv("META_PHONE_NUMBER_ID")
    or os.getenv("PHONE_NUMBER_ID")
    or os.getenv("NUMERO_DI_TELEFONO", "")
)

META_APP_SECRET = os.getenv("META_APP_SECRET") or os.getenv("META_API_SECRET", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v20.0")

# ============================================================
# ENV - BOT SETTINGS
# ============================================================
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "30"))
MAX_LOOKAHEAD_DAYS = int(os.getenv("MAX_LOOKAHEAD_DAYS", "14"))
DEFAULT_SLOT_MINUTES = int(os.getenv("DEFAULT_SLOT_MINUTES", "30"))
BLOCK_KEYWORDS = {"chiuso", "ferie", "malattia", "off", "closed", "vacation", "sick"}

# >>> IMPORTANTISSIMO: per "per sempre", tienilo a 0 (default)
# Se >0 allora scadrebbe.
CUSTOMER_SHOP_TTL_DAYS = int(os.getenv("CUSTOMER_SHOP_TTL_DAYS", "0"))

# Tab customers (default: customers)
CUSTOMERS_TAB = os.getenv("CUSTOMERS_TAB", "customers")

# Se TRUE, salviamo customer_name e last_seen_phone_number_id su customers (colonne aggiunte se mancanti)
STORE_CUSTOMER_DEBUG_FIELDS = os.getenv("STORE_CUSTOMER_DEBUG_FIELDS", "true").strip().lower() in {
    "1", "true", "yes", "y", "si", "sì"
}

# ============================================================
# GOOGLE CLIENTS
# ============================================================
_sheets = None
_calendar = None


def _log(msg: str):
    print(msg, flush=True)


def creds():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON env var")
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID env var")
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


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_bool(v: str) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes", "y", "si", "sì"}


def parse_int(v: str, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def norm_text(v: str) -> str:
    return (v or "").strip()


def safe_lower(v: str) -> str:
    return norm_text(v).lower()


def _iso_time(t: dt.time) -> str:
    return t.strftime("%H:%M")


def _is_affirmative(t: str) -> bool:
    low = safe_lower(t)
    return low in {"ok", "va bene", "confermo", "si", "sì", "1"}


def _is_second_choice(t: str) -> bool:
    return safe_lower(t) == "2"


def shop_tz(shop: Dict) -> dt.tzinfo:
    tz_name = norm_text(shop.get("timezone")) or "UTC"
    if ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return dt.timezone.utc
    return dt.timezone.utc


def utc_now_iso() -> str:
    return now().replace(microsecond=0).isoformat()


def parse_iso_dt(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def safe_values_get(a1: str) -> List[List[str]]:
    """Get values from Sheets without crashing the whole webhook."""
    try:
        res = sheets().spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=a1
        ).execute()
        return res.get("values", []) or []
    except HttpError as e:
        _log(f"[SHEETS] values.get failed for {a1}: {e}")
        return []
    except Exception as e:
        _log(f"[SHEETS] values.get error for {a1}: {e}")
        return []


# ============================================================
# C2: SHOP=... nel primo messaggio (QR/link)
# ============================================================
def extract_shop_hint(text: str) -> Optional[str]:
    m = re.search(r"\bSHOP\s*=\s*([A-Za-z0-9_\-]+)\b", text or "", flags=re.I)
    return m.group(1) if m else None


def strip_shop_hint(text: str) -> str:
    return re.sub(r"\bSHOP\s*=\s*[A-Za-z0-9_\-]+\b", "", text or "", flags=re.I).strip()


# ============================================================
# DATE / TIME PARSING
# ============================================================
def parse_date(text: str) -> Optional[dt.date]:
    t = safe_lower(text)
    today = dt.date.today()

    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)

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
    if "tardo" in t or "sera" in t:
        return dt.time(17, 0), dt.time(21, 0)
    return None, None


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
    return None


# ============================================================
# SHEETS LOADERS
# ============================================================
def load_tab(tab: str) -> List[Dict]:
    rows = safe_values_get(f"{tab}!A:Z")
    if not rows:
        return []
    headers = rows[0]
    out = []
    for r in rows[1:]:
        row = dict(zip(headers, r + [""] * (len(headers) - len(r))))
        out.append(row)
    return out


def get_shop_by_id(shop_id: str) -> Optional[Dict]:
    sid = norm_text(shop_id)
    if not sid:
        return None
    for s in load_tab("shops"):
        if norm_text(s.get("shop_id")) == sid:
            return s
    return None


def load_shop_auto(display_phone_number: str, phone_number_id: str) -> Optional[Dict]:
    """
    - Se phone_number_id identifica UNO shop (numero dedicato) -> ok
    - Altrimenti (numero condiviso) non scegliere qui: useremo customers / SHOP=
    """
    pnid = norm_text(phone_number_id)
    disp = norm_phone(display_phone_number)
    shops = load_tab("shops")

    if pnid:
        matches = [s for s in shops if norm_text(s.get("phone_number_id")) == pnid]
        if len(matches) == 1:
            return matches[0]

    # fallback (utile se hai 1 solo shop su quel display number)
    if disp:
        matches = [s for s in shops if norm_phone(s.get("whatsapp_number")) == disp]
        if len(matches) == 1:
            return matches[0]

    return None


# ============================================================
# customers tab: cliente -> shop + ultimo servizio (persistente)
# (compatibile con il tuo foglio: shop_id | phone | last_service | total_visits | last_visit)
# ============================================================
def _get_customers_values() -> List[List[str]]:
    return safe_values_get(f"{CUSTOMERS_TAB}!A:Z")


def _update_customers_range(a1: str, values: List[List[str]]):
    sheets().spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=a1,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def _append_customers_row(values: List[str]):
    sheets().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{CUSTOMERS_TAB}!A:Z",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()


def _ensure_columns(header: List[str], needed: List[str]) -> Tuple[List[str], Dict[str, int], bool]:
    col = {h: i for i, h in enumerate(header)}
    changed = False
    for n in needed:
        if n not in col:
            header.append(n)
            col[n] = len(header) - 1
            changed = True
    return header, col, changed


def _ensure_customers_header() -> Tuple[List[str], Dict[str, int]]:
    """
    Garantisce che esista l'header e che contenga le colonne minime.
    Mantiene l'ordine esistente e aggiunge solo colonne mancanti.
    """
    values = _get_customers_values()

    # se tab vuota, crea header come nel tuo screenshot
    if not values:
        header = ["shop_id", "phone", "last_service", "total_visits", "last_visit"]
        if STORE_CUSTOMER_DEBUG_FIELDS:
            header += ["customer_name", "last_seen_phone_number_id", "updated_at"]
        else:
            header += ["updated_at"]
        _update_customers_range(f"{CUSTOMERS_TAB}!A1:Z1", [header])
        values = _get_customers_values()

    if not values:
        return (["shop_id", "phone", "last_service", "total_visits", "last_visit", "updated_at"],
                {"shop_id": 0, "phone": 1, "last_service": 2, "total_visits": 3, "last_visit": 4, "updated_at": 5})

    header = values[0]
    needed = ["shop_id", "phone", "last_service", "total_visits", "last_visit", "updated_at"]
    if STORE_CUSTOMER_DEBUG_FIELDS:
        needed += ["customer_name", "last_seen_phone_number_id"]

    header, col, changed = _ensure_columns(header, needed)
    if changed:
        _update_customers_range(f"{CUSTOMERS_TAB}!A1:Z1", [header])

    col = {h: i for i, h in enumerate(header)}
    return header, col


def get_customer_shop_id(customer_phone: str) -> Optional[str]:
    """
    Restituisce shop_id salvato per quel numero.
    Se CUSTOMER_SHOP_TTL_DAYS = 0 -> NON SCADRA' MAI (per sempre).
    """
    phone = norm_phone(customer_phone)
    if not phone:
        return None

    rows = load_tab(CUSTOMERS_TAB)
    for r in rows:
        if norm_phone(r.get("phone")) != phone:
            continue

        sid = norm_text(r.get("shop_id"))
        if not sid:
            return None

        if CUSTOMER_SHOP_TTL_DAYS > 0:
            ts = parse_iso_dt(r.get("updated_at") or "")
            if ts:
                age_days = (now() - ts).total_seconds() / 86400.0
                if age_days > CUSTOMER_SHOP_TTL_DAYS:
                    return None

        return sid

    return None


def get_customer_last_service(customer_phone: str) -> Optional[str]:
    phone = norm_phone(customer_phone)
    if not phone:
        return None
    rows = load_tab(CUSTOMERS_TAB)
    for r in rows:
        if norm_phone(r.get("phone")) != phone:
            continue
        return norm_text(r.get("last_service"))
    return None


def upsert_customer_shop(
    customer_phone: str,
    shop_id: str,
    *,
    customer_name: Optional[str] = None,
    last_seen_phone_number_id: Optional[str] = None,
    touch_updated_at: bool = True,
):
    """
    Salva/aggiorna mapping phone -> shop_id nel tab customers.
    Non cancella campi esistenti (last_service, ecc.), aggiorna solo mapping + debug fields se presenti.
    """
    phone = norm_phone(customer_phone)
    sid = norm_text(shop_id)
    if not phone or not sid:
        return

    # evita update inutile se già uguale
    current = None
    try:
        current = get_customer_shop_id(phone)
    except Exception:
        current = None
    if current == sid and not (STORE_CUSTOMER_DEBUG_FIELDS and (customer_name or last_seen_phone_number_id)):
        return

    header, col = _ensure_customers_header()
    values = _get_customers_values()
    if not values:
        return

    updated_at = utc_now_iso()

    # cerca riga cliente (unica per phone)
    target_row = None  # 1-based
    for i in range(1, len(values)):
        row = values[i]
        p = row[col["phone"]] if col["phone"] < len(row) else ""
        if norm_phone(p) == phone:
            target_row = i + 1
            break

    def _pad(row: List[str]) -> List[str]:
        return row + [""] * (len(header) - len(row))

    if target_row:
        row = _pad(values[target_row - 1])
        row[col["phone"]] = phone
        row[col["shop_id"]] = sid
        if touch_updated_at:
            row[col["updated_at"]] = updated_at
        if STORE_CUSTOMER_DEBUG_FIELDS:
            if customer_name and "customer_name" in col:
                row[col["customer_name"]] = customer_name
            if last_seen_phone_number_id and "last_seen_phone_number_id" in col:
                row[col["last_seen_phone_number_id"]] = last_seen_phone_number_id
        _update_customers_range(f"{CUSTOMERS_TAB}!A{target_row}:Z{target_row}", [row])
    else:
        new_row = [""] * len(header)
        new_row[col["phone"]] = phone
        new_row[col["shop_id"]] = sid
        if touch_updated_at:
            new_row[col["updated_at"]] = updated_at
        # init visits a 0
        if "total_visits" in col:
            new_row[col["total_visits"]] = "0"
        if STORE_CUSTOMER_DEBUG_FIELDS:
            if customer_name and "customer_name" in col:
                new_row[col["customer_name"]] = customer_name
            if last_seen_phone_number_id and "last_seen_phone_number_id" in col:
                new_row[col["last_seen_phone_number_id"]] = last_seen_phone_number_id
        _append_customers_row(new_row)


def update_customer_after_booking(
    customer_phone: str,
    shop_id: str,
    service_name: str,
    start_dt: dt.datetime,
    *,
    customer_name: Optional[str] = None,
    last_seen_phone_number_id: Optional[str] = None,
):
    """
    Dopo conferma appuntamento:
    - garantisce mapping phone->shop_id (per sempre)
    - last_service = service_name
    - total_visits += 1
    - last_visit = ISO datetime (con tz dello start_dt)
    - updated_at = UTC ISO
    - (opzionale) customer_name / last_seen_phone_number_id
    """
    phone = norm_phone(customer_phone)
    sid = norm_text(shop_id)
    if not phone or not sid:
        return

    header, col = _ensure_customers_header()
    values = _get_customers_values()
    if not values:
        return

    updated_at = utc_now_iso()
    last_visit = start_dt.replace(microsecond=0).isoformat()

    target_row = None
    for i in range(1, len(values)):
        row = values[i]
        p = row[col["phone"]] if col["phone"] < len(row) else ""
        if norm_phone(p) == phone:
            target_row = i + 1
            break

    def _pad(row: List[str]) -> List[str]:
        return row + [""] * (len(header) - len(row))

    if not target_row:
        new_row = [""] * len(header)
        new_row[col["shop_id"]] = sid
        new_row[col["phone"]] = phone
        new_row[col["last_service"]] = service_name
        new_row[col["last_visit"]] = last_visit
        new_row[col["updated_at"]] = updated_at
        new_row[col["total_visits"]] = "1" if "total_visits" in col else "1"
        if STORE_CUSTOMER_DEBUG_FIELDS:
            if customer_name and "customer_name" in col:
                new_row[col["customer_name"]] = customer_name
            if last_seen_phone_number_id and "last_seen_phone_number_id" in col:
                new_row[col["last_seen_phone_number_id"]] = last_seen_phone_number_id
        _append_customers_row(new_row)
        return

    row = _pad(values[target_row - 1])

    # mapping (per sempre)
    row[col["shop_id"]] = sid
    row[col["phone"]] = phone

    # stats
    if "last_service" in col:
        row[col["last_service"]] = service_name
    if "last_visit" in col:
        row[col["last_visit"]] = last_visit
    if "updated_at" in col:
        row[col["updated_at"]] = updated_at

    if "total_visits" in col:
        cur_tv = row[col["total_visits"]] if col["total_visits"] < len(row) else "0"
        try:
            tv = int(str(cur_tv).strip() or "0")
        except Exception:
            tv = 0
        row[col["total_visits"]] = str(tv + 1)

    if STORE_CUSTOMER_DEBUG_FIELDS:
        if customer_name and "customer_name" in col:
            row[col["customer_name"]] = customer_name
        if last_seen_phone_number_id and "last_seen_phone_number_id" in col:
            row[col["last_seen_phone_number_id"]] = last_seen_phone_number_id

    _update_customers_range(f"{CUSTOMERS_TAB}!A{target_row}:Z{target_row}", [row])


# ============================================================
# LOAD SHOP DATA
# ============================================================
def load_services(shop_id: str) -> List[Dict]:
    return [
        {
            **s,
            "duration": parse_int(s.get("duration", "30"), 30),
            "active": parse_bool(s.get("active", "TRUE")),
        }
        for s in load_tab("services")
        if s.get("shop_id") == shop_id and parse_bool(s.get("active", "TRUE"))
    ]


def load_hours(shop_id: str) -> Dict[int, List[Tuple[dt.time, dt.time]]]:
    out = {i: [] for i in range(7)}
    for r in load_tab("hours"):
        if r.get("shop_id") == shop_id:
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
            "operator_id": norm_text(r.get("operator_id")),
            "operator_name": norm_text(r.get("operator_name")) or norm_text(r.get("operator_id")),
            "calendar_id": norm_text(r.get("calendar_id")),
            "priority": parse_int(r.get("priority", ""), 9999),
        })
    ops.sort(key=lambda x: (x["priority"], safe_lower(x["operator_name"])))
    return ops


# ============================================================
# SESSION (memoria breve) - in-memory
# ============================================================
SESSIONS: Dict[str, Dict] = {}


def get_session(key: str) -> Dict:
    s = SESSIONS.get(key)
    if not s:
        return {}
    if (now() - s["ts"]).total_seconds() / 60 > SESSION_TTL_MINUTES:
        del SESSIONS[key]
        return {}
    return dict(s)


def save_session(key: str, data: Dict):
    SESSIONS[key] = {"ts": now(), **data}


def clear_session(key: str):
    if key in SESSIONS:
        del SESSIONS[key]


# ============================================================
# DEDUP message ids (anti doppia risposta)
# ============================================================
PROCESSED_MSG_IDS: Dict[str, dt.datetime] = {}


def _gc_processed(ttl_minutes: int = 60):
    cut = now() - dt.timedelta(minutes=ttl_minutes)
    for k, ts in list(PROCESSED_MSG_IDS.items()):
        if ts < cut:
            del PROCESSED_MSG_IDS[k]


def seen_message(message_id: str) -> bool:
    _gc_processed()
    if not message_id:
        return False
    if message_id in PROCESSED_MSG_IDS:
        return True
    PROCESSED_MSG_IDS[message_id] = now()
    return False


# ============================================================
# OPERATOR PREFERENCES
# ============================================================
def operator_label(op: Dict) -> str:
    return op.get("operator_name") or op.get("operator_id") or "Operatore"


def _operator_tokens(op: Dict) -> List[str]:
    toks = []
    if op.get("operator_name"):
        toks.append(safe_lower(op["operator_name"]))
    if op.get("operator_id"):
        toks.append(safe_lower(op["operator_id"]))
    return list({t for t in toks if t})


def parse_operator_prefs(text: str, operators: List[Dict]) -> Tuple[Optional[str], Set[str]]:
    t = " " + safe_lower(text) + " "
    preferred: Optional[str] = None
    excluded: Set[str] = set()
    neg_markers = [" non ", " senza ", " no ", " evita ", " non voglio "]

    for op in operators:
        op_id = op.get("operator_id")
        if not op_id:
            continue
        for tok in _operator_tokens(op):
            for nm in neg_markers:
                if nm + tok + " " in t or nm + tok + "." in t or nm + tok + "," in t:
                    excluded.add(op_id)
            if f" con {tok} " in t or f" da {tok} " in t or f" voglio {tok} " in t or f" preferisco {tok} " in t:
                preferred = op_id

    if preferred and preferred in excluded:
        preferred = None
    return preferred, excluded


# ============================================================
# CALENDAR HELPERS
# ============================================================
def _has_block_keyword(summary: str) -> bool:
    s = safe_lower(summary)
    return any(k in s for k in BLOCK_KEYWORDS)


def slot_is_free(calendar_id: str, start: dt.datetime, end: dt.datetime) -> bool:
    evs = calendar().events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50
    ).execute().get("items", [])

    for ev in evs:
        summary = ev.get("summary", "")
        transparency = ev.get("transparency", "")
        if _has_block_keyword(summary):
            return False
        if transparency != "transparent":
            return False
    return True


def find_event_by_booking_key(calendar_id: str, start: dt.datetime, end: dt.datetime, booking_key: str) -> Optional[Dict]:
    buf_start = (start - dt.timedelta(minutes=5)).isoformat()
    buf_end = (end + dt.timedelta(minutes=5)).isoformat()
    evs = calendar().events().list(
        calendarId=calendar_id,
        timeMin=buf_start,
        timeMax=buf_end,
        singleEvents=True,
        orderBy="startTime",
        maxResults=50
    ).execute().get("items", [])
    for ev in evs:
        ep = (ev.get("extendedProperties") or {}).get("private") or {}
        if ep.get("booking_key") == booking_key:
            return ev
    return None


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
    booking_key: str,
    notes: str = ""
) -> str:
    existing = find_event_by_booking_key(calendar_id, start, end, booking_key)
    if existing:
        return existing.get("id", "")

    summary = f"{service_name} – {customer_name}".strip(" –")

    description_lines = [
        f"Attività: {shop_name}",
        f"Operatore: {operator_name}",
        "",
        f"Cliente: {customer_name}",
        f"Telefono: {customer_phone}",
        f"Servizio: {service_name}",
    ]
    if notes:
        description_lines.append(f"Note: {notes}")
    description_lines += ["", f"Booking ID: {booking_id}"]

    body = {
        "summary": summary,
        "description": "\n".join(description_lines),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "transparency": "opaque",
        "visibility": "private",
        "extendedProperties": {
            "private": {
                "booking_id": booking_id,
                "booking_key": booking_key,
                "customer_phone": customer_phone,
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
# SEARCH
# ============================================================
def find_best_slots(
    hours: Dict[int, List[Tuple[dt.time, dt.time]]],
    operators: List[Dict],
    base_date: dt.date,
    dur_min: int,
    slot_minutes: int,
    preferred_time: Optional[dt.time],
    after: Optional[dt.time],
    before: Optional[dt.time],
    preferred_operator_id: Optional[str],
    excluded_operator_ids: Set[str],
    tz: dt.tzinfo,
    limit: int = 2,
) -> List[Tuple[dt.datetime, Dict]]:
    ops_by_id = {op.get("operator_id"): op for op in operators if op.get("operator_id")}

    def op_order() -> List[Dict]:
        ordered = []
        if preferred_operator_id and preferred_operator_id in ops_by_id and preferred_operator_id not in excluded_operator_ids:
            ordered.append(ops_by_id[preferred_operator_id])
        for op in operators:
            oid = op.get("operator_id")
            if not oid or oid in excluded_operator_ids:
                continue
            if preferred_operator_id and oid == preferred_operator_id:
                continue
            ordered.append(op)
        return ordered

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
                cand = dt.datetime.combine(day, preferred_time, tzinfo=tz)
                if cand.time() >= sst and (cand + dt.timedelta(minutes=dur_min)).time() <= een:
                    return [cand]
                return []

            cur = dt.datetime.combine(day, sst, tzinfo=tz)
            limit_dt = dt.datetime.combine(day, een, tzinfo=tz)
            while cur + dt.timedelta(minutes=dur_min) <= limit_dt:
                slots.append(cur)
                cur += dt.timedelta(minutes=slot_minutes)
        return slots

    ordered_ops = op_order()
    results: List[Tuple[dt.datetime, Dict]] = []

    for day_offset in range(MAX_LOOKAHEAD_DAYS):
        day = base_date + dt.timedelta(days=day_offset)
        day_slots = candidate_slots_for_day(day)
        if not day_slots:
            continue

        for slot_dt in day_slots:
            end_dt = slot_dt + dt.timedelta(minutes=dur_min)
            for op in ordered_ops:
                cal_id = op.get("calendar_id")
                if not cal_id:
                    continue
                if slot_is_free(cal_id, slot_dt, end_dt):
                    results.append((slot_dt, op))
                    if len(results) >= limit:
                        return results
                    break

    return results


# ============================================================
# CORE BOT LOGIC
# - Invariato, ma: dopo booking aggiorniamo customers (shop + last_service + visits + last_visit)
# ============================================================
def handle(shop: Dict, customer_phone: str, text: str, customer_name: Optional[str] = None, *, last_seen_phone_number_id: Optional[str] = None) -> str:
    shop_id = shop["shop_id"]
    key = f"{shop_id}:{norm_phone(customer_phone)}"
    sess = get_session(key)

    services = load_services(shop_id)
    hours = load_hours(shop_id)
    operators = load_operators(shop_id)

    slot_minutes = parse_int(shop.get("slot_minutes", ""), DEFAULT_SLOT_MINUTES)
    low = safe_lower(text)

    if customer_name and "customer_name" not in sess:
        sess["customer_name"] = customer_name

    if low in {"reset", "annulla", "cancella"}:
        clear_session(key)
        return "Ok 👍 Ho azzerato la richiesta. Dimmi che servizio ti serve."

    # Saluto: se abbiamo info last_service, la citiamo (bella UX)
    if low in {"ciao", "salve", "buongiorno", "buonasera"} and not sess:
        last_srv = None
        try:
            last_srv = get_customer_last_service(customer_phone)
        except Exception:
            last_srv = None

        if last_srv:
            return (
                f"Ciao! 👋 Sono l’assistente di *{shop.get('name','l’attività')}*.\n"
                f"Ho visto che l’ultima volta hai fatto: *{last_srv}*.\n"
                "Dimmi pure che servizio ti serve 😊"
            )

        return (
            f"Ciao! 👋 Sono l’assistente di *{shop.get('name','l’attività')}*.\n"
            "Dimmi pure che servizio ti serve 😊"
        )

    if operators:
        pref, excl = parse_operator_prefs(text, operators)
        if pref:
            sess["preferred_operator_id"] = pref
        if excl:
            cur_excl = set(sess.get("excluded_operator_ids") or [])
            cur_excl |= set(excl)
            sess["excluded_operator_ids"] = list(cur_excl)

    if sess.get("state") == "await_choice" and sess.get("options"):
        if _is_affirmative(text) or _is_second_choice(text):
            idx = 0 if _is_affirmative(text) else 1
            if idx >= len(sess["options"]):
                idx = 0

            opt = sess["options"][idx]
            start = dt.datetime.fromisoformat(opt["slot"])
            op = opt["operator"]
            service = sess["service"]
            dur = int(service.get("duration", 30))
            end = start + dt.timedelta(minutes=dur)

            booking_id = sess.get("booking_id") or uuid.uuid4().hex[:10]
            cname = sess.get("customer_name") or "Cliente"

            bk_raw = f"{shop_id}|{norm_phone(customer_phone)}|{service.get('name','')}|{start.isoformat()}"
            booking_key = uuid.uuid5(uuid.NAMESPACE_URL, bk_raw).hex

            create_booking_event(
                calendar_id=op["calendar_id"],
                start=start,
                end=end,
                service_name=service["name"],
                customer_name=cname,
                customer_phone=customer_phone,
                shop_name=shop.get("name", ""),
                operator_name=op.get("operator_name", ""),
                booking_id=booking_id,
                booking_key=booking_key,
                notes=sess.get("notes", "")
            )

            # ✅ Aggiorna customers (per sempre + ultimo servizio)
            try:
                update_customer_after_booking(
                    customer_phone=customer_phone,
                    shop_id=shop_id,
                    service_name=service["name"],
                    start_dt=start,
                    customer_name=customer_name,
                    last_seen_phone_number_id=last_seen_phone_number_id,
                )
            except Exception as e:
                _log(f"[CUSTOMERS] update after booking failed: {e}")

            clear_session(key)
            return (
                "Perfetto! ✅ Appuntamento confermato.\n\n"
                f"🔧 *{service['name']}*\n"
                f"👤 Con: *{operator_label(op)}*\n"
                f"🕒 {start.strftime('%a %d/%m %H:%M')}\n"
                f"🔖 Booking ID: {booking_id}\n\n"
                "A presto 😊"
            )

        if ("non " in low) or ("senza " in low) or low in {"no", "cambia", "altro"}:
            first_op = sess["options"][0]["operator"]
            oid = first_op.get("operator_id")
            if oid:
                cur_excl = set(sess.get("excluded_operator_ids") or [])
                cur_excl.add(oid)
                sess["excluded_operator_ids"] = list(cur_excl)

            sess["state"] = "searching"
            sess.pop("options", None)
            save_session(key, sess)

    if "service" not in sess:
        service = fuzzy_service(text, services)
        if service:
            sess["service"] = service
            save_session(key, sess)
        else:
            lst = "\n".join(f"• {s['name']}" for s in services) if services else "• (nessun servizio configurato)"
            return "Dimmi solo che servizio ti serve:\n" + lst

    d = parse_date(text)
    t = parse_time(text)
    a, b = parse_fascia(text)

    if d:
        sess["date"] = d.isoformat()
    if t:
        sess["time"] = _iso_time(t)
    if a and b:
        sess["after"] = _iso_time(a)
        sess["before"] = _iso_time(b)

    save_session(key, sess)

    if "date" not in sess:
        return "Perfetto 👍 Quando preferisci? (es. *domani* oppure *12/01*)"

    if "time" not in sess and "after" not in sess:
        return "Preferisci *mattina*, *pomeriggio* o *sera*? 😊"

    if not operators:
        return (
            "Mi manca la configurazione degli operatori 😕\n"
            "Nel foglio Google, tab *operators*, aggiungi almeno un operatore con calendar_id."
        )

    service = sess["service"]
    dur = int(service.get("duration", 30))
    base = dt.date.fromisoformat(sess["date"])

    preferred_time = dt.time.fromisoformat(sess["time"]) if sess.get("time") else None
    after = dt.time.fromisoformat(sess["after"]) if sess.get("after") else None
    before = dt.time.fromisoformat(sess["before"]) if sess.get("before") else None

    preferred_operator_id = sess.get("preferred_operator_id")
    excluded_operator_ids = set(sess.get("excluded_operator_ids") or [])

    tz = shop_tz(shop)

    options = find_best_slots(
        hours=hours,
        operators=operators,
        base_date=base,
        dur_min=dur,
        slot_minutes=slot_minutes,
        preferred_time=preferred_time,
        after=after,
        before=before,
        preferred_operator_id=preferred_operator_id,
        excluded_operator_ids=excluded_operator_ids,
        tz=tz,
        limit=2
    )

    if not options:
        return (
            "Al momento non vedo disponibilità nei prossimi giorni 😕\n"
            "Vuoi provare un altro giorno o un’altra fascia?"
        )

    packed = []
    for slot_dt, op in options:
        packed.append({"slot": slot_dt.isoformat(), "operator": op})

    sess["options"] = packed
    sess["state"] = "await_choice"
    sess["booking_id"] = sess.get("booking_id") or uuid.uuid4().hex[:10]
    save_session(key, sess)

    msg = "Ti propongo questi orari 👇\n\n"
    slot1, op1 = options[0]
    msg += f"1) 🕒 {slot1.strftime('%a %d/%m %H:%M')} — con *{operator_label(op1)}*\n"
    if len(options) > 1:
        slot2, op2 = options[1]
        msg += f"2) 🕒 {slot2.strftime('%a %d/%m %H:%M')} — con *{operator_label(op2)}*\n"

    msg += "\nRispondi *1* o *2* (oppure *OK* per confermare la 1).\n"
    msg += "Se vuoi un operatore specifico scrivi: *con Marco* oppure *non Marco* 😊"
    return msg


# ============================================================
# META SIGNATURE VERIFY
# ============================================================
def verify_meta_signature(req) -> bool:
    if not META_APP_SECRET:
        return True

    sig = req.headers.get("X-Hub-Signature-256", "")
    if not sig.startswith("sha256="):
        return False

    their = sig.split("=", 1)[1].strip()
    mac = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        msg=req.get_data(),
        digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(mac, their)


# ============================================================
# WHATSAPP SEND
# ============================================================
def wa_send_text(to_phone: str, text: str, phone_number_id: Optional[str] = None):
    pid = (phone_number_id or "").strip() or META_PHONE_NUMBER_ID
    if not pid:
        raise RuntimeError("Missing META_PHONE_NUMBER_ID / PHONE_NUMBER_ID env var")
    if not META_ACCESS_TOKEN:
        raise RuntimeError("Missing META_ACCESS_TOKEN / WHATSAPP_TOKEN env var")

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{pid}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": norm_phone(to_phone),
        "type": "text",
        "text": {"body": text},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"WhatsApp send failed: {r.status_code} {r.text}")


def _is_meta_sample_payload(display_phone: str, phone_number_id: str) -> bool:
    return norm_phone(display_phone) == "16505551111" or norm_text(phone_number_id) == "123456123"


# ============================================================
# ROUTES
# ============================================================
@app.route("/", methods=["GET"])
def home():
    return "OK - WhatsApp Bot online ✅", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token and token == META_VERIFY_TOKEN:
            return (challenge or ""), 200

        return "Forbidden", 403

    if not verify_meta_signature(request):
        return "Invalid signature", 403

    data = request.get_json(silent=True) or {}

    try:
        entries = data.get("entry", []) or []
        for entry in entries:
            changes = entry.get("changes", []) or []
            for ch in changes:
                value = ch.get("value", {}) or {}
                metadata = value.get("metadata", {}) or {}

                display_phone_number = metadata.get("display_phone_number", "") or ""
                phone_number_id = (metadata.get("phone_number_id") or "").strip()

                messages = value.get("messages", []) or []
                contacts = value.get("contacts", []) or []
                contact_name = None
                if contacts:
                    profile = (contacts[0].get("profile") or {})
                    contact_name = profile.get("name")

                for m in messages:
                    msg_id = m.get("id", "")
                    if msg_id and seen_message(msg_id):
                        continue

                    from_phone = m.get("from", "")
                    mtype = m.get("type", "")

                    _log(f"[WEBHOOK] msg_id={msg_id} from={from_phone} type={mtype} display_phone={display_phone_number} phone_number_id={phone_number_id}")

                    # sample payload -> NO reply, NO side effects
                    if _is_meta_sample_payload(display_phone_number, phone_number_id):
                        _log("[WEBHOOK] Meta sample payload detected -> skip.")
                        continue

                    if mtype != "text":
                        try:
                            wa_send_text(from_phone, "Per ora gestisco solo messaggi di testo 🙂", phone_number_id=phone_number_id)
                        except Exception as e:
                            _log(f"[SEND] non-text reply failed: {e}")
                        continue

                    text = ((m.get("text") or {}).get("body")) or ""

                    # 0) Se arriva SHOP=..., salva mapping persistente (per sempre)
                    hint = extract_shop_hint(text)
                    if hint:
                        hinted_shop = get_shop_by_id(hint)
                        if hinted_shop:
                            try:
                                upsert_customer_shop(
                                    from_phone,
                                    hint,
                                    customer_name=contact_name,
                                    last_seen_phone_number_id=phone_number_id,
                                    touch_updated_at=True
                                )
                            except Exception as e:
                                _log(f"[CUSTOMERS] upsert from hint failed: {e}")

                            text_clean = strip_shop_hint(text)
                            if not text_clean:
                                wa_send_text(
                                    from_phone,
                                    f"Perfetto ✅ Sei connesso a *{hinted_shop.get('name','questa sede')}*.\nDimmi che servizio ti serve 😊",
                                    phone_number_id=phone_number_id
                                )
                                continue
                            text = text_clean

                    # 1) Prova a recuperare shop dal mapping cliente->shop (customers)
                    saved_shop_id = None
                    try:
                        saved_shop_id = get_customer_shop_id(from_phone)
                    except Exception as e:
                        _log(f"[CUSTOMERS] get_customer_shop_id failed: {e}")

                    shop = get_shop_by_id(saved_shop_id) if saved_shop_id else None

                    # 2) Se non c'è mapping, prova auto-detect (numero dedicato)
                    auto_shop = None
                    if not shop:
                        auto_shop = load_shop_auto(display_phone_number, phone_number_id)
                        shop = auto_shop

                    # 2b) Se auto-detect ha trovato shop, salva subito mapping (così tra mesi se lo ricorda)
                    if auto_shop and auto_shop.get("shop_id"):
                        try:
                            upsert_customer_shop(
                                from_phone,
                                auto_shop["shop_id"],
                                customer_name=contact_name,
                                last_seen_phone_number_id=phone_number_id,
                                touch_updated_at=True
                            )
                        except Exception as e:
                            _log(f"[CUSTOMERS] upsert from auto-detect failed: {e}")

                    # 3) Se ancora niente, chiedi QR/link
                    if not shop:
                        wa_send_text(
                            from_phone,
                            "Per iniziare, usa il QR/link del negozio (contiene `SHOP=...`).\n"
                            "Esempio: `SHOP=barber_test_3 taglio domani`",
                            phone_number_id=phone_number_id
                        )
                        continue

                    # (Opzionale) se lo shop è già noto da mapping, aggiorna debug fields senza cambiare shop
                    try:
                        if shop and shop.get("shop_id"):
                            upsert_customer_shop(
                                from_phone,
                                shop["shop_id"],
                                customer_name=contact_name,
                                last_seen_phone_number_id=phone_number_id,
                                touch_updated_at=True
                            )
                    except Exception as e:
                        _log(f"[CUSTOMERS] touch failed: {e}")

                    reply = handle(
                        shop,
                        from_phone,
                        text,
                        customer_name=contact_name,
                        last_seen_phone_number_id=phone_number_id
                    )

                    try:
                        wa_send_text(from_phone, reply, phone_number_id=phone_number_id)
                    except Exception as e:
                        _log(f"[SEND] reply failed: {e}")

    except Exception as e:
        _log(f"[WEBHOOK] processing error: {e}")

    return "OK", 200


@app.route("/test", methods=["GET"])
def test():
    phone = request.args.get("phone")
    customer = request.args.get("customer")
    msg = request.args.get("msg", "")

    if not phone or not customer:
        return jsonify({"error": "missing phone or customer"}), 400

    shop = load_shop_auto(phone, "")
    if not shop:
        return jsonify({"error": "shop not found"}), 404

    reply = handle(shop, customer, msg)
    return jsonify({
        "shop": shop.get("name"),
        "shop_number": phone,
        "customer": customer,
        "message_in": msg,
        "bot_reply": reply
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
