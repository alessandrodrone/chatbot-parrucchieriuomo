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


APP = Flask(__name__)

# =========================
# ENV
# =========================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "30"))

# =========================
# SHEETS + CALENDAR clients
# =========================
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


# =========================
# Small cache for config tabs (reduce API calls)
# =========================
_CACHE: Dict[str, Dict[str, Any]] = {}  # key -> {"ts": datetime, "data": ...}
CACHE_TTL_SECONDS = 20

def cache_get(key: str):
    item = _CACHE.get(key)
    if not item:
        return None
    if (dt.datetime.utcnow() - item["ts"]).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return item["data"]

def cache_set(key: str, data: Any):
    _CACHE[key] = {"ts": dt.datetime.utcnow(), "data": data}


# =========================
# Helpers: phone normalization
# =========================
def norm_phone(p: str) -> str:
    """
    Normalizza:
    - 'whatsapp:+39348...' -> '39348...'
    - '+39 348...' -> '39348...'
    - '348...' -> '348...'
    """
    if not p:
        return ""
    p = p.strip()
    p = p.replace("whatsapp:", "").strip()
    # tieni solo cifre
    digits = re.sub(r"\D+", "", p)
    # rimuovi eventuali zeri strani davanti (non aggressivo)
    digits = digits.lstrip("0") or digits
    return digits

def phone_matches(a: str, b: str) -> bool:
    """
    Match robusto:
    - confronta digits
    - accetta sia con che senza prefisso 39 se uno dei due è IT
    """
    da = norm_phone(a)
    db = norm_phone(b)
    if not da or not db:
        return False
    if da == db:
        return True
    # Italia: spesso arriva 348... ma in shops hai 39348...
    if da.startswith("39") and da[2:] == db:
        return True
    if db.startswith("39") and db[2:] == da:
        return True
    return False


# =========================
# Time / Date parsing (IT)
# =========================
WEEKDAYS_IT = {
    "lunedì": 0, "lunedi": 0, "lun": 0,
    "martedì": 1, "martedi": 1, "mar": 1,
    "mercoledì": 2, "mercoledi": 2, "mer": 2,
    "giovedì": 3, "giovedi": 3, "gio": 3,
    "venerdì": 4, "venerdi": 4, "ven": 4,
    "sabato": 5, "sab": 5,
    "domenica": 6, "dom": 6,
}

def tzinfo_for(shop_tz: str):
    if ZoneInfo:
        return ZoneInfo(shop_tz)
    return None

def now_local(shop_tz: str) -> dt.datetime:
    tz = tzinfo_for(shop_tz)
    return dt.datetime.now(tz) if tz else dt.datetime.now()

def parse_date(text: str, shop_tz: str) -> Optional[dt.date]:
    t = text.lower()

    today = now_local(shop_tz).date()
    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)

    # stasera -> oggi (ma userà fascia serale)
    if "stasera" in t:
        return today

    # dd/mm(/yyyy)
    m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b", t)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        yraw = m.group(3)
        y = today.year if not yraw else (2000 + int(yraw) if int(yraw) < 100 else int(yraw))
        try:
            return dt.date(y, mo, d)
        except ValueError:
            return None

    # weekday
    for k, wd in WEEKDAYS_IT.items():
        if re.search(r"\b" + re.escape(k) + r"\b", t):
            # prossimo giorno della settimana (anche oggi se coincide? qui: prossimo)
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    return None

def parse_time(text: str) -> Optional[dt.time]:
    t = text.lower().strip()

    # 17:30 / 17.30
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    # 1730
    m = re.search(r"\b([01]\d|2[0-3])([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    # "alle 18" / "ore 18"
    m = re.search(r"\b(?:alle|ore)\s*([01]?\d|2[0-3])\b", t)
    if m:
        return dt.time(int(m.group(1)), 0)

    # solo "18" (se il testo contiene contesto temporale)
    m = re.search(r"\b([01]?\d|2[0-3])\b", t)
    if m and any(x in t for x in ["alle", "ore", "dopo", "prima"]):
        return dt.time(int(m.group(1)), 0)

    return None

def parse_window(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = text.lower()
    after = None
    before = None

    m = re.search(r"\bdopo\s+le?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m:
        after = parse_time(m.group(1))

    m = re.search(r"\bprima\s+delle?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m:
        before = parse_time(m.group(1))

    # fasce
    if "mattina" in t:
        after = after or dt.time(9, 0)
        before = before or dt.time(12, 0)
    if "pomeriggio" in t:
        after = after or dt.time(14, 0)
        before = before or dt.time(19, 0)
    if "sera" in t or "stasera" in t:
        after = after or dt.time(17, 30)
        before = before or dt.time(22, 0)

    return after, before


# =========================
# Sheets read/write
# =========================
def load_tab(tab: str) -> List[Dict[str, str]]:
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    cache_key = f"tab:{tab}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # Legge tutto il tab (A:Z)
    rng = f"{tab}!A:Z"
    res = sheets().spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=rng).execute()
    values = res.get("values", [])
    if not values:
        cache_set(cache_key, [])
        return []

    headers = [h.strip() for h in values[0]]
    rows = []
    for row in values[1:]:
        obj = {}
        for i, h in enumerate(headers):
            obj[h] = row[i].strip() if i < len(row) and isinstance(row[i], str) else (row[i] if i < len(row) else "")
        rows.append(obj)

    cache_set(cache_key, rows)
    return rows

def _find_row_index(tab: str, predicate) -> Optional[int]:
    """
    Ritorna row index 2-based (perché riga 1 è header).
    """
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    rng = f"{tab}!A:Z"
    res = sheets().spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=rng).execute()
    values = res.get("values", [])
    if not values:
        return None
    headers = [h.strip() for h in values[0]]
    for idx, row in enumerate(values[1:], start=2):
        obj = {}
        for i, h in enumerate(headers):
            obj[h] = row[i].strip() if i < len(row) and isinstance(row[i], str) else (row[i] if i < len(row) else "")
        if predicate(obj):
            return idx
    return None

def _tab_headers(tab: str) -> List[str]:
    rng = f"{tab}!1:1"
    res = sheets().spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=rng).execute()
    vals = res.get("values", [[]])
    return [h.strip() for h in (vals[0] if vals else [])]

def upsert_row(tab: str, key_predicate, data: Dict[str, Any]):
    """
    Aggiorna la riga se trovata, altrimenti append.
    Usa headers del tab per ordinare le colonne.
    """
    headers = _tab_headers(tab)
    if not headers:
        raise RuntimeError(f"Tab '{tab}' non ha header")

    # normalizza valori in string
    row_values = []
    for h in headers:
        v = data.get(h, "")
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        elif v is None:
            v = ""
        else:
            v = str(v)
        row_values.append(v)

    row_idx = _find_row_index(tab, key_predicate)
    if row_idx is None:
        # append
        sheets().spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{tab}!A:Z",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        ).execute()
    else:
        # update range for that row
        end_col = chr(ord("A") + max(0, len(headers) - 1))
        rng = f"{tab}!A{row_idx}:{end_col}{row_idx}"
        sheets().spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=rng,
            valueInputOption="RAW",
            body={"values": [row_values]},
        ).execute()

    # invalida cache
    cache_set(f"tab:{tab}", None)


# =========================
# Load shop config
# =========================
def load_shop_by_phone(phone: str) -> Optional[Dict[str, str]]:
    shops = load_tab("shops")
    for s in shops:
        if phone_matches(phone, s.get("whatsapp_number", "")):
            return s
    return None

def load_hours(shop_id: str) -> Dict[int, List[Tuple[dt.time, dt.time]]]:
    rows = load_tab("hours")
    out: Dict[int, List[Tuple[dt.time, dt.time]]] = {i: [] for i in range(7)}
    for r in rows:
        if r.get("shop_id") != shop_id:
            continue
        try:
            wd = int(r.get("weekday", ""))
            st = dt.time.fromisoformat(r.get("start", "09:00"))
            en = dt.time.fromisoformat(r.get("end", "19:00"))
            out[wd].append((st, en))
        except Exception:
            continue
    # ordina fasce
    for wd in out:
        out[wd].sort(key=lambda x: x[0])
    return out

def load_services(shop_id: str) -> List[Dict[str, str]]:
    rows = load_tab("services")
    out = []
    for r in rows:
        if r.get("shop_id") != shop_id:
            continue
        active = (r.get("active", "TRUE").strip().lower() != "false")
        if not active:
            continue
        out.append(r)
    return out


# =========================
# Customers + Sessions (Sheets)
# =========================
def get_customer(shop_id: str, phone: str) -> Optional[Dict[str, str]]:
    rows = load_tab("customers")
    for r in rows:
        if r.get("shop_id") == shop_id and phone_matches(phone, r.get("phone", "")):
            return r
    return None

def upsert_customer(shop_id: str, phone: str, last_service: str):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    prev = get_customer(shop_id, phone)
    total = 0
    if prev:
        try:
            total = int(prev.get("total_visits", "0") or "0")
        except Exception:
            total = 0
    total += 1

    upsert_row(
        "customers",
        key_predicate=lambda r: r.get("shop_id") == shop_id and phone_matches(phone, r.get("phone", "")),
        data={
            "shop_id": shop_id,
            "phone": norm_phone(phone),
            "last_service": last_service,
            "total_visits": str(total),
            "last_visit": now_iso,
        },
    )

def get_session(shop_id: str, phone: str) -> Optional[Dict[str, Any]]:
    rows = load_tab("sessions")
    for r in rows:
        if r.get("shop_id") == shop_id and phone_matches(phone, r.get("phone", "")):
            data = r.get("data", "") or "{}"
            try:
                data_obj = json.loads(data) if isinstance(data, str) else {}
            except Exception:
                data_obj = {}
            return {
                "shop_id": r.get("shop_id", shop_id),
                "phone": r.get("phone", norm_phone(phone)),
                "state": r.get("state", ""),
                "data": data_obj,
                "updated_at": r.get("updated_at", ""),
            }
    return None

def save_session(shop_id: str, phone: str, state: str, data: Dict[str, Any]):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    upsert_row(
        "sessions",
        key_predicate=lambda r: r.get("shop_id") == shop_id and phone_matches(phone, r.get("phone", "")),
        data={
            "shop_id": shop_id,
            "phone": norm_phone(phone),
            "state": state,
            "data": json.dumps(data, ensure_ascii=False),
            "updated_at": now_iso,
        },
    )

def reset_session(shop_id: str, phone: str):
    # "delete" non è comodissimo su Sheets: mettiamo state vuoto e data vuota
    save_session(shop_id, phone, "", {})

def session_expired(sess: Dict[str, Any]) -> bool:
    raw = sess.get("updated_at") or ""
    if not raw:
        return True
    try:
        ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age_min = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60
        return age_min > SESSION_TTL_MINUTES
    except Exception:
        return True


# =========================
# Google Calendar availability with capacity
# =========================
def count_overlaps(cal_id: str, start: dt.datetime, end: dt.datetime, tz: str) -> int:
    """
    Conta quanti eventi si sovrappongono a [start, end).
    """
    svc = calendar()
    res = svc.events().list(
        calendarId=cal_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250
    ).execute()
    items = res.get("items", [])
    return len(items)

def slot_has_capacity(cal_id: str, start: dt.datetime, end: dt.datetime, tz: str, capacity: int) -> bool:
    overlaps = count_overlaps(cal_id, start, end, tz)
    return overlaps < max(1, capacity)

def create_event(cal_id: str, start: dt.datetime, end: dt.datetime, tz: str, summary: str, phone: str, service_name: str):
    svc = calendar()
    ev = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
        "description": f"Prenotazione WhatsApp\nTelefono: {norm_phone(phone)}\nServizio: {service_name}",
        "extendedProperties": {"private": {"phone": norm_phone(phone), "service": service_name}},
    }
    svc.events().insert(calendarId=cal_id, body=ev).execute()


# =========================
# Slot finding
# =========================
def round_up_to_slot(dtobj: dt.datetime, slot_minutes: int) -> dt.datetime:
    dtobj = dtobj.replace(second=0, microsecond=0)
    m = (dtobj.minute // slot_minutes) * slot_minutes
    base = dtobj.replace(minute=m)
    if base < dtobj:
        base += dt.timedelta(minutes=slot_minutes)
    return base

def find_slots(
    shop: Dict[str, str],
    hours_map: Dict[int, List[Tuple[dt.time, dt.time]]],
    service_duration_min: int,
    preferred_date: Optional[dt.date],
    exact_time: Optional[dt.time],
    after: Optional[dt.time],
    before: Optional[dt.time],
    limit: int = 5,
    max_days: int = 14,
) -> List[dt.datetime]:
    tz = shop.get("timezone", "Europe/Rome")
    tzinfo = tzinfo_for(tz)
    slot_minutes = int(shop.get("slot_minutes", "30") or "30")
    capacity = int(shop.get("capacity", "1") or "1")
    cal_id = shop["calendar_id"]

    now_l = now_local(tz)
    today = now_l.date()
    base = preferred_date or today
    if base < today:
        base = today

    duration = dt.timedelta(minutes=service_duration_min)

    def in_open_hours(d: dt.date, start_t: dt.time, end_t: dt.time) -> bool:
        for st, en in hours_map.get(d.weekday(), []):
            if st <= start_t and end_t <= en:
                return True
        return False

    results: List[dt.datetime] = []

    # 1) se ho data+orario preciso: prova quello prima
    if preferred_date and exact_time:
        start = dt.datetime.combine(preferred_date, exact_time)
        if tzinfo:
            start = start.replace(tzinfo=tzinfo)
        end = start + duration
        if in_open_hours(preferred_date, start.time(), end.time()):
            # se oggi: non nel passato
            if preferred_date == today and start < now_l:
                pass
            else:
                if slot_has_capacity(cal_id, start, end, tz, capacity):
                    return [start]

    # 2) scan generale
    for day_off in range(0, max_days + 1):
        d = base + dt.timedelta(days=day_off)
        ranges = hours_map.get(d.weekday(), [])
        if not ranges:
            continue

        for st, en in ranges:
            start_dt = dt.datetime.combine(d, st)
            end_dt = dt.datetime.combine(d, en)
            if tzinfo:
                start_dt = start_dt.replace(tzinfo=tzinfo)
                end_dt = end_dt.replace(tzinfo=tzinfo)

            # vincoli after/before
            if after:
                tmp = dt.datetime.combine(d, after)
                if tzinfo:
                    tmp = tmp.replace(tzinfo=tzinfo)
                start_dt = max(start_dt, tmp)
            if before:
                tmp = dt.datetime.combine(d, before)
                if tzinfo:
                    tmp = tmp.replace(tzinfo=tzinfo)
                end_dt = min(end_dt, tmp)

            if end_dt <= start_dt:
                continue

            # oggi: dal prossimo slot
            if d == today:
                start_dt = max(start_dt, round_up_to_slot(now_l, slot_minutes))

            cur = round_up_to_slot(start_dt, slot_minutes)
            while cur + duration <= end_dt:
                if slot_has_capacity(cal_id, cur, cur + duration, tz, capacity):
                    results.append(cur)
                    if len(results) >= limit:
                        return results
                cur += dt.timedelta(minutes=slot_minutes)

    return results

def format_slot(shop_tz: str, d: dt.datetime) -> str:
    # e.g. "Mer 18/12 18:30"
    tzinfo = tzinfo_for(shop_tz)
    dd = d.astimezone(tzinfo) if (tzinfo and d.tzinfo) else d
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[dd.weekday()]} {dd.strftime('%d/%m')} {dd.strftime('%H:%M')}"


# =========================
# Human messages + upsell logic
# =========================
CONFIRM_WORDS = {"ok", "va bene", "confermo", "conferma", "sì", "si", "perfetto", "certo"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no", "non va bene", "non confermo"}

def render_slots(shop: Dict[str, str], title: str, slots: List[dt.datetime]) -> str:
    lines = [title]
    for i, sl in enumerate(slots, start=1):
        lines.append(f"{i}) {format_slot(shop.get('timezone','Europe/Rome'), sl)}")
    lines.append("\nRispondi con il numero (1,2,3...) oppure scrivi un giorno/orario diverso.")
    return "\n".join(lines)

def pick_service_from_text(services: List[Dict[str, str]], text: str) -> Optional[Dict[str, str]]:
    t = text.lower()
    # match semplice: contiene nome servizio
    for s in services:
        name = (s.get("name","") or "").strip()
        if name and name.lower() in t:
            return s
    # match parole chiave comuni
    if "barba" in t:
        for s in services:
            if "barba" in (s.get("name","").lower()):
                return s
    if "colore" in t or "tinta" in t:
        for s in services:
            n = s.get("name","").lower()
            if "colore" in n or "tinta" in n:
                return s
    if "piega" in t:
        for s in services:
            if "piega" in s.get("name","").lower():
                return s
    if "taglio" in t:
        # preferisci "taglio" se esiste
        for s in services:
            if "taglio" in s.get("name","").lower():
                return s
    return None

def default_service_if_single(services: List[Dict[str, str]], shop_gender: str) -> Optional[Dict[str, str]]:
    # se c'è un solo servizio attivo -> default
    if len(services) == 1:
        return services[0]

    # se uomo e c'è un classico "taglio uomo" -> default soft
    if shop_gender == "uomo":
        for s in services:
            if s.get("name","").lower() in {"taglio", "taglio uomo", "taglio capelli", "taglio uomo (30m)"}:
                return s
    return None


# =========================
# ✅ DEFINITIVE CORE: handle_message
# =========================
def handle_message(shop: Dict[str, str], phone: str, text: str) -> str:
    tz = shop.get("timezone", "Europe/Rome")
    shop_id = shop.get("shop_id", "")
    shop_name = shop.get("name", "il salone")
    slot_minutes = int(shop.get("slot_minutes", "30") or "30")
    capacity = int(shop.get("capacity", "1") or "1")
    gender = (shop.get("gender", "unisex") or "unisex").lower()

    services = load_services(shop_id)
    hours_map = load_hours(shop_id)

    t = (text or "").strip()
    tlow = t.lower().strip()

    # session
    sess = get_session(shop_id, phone)
    if sess and session_expired(sess):
        reset_session(shop_id, phone)
        sess = None

    # cancel
    if any(w in tlow for w in CANCEL_WORDS):
        reset_session(shop_id, phone)
        return "Ok 👍 Nessun problema. Se vuoi riprenotare, dimmi pure giorno e orario (es. “domani alle 18”)."

    # helper: greeting
    if tlow in {"ciao", "salve", "buongiorno", "buonasera", "hey"} and not sess:
        cust = get_customer(shop_id, phone)
        if cust and cust.get("last_service"):
            return (
                f"Ciao! 👋 Sei in contatto con *{shop_name}* 💈\n"
                f"Ultima volta hai fatto: *{cust.get('last_service')}*.\n\n"
                f"Quando vuoi prenotare?"
            )
        # se uomo e praticamente fa solo tagli -> non stressare col servizio
        if gender == "uomo":
            default = default_service_if_single(services, gender)
            if default:
                return (
                    f"Ciao! 👋 Sei in contatto con *{shop_name}* 💈\n"
                    f"Dimmi quando vuoi prenotare 😊"
                )
        return (
            f"Ciao! 👋 Sei in contatto con *{shop_name}* 💈\n"
            f"Dimmi quando vuoi prenotare 😊"
        )

    # ====== intents / parsing
    date_ = parse_date(t, tz)
    exact_time = parse_time(t)
    after, before = parse_window(t)

    wants_booking = any(x in tlow for x in ["prenot", "appunt", "posto", "disponib", "libero"]) or bool(date_) or bool(exact_time) or bool(after) or bool(before)

    # ====== determine service
    chosen_service = None
    if services:
        chosen_service = pick_service_from_text(services, t)

    # if session has service already
    if sess and isinstance(sess.get("data"), dict):
        if sess["data"].get("service"):
            chosen_service = sess["data"]["service"]

    # if no service chosen, maybe default
    if not chosen_service:
        default = default_service_if_single(services, gender)
        if default:
            chosen_service = default

    # derive duration
    def service_duration(svc: Dict[str, str]) -> int:
        try:
            return int(svc.get("duration", "") or svc.get("duration_minutes","") or "30")
        except Exception:
            return 30

    # ====== if not booking -> soft help
    if not wants_booking:
        # se servono servizi (unisex/donna)
        if not chosen_service and (gender in {"donna", "unisex"}) and len(services) > 1:
            s_list = "\n".join([f"• {s.get('name')}" for s in services[:8]])
            return (
                f"Per aiutarti meglio 😊 che servizio desideri?\n{s_list}\n\n"
                f"Poi dimmi giorno e orario (es. “domani alle 18”)."
            )
        return f"Dimmi pure quando vuoi venire (es. “domani alle 18” o “mercoledì dopo le 17:30”)."

    # ====== state machine
    state = (sess.get("state") if sess else "") or ""
    data = (sess.get("data") if sess else {}) or {}
    data = data if isinstance(data, dict) else {}

    # ========= STATE: confirm
    if state == "confirm":
        if tlow in CONFIRM_WORDS:
            slot_iso = data.get("slot_iso")
            service = data.get("service")
            if not slot_iso or not service:
                reset_session(shop_id, phone)
                return "Ops, ho perso i dettagli 😅 Ripartiamo: quando vuoi venire?"
            start = dt.datetime.fromisoformat(slot_iso)
            dur = service_duration(service)
            end = start + dt.timedelta(minutes=dur)

            # re-check capacity and then create
            if not slot_has_capacity(shop["calendar_id"], start, end, tz, capacity):
                reset_session(shop_id, phone)
                # proponi alternative
                alt = find_slots(shop, hours_map, dur, start.date(), None, None, None, limit=5, max_days=7)
                if not alt:
                    return "Quello slot è appena stato preso 😅 Vuoi indicarmi un’altra fascia?"
                save_session(shop_id, phone, "choose", {"service": service, "options": [x.isoformat() for x in alt]})
                return render_slots(shop, "Quell’orario non è più disponibile. Ecco alcune alternative:", alt)

            # create event
            create_event(
                shop["calendar_id"],
                start,
                end,
                tz,
                summary=f"{shop_name} - {service.get('name','Servizio')}",
                phone=phone,
                service_name=service.get("name",""),
            )
            upsert_customer(shop_id, phone, service.get("name",""))
            reset_session(shop_id, phone)

            return (
                f"✅ Perfetto! Ti ho prenotato da *{shop_name}*.\n"
                f"💈 *{service.get('name','Servizio')}*\n"
                f"🕒 {format_slot(tz, start)}\n\n"
                f"A presto 👋"
            )

        # se non conferma, interpreta come nuova richiesta
        reset_session(shop_id, phone)
        sess = None
        state = ""
        data = {}

    # ========= STATE: choose (lista)
    if state == "choose":
        options = data.get("options") or []
        service = data.get("service") or chosen_service
        # numero scelto?
        m = re.search(r"\b(\d{1,2})\b", tlow)
        if m and options:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(options):
                start = dt.datetime.fromisoformat(options[idx])
                save_session(shop_id, phone, "confirm", {"service": service, "slot_iso": start.isoformat()})
                return (
                    f"Confermi questo appuntamento?\n"
                    f"💈 *{service.get('name','Servizio')}*\n"
                    f"🕒 {format_slot(tz, start)}\n\n"
                    f"Rispondi *OK* per confermare oppure *annulla*."
                )

        # altrimenti: nuova preferenza -> continua sotto
        # (non bloccare)
        reset_session(shop_id, phone)
        state = ""
        data = {}

    # ========= SERVICE missing?
    if not chosen_service and len(services) > 1:
        # chiedi servizio (tono umano + poco invasivo)
        s_list = "\n".join([f"• {s.get('name')}" for s in services[:10]])
        save_session(shop_id, phone, "need_service", {"asked": True})
        return (
            f"Perfetto 😊 Per che servizio vuoi prenotare da *{shop_name}*?\n"
            f"{s_list}\n\n"
            f"Scrivimi ad esempio: “{services[0].get('name','Taglio')} domani alle 18”."
        )

    # ========= STATE: need_service
    if state == "need_service":
        svc = pick_service_from_text(services, t)
        if not svc:
            s_list = "\n".join([f"• {s.get('name')}" for s in services[:10]])
            return f"Dimmi il servizio desiderato:\n{s_list}"
        chosen_service = svc
        # continuiamo: se manca data/ora chiediamo
        # (non ritorniamo subito qui)

    # ======= UPSell soft (1 volta), solo se senso
    # Esempio: barber uomo, ha "Barba" disponibile e cliente chiede "taglio" senza barba
    def has_barba():
        return any("barba" in (s.get("name","").lower()) for s in services)

    if gender == "uomo" and chosen_service and ("taglio" in chosen_service.get("name","").lower()) and has_barba():
        if not data.get("upsell_barba_done") and "barba" not in tlow:
            # non insistere: una volta sola
            data["upsell_barba_done"] = True
            save_session(shop_id, phone, "need_date_or_time", {"service": chosen_service, **data})
            return (
                "Perfetto 👍 Vuoi aggiungere anche la *barba* oppure solo *taglio*?\n"
                "• Scrivi “solo taglio” oppure “taglio e barba”."
            )

    # se risposta all’upsell
    if "solo taglio" in tlow and chosen_service:
        pass
    if ("taglio e barba" in tlow or ("barba" in tlow and "taglio" in tlow)) and services:
        # prova a selezionare un servizio combo se esiste
        combo = None
        for s in services:
            n = s.get("name","").lower()
            if "taglio" in n and "barba" in n:
                combo = s
                break
        if combo:
            chosen_service = combo

    # ========= If missing date or time -> ask smartly
    if chosen_service and not date_ and not exact_time and not after and not before:
        save_session(shop_id, phone, "need_when", {"service": chosen_service, **data})
        return "Quando preferisci venire? (es. “domani alle 18”, “mercoledì dopo le 17:30”)."

    # if only date -> ask time
    if chosen_service and date_ and not exact_time and not after and not before:
        save_session(shop_id, phone, "need_time", {"service": chosen_service, "date": date_.isoformat(), **data})
        return f"Ok 👍 {date_.strftime('%d/%m')} a che ora preferisci? (es. 18:00) oppure una fascia (es. “dopo le 18”)."

    # if only time window/time -> ask day
    if chosen_service and (exact_time or after or before) and not date_:
        payload = {"service": chosen_service, **data}
        if exact_time:
            payload["time"] = exact_time.isoformat()
        if after:
            payload["after"] = after.isoformat()
        if before:
            payload["before"] = before.isoformat()
        save_session(shop_id, phone, "need_date", payload)
        return "Perfetto 👍 Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # ========= STATE: need_date
    if state == "need_date":
        svc = chosen_service or data.get("service")
        d = parse_date(t, tz)
        if not d:
            return "Ok 😊 Dimmi il giorno (es. “domani”, “mercoledì”, “17/12”)."
        # recupera vincoli
        exact = dt.time.fromisoformat(data["time"]) if data.get("time") else None
        aft = dt.time.fromisoformat(data["after"]) if data.get("after") else None
        bef = dt.time.fromisoformat(data["before"]) if data.get("before") else None
        date_ = d
        exact_time = exact_time or exact
        after = after or aft
        before = before or bef
        chosen_service = svc

    # ========= STATE: need_time
    if state == "need_time":
        svc = chosen_service or data.get("service")
        d_iso = data.get("date")
        if d_iso:
            try:
                date_ = dt.date.fromisoformat(d_iso)
            except Exception:
                date_ = date_
        exact_time = exact_time or parse_time(t)
        a2, b2 = parse_window(t)
        after = after or a2
        before = before or b2
        chosen_service = svc
        if not exact_time and not after and not before:
            return "Dimmi un orario valido (es. 18:00) oppure una fascia (es. “dopo le 18”)."

    # ========= MAIN BOOKING: propose or confirm
    if not chosen_service:
        # fallback: se non sappiamo il servizio
        if len(services) > 1:
            s_list = "\n".join([f"• {s.get('name')}" for s in services[:10]])
            return f"Che servizio desideri?\n{s_list}"
        chosen_service = services[0] if services else {"name": "Appuntamento", "duration": "30"}

    dur = service_duration(chosen_service)

    # se ho data+ora precisa
    if date_ and exact_time:
        slots = find_slots(
            shop, hours_map, dur,
            preferred_date=date_,
            exact_time=exact_time,
            after=None, before=None,
            limit=5, max_days=10
        )
        if slots and slots[0].date() == date_ and slots[0].time() == exact_time:
            start = slots[0]
            save_session(shop_id, phone, "confirm", {"service": chosen_service, "slot_iso": start.isoformat(), "upsell_barba_done": data.get("upsell_barba_done", False)})
            return (
                f"Perfetto 👍 Confermi?\n"
                f"💈 *{chosen_service.get('name','Servizio')}*\n"
                f"🕒 {format_slot(tz, start)}\n\n"
                f"Rispondi *OK* per confermare oppure *annulla*."
            )

        # non disponibile: alternative “vicine” (stesso giorno) + stesso orario giorni successivi
        # (il finder già cerca a partire da quel giorno)
        if not slots:
            return "Non vedo disponibilità in quel momento. Vuoi indicarmi un’altra fascia (es. “dopo le 18”)?"

        save_session(shop_id, phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in slots], "upsell_barba_done": data.get("upsell_barba_done", False)})
        return render_slots(
            shop,
            f"A quell’ora non riesco 😅\nSe vuoi, posso proporti questi orari vicini:",
            slots
        )

    # data + fascia
    if date_ and (after or before) and not exact_time:
        slots = find_slots(
            shop, hours_map, dur,
            preferred_date=date_,
            exact_time=None,
            after=after, before=before,
            limit=5, max_days=10
        )
        if not slots:
            return "In quella fascia non vedo posti liberi 😕 Vuoi provare un altro orario o un altro giorno?"
        save_session(shop_id, phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in slots], "upsell_barba_done": data.get("upsell_barba_done", False)})
        return render_slots(shop, "Perfetto 👍 Ecco alcune disponibilità:", slots)

    # richiesta generica: proponi prossimi slot (domani se non specifica)
    base_date = date_ or (now_local(tz).date() + dt.timedelta(days=1) if "domani" in tlow else now_local(tz).date())
    slots = find_slots(
        shop, hours_map, dur,
        preferred_date=base_date,
        exact_time=None,
        after=after, before=before,
        limit=5, max_days=10
    )
    if not slots:
        return "Non vedo disponibilità a breve 😕 Dimmi un giorno preciso o una fascia (es. “mercoledì dopo le 18”)."

    save_session(shop_id, phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in slots], "upsell_barba_done": data.get("upsell_barba_done", False)})
    return render_slots(shop, "Ecco i prossimi orari liberi:", slots)


# =========================
# ROUTES
# =========================
@APP.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"

@APP.route("/test", methods=["GET"])
def test():
    """
    Esempio:
    /test?phone=393481111111&msg=ciao
    """
    phone = request.args.get("phone", "cliente1")
    msg = request.args.get("msg", "ciao")

    shop = load_shop_by_phone(phone)
    if not shop:
        return jsonify({"error": "shop non trovato", "phone": phone}), 404

    reply = handle_message(shop, phone, msg)
    return jsonify({
        "shop": shop.get("name"),
        "phone": phone,
        "message_in": msg,
        "bot_reply": reply
    })

# (placeholder) endpoint inbound per WhatsApp Cloud API:
# quando Meta ti sblocca, collegheremo qui i webhook veri.
@APP.route("/wa", methods=["POST"])
def wa_placeholder():
    return jsonify({"ok": True, "note": "Webhook WhatsApp Cloud API non configurato in questa fase."})

if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
