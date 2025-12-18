from __future__ import annotations

import os
import re
import json
import time
import datetime as dt
from typing import Optional, Tuple, List, Dict, Any

from flask import Flask, request, jsonify

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # Railway di solito lo supporta


# =========================
# ENV
# =========================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Rome")

# opzionale: se vuoi stampare debug nei log
DEBUG = os.getenv("DEBUG", "0") == "1"

# limite massimo giorni di ricerca se il cliente è vago
MAX_DAYS_LOOKAHEAD = int(os.getenv("MAX_DAYS_LOOKAHEAD", "14"))

# =========================
# APP
# =========================
app = Flask(__name__)

# =========================
# GOOGLE CLIENTS (CACHE)
# =========================
_sheets = None
_calendar = None

def _log(*args):
    if DEBUG:
        print("DEBUG:", *args, flush=True)

def sheets_service():
    global _sheets
    if _sheets is not None:
        return _sheets
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        raise RuntimeError("Mancano GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_SHEET_ID")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    _sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets

def calendar_service():
    global _calendar
    if _calendar is not None:
        return _calendar
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Manca GOOGLE_SERVICE_ACCOUNT_JSON")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    _calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _calendar

# =========================
# SHEETS HELPERS
# =========================
def sheet_read(tab_name: str) -> List[Dict[str, str]]:
    """
    Legge un tab con header in riga 1 e ritorna lista dict.
    """
    srv = sheets_service()
    rng = f"{tab_name}!A:Z"
    resp = srv.spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=rng).execute()
    values = resp.get("values", [])
    if not values:
        return []
    headers = [h.strip() for h in values[0]]
    rows = []
    for r in values[1:]:
        row = {}
        for i, h in enumerate(headers):
            row[h] = (r[i].strip() if i < len(r) else "")
        # ignora righe completamente vuote
        if any(v != "" for v in row.values()):
            rows.append(row)
    return rows

def sheet_append(tab_name: str, row: Dict[str, Any], header: List[str]) -> None:
    srv = sheets_service()
    values = [[str(row.get(h, "")) for h in header]]
    srv.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab_name}!A:Z",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()

def sheet_upsert_by_keys(tab_name: str, keys: Dict[str, str], new_values: Dict[str, Any], header: List[str]) -> None:
    """
    Upsert semplice:
    - legge tab
    - trova la riga dove tutti i keys matchano
    - se esiste: update
    - altrimenti: append
    """
    srv = sheets_service()
    rng = f"{tab_name}!A:Z"
    resp = srv.spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=rng).execute()
    values = resp.get("values", [])
    if not values:
        # crea header + append
        sheet_append(tab_name, {}, header)
        sheet_append(tab_name, {**keys, **new_values}, header)
        return

    headers = [h.strip() for h in values[0]]
    # se header non matcha, useremo quello che c'è (ma consigliato: header corretto)
    if headers:
        header = headers

    # cerca riga
    match_idx = None
    for idx in range(1, len(values)):
        row = values[idx]
        row_map = {header[i]: (row[i].strip() if i < len(row) else "") for i in range(len(header))}
        ok = True
        for k, v in keys.items():
            if (row_map.get(k, "") or "") != (v or ""):
                ok = False
                break
        if ok:
            match_idx = idx
            break

    if match_idx is None:
        sheet_append(tab_name, {**keys, **new_values}, header)
        return

    # update row
    updated_row = []
    # ricostruisci row_map
    old = values[match_idx]
    old_map = {header[i]: (old[i].strip() if i < len(old) else "") for i in range(len(header))}
    merged = dict(old_map)
    for k, v in keys.items():
        merged[k] = v
    for k, v in new_values.items():
        merged[k] = str(v)
    for h in header:
        updated_row.append(merged.get(h, ""))

    # range della riga (1-indexed in Sheets)
    row_number = match_idx + 1
    start_col = "A"
    end_col = chr(ord("A") + max(0, len(header) - 1))
    target = f"{tab_name}!{start_col}{row_number}:{end_col}{row_number}"

    srv.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=target,
        valueInputOption="USER_ENTERED",
        body={"values": [updated_row]},
    ).execute()

# =========================
# NORMALIZATION
# =========================
def normalize_phone(raw: str) -> str:
    """
    Accetta:
    - whatsapp:+39348...
    - +39348...
    - 39348...
    - 348...
    Ritorna stringa numerica senza prefissi tipo whatsapp:
    (manteniamo solo cifre).
    """
    if not raw:
        return ""
    s = raw.strip()
    s = s.replace("whatsapp:", "").strip()
    digits = re.sub(r"\D+", "", s)
    return digits

def normalize_whatsapp_number(raw: str) -> str:
    """
    Numero del negozio nel foglio: può essere 'whatsapp:+39...' oppure '393...'
    -> normalizziamo a sole cifre per confrontare.
    """
    return normalize_phone(raw)

# =========================
# DATE/TIME PARSING (IT)
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

def tzinfo(tzname: str):
    if ZoneInfo:
        try:
            return ZoneInfo(tzname)
        except Exception:
            return ZoneInfo(DEFAULT_TIMEZONE)
    return None

def now_local(tzname: str) -> dt.datetime:
    tz = tzinfo(tzname)
    return dt.datetime.now(tz) if tz else dt.datetime.now()

def parse_date(text: str, tzname: str) -> Optional[dt.date]:
    t = text.lower()

    today = now_local(tzname).date()
    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)

    # "stasera" / "questa sera" -> stesso giorno, fascia sera
    # (la data resta oggi; la fascia la gestiamo altrove)
    if "stasera" in t or "questa sera" in t:
        return today

    # 17/12 o 17-12 o 17/12/2025
    m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b", t)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        yy = m.group(3)
        if yy:
            y = int(yy)
            y = 2000 + y if y < 100 else y
        else:
            y = today.year
        try:
            return dt.date(y, mo, d)
        except ValueError:
            return None

    # giorno settimana
    for k, wd in WEEKDAYS_IT.items():
        if re.search(r"\b" + re.escape(k) + r"\b", t):
            # prossimo occorrere
            base = today
            delta = (wd - base.weekday()) % 7
            if delta == 0:
                delta = 7
            return base + dt.timedelta(days=delta)

    return None

def parse_time(text: str) -> Optional[dt.time]:
    t = text.lower()

    # HH:MM o HH.MM
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    # 1830
    m = re.search(r"\b([01]\d|2[0-3])([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    # "alle 18" / "ore 18" / "per le 18"
    m = re.search(r"\b(?:alle|ore|per\s+le)\s*([01]?\d|2[0-3])\b", t)
    if m:
        return dt.time(int(m.group(1)), 0)

    # solo numero "18" (lo accettiamo solo se contesto temporale chiaro)
    m = re.search(r"\b([01]?\d|2[0-3])\b", t)
    if m and any(x in t for x in ["oggi", "domani", "dopodomani", "stasera", "mattina", "pomeriggio", "sera"]):
        return dt.time(int(m.group(1)), 0)

    return None

def parse_time_window(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = text.lower()
    after = None
    before = None

    m_after = re.search(r"\bdopo\s+le?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m_after:
        after = parse_time(m_after.group(1))

    m_before = re.search(r"\bprima\s+delle?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m_before:
        before = parse_time(m_before.group(1))

    # fasce
    if "mattina" in t:
        after = after or dt.time(9, 0)
        before = before or dt.time(12, 0)
    if "pomeriggio" in t:
        after = after or dt.time(14, 0)
        before = before or dt.time(19, 0)
    if "sera" in t or "stasera" in t or "questa sera" in t:
        after = after or dt.time(17, 30)
        before = before or dt.time(22, 0)

    return after, before

def round_up_minutes(d: dt.datetime, slot_minutes: int) -> dt.datetime:
    d = d.replace(second=0, microsecond=0)
    m = (d.minute // slot_minutes) * slot_minutes
    base = d.replace(minute=m)
    if base < d:
        base = base + dt.timedelta(minutes=slot_minutes)
    return base

def fmt_dt_local(d: dt.datetime, tzname: str) -> str:
    # d già tz-aware (consigliato)
    s = d.strftime("%a %d/%m %H:%M")
    return (s.replace("Mon","Lun").replace("Tue","Mar").replace("Wed","Mer")
              .replace("Thu","Gio").replace("Fri","Ven").replace("Sat","Sab").replace("Sun","Dom"))

# =========================
# LOAD CONFIG FROM SHEETS
# =========================
def load_shops() -> List[Dict[str, str]]:
    return sheet_read("shops")

def load_hours() -> List[Dict[str, str]]:
    return sheet_read("hours")

def load_services() -> List[Dict[str, str]]:
    return sheet_read("services")

def shop_by_id(shop_id: str) -> Optional[Dict[str, str]]:
    for s in load_shops():
        if (s.get("shop_id","") or "").strip() == shop_id:
            return s
    return None

def shop_by_number(shop_number_digits: str) -> Optional[Dict[str, str]]:
    # match tra cifre
    for s in load_shops():
        if normalize_whatsapp_number(s.get("whatsapp_number","")) == shop_number_digits:
            return s
    return None

def get_shop_hours(shop_id: str) -> Dict[int, List[Tuple[dt.time, dt.time]]]:
    """
    Ritorna {weekday: [(start,end),...]}
    weekday: 0=lun ... 6=dom
    """
    out: Dict[int, List[Tuple[dt.time, dt.time]]] = {i: [] for i in range(7)}
    for r in load_hours():
        if (r.get("shop_id","") or "").strip() != shop_id:
            continue
        wd = int(r.get("weekday","-1") or -1)
        st = r.get("start","") or r.get("start_time","")
        en = r.get("end","") or r.get("end_time","")
        if wd < 0 or wd > 6 or not st or not en:
            continue
        sh, sm = map(int, st.split(":"))
        eh, em = map(int, en.split(":"))
        out[wd].append((dt.time(sh, sm), dt.time(eh, em)))
    return out

def get_shop_services(shop_id: str) -> List[Dict[str, str]]:
    sv = []
    for r in load_services():
        if (r.get("shop_id","") or "").strip() == shop_id:
            # attivo se non c'è colonna active oppure se != "false"
            active = (r.get("active","") or "true").lower() != "false"
            if active:
                sv.append(r)
    return sv

# =========================
# SESSIONS + CUSTOMERS (SHEETS)
# =========================
SESSIONS_HEADER = ["shop_id","phone","state","data","updated_at"]
CUSTOMERS_HEADER = ["shop_id","phone","last_service","last_visit","total_visits"]

def load_session(shop_id: str, phone: str) -> Optional[Dict[str, str]]:
    rows = sheet_read("sessions")
    for r in rows:
        if (r.get("shop_id","") == shop_id) and (r.get("phone","") == phone):
            return r
    return None

def save_session(shop_id: str, phone: str, state: str, data: Dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    sheet_upsert_by_keys(
        "sessions",
        keys={"shop_id": shop_id, "phone": phone},
        new_values={"state": state, "data": payload, "updated_at": dt.datetime.utcnow().isoformat()},
        header=SESSIONS_HEADER
    )

def reset_session(shop_id: str, phone: str) -> None:
    # Non avendo delete semplice via Sheets API senza usare batchUpdate complesso,
    # lo "resettiamo" impostando state vuoto e data vuota.
    sheet_upsert_by_keys(
        "sessions",
        keys={"shop_id": shop_id, "phone": phone},
        new_values={"state": "", "data": "{}", "updated_at": dt.datetime.utcnow().isoformat()},
        header=SESSIONS_HEADER
    )

def load_customer(shop_id: str, phone: str) -> Optional[Dict[str, str]]:
    rows = sheet_read("customers")
    for r in rows:
        if (r.get("shop_id","") == shop_id) and (r.get("phone","") == phone):
            return r
    return None

def upsert_customer_after_booking(shop_id: str, phone: str, service_name: str, when_iso: str) -> None:
    c = load_customer(shop_id, phone)
    total = 0
    if c and c.get("total_visits","").isdigit():
        total = int(c["total_visits"])
    total += 1
    sheet_upsert_by_keys(
        "customers",
        keys={"shop_id": shop_id, "phone": phone},
        new_values={"last_service": service_name, "last_visit": when_iso, "total_visits": str(total)},
        header=CUSTOMERS_HEADER
    )

# =========================
# CALENDAR AVAILABILITY (capacity aware)
# =========================
def count_overlapping_events(calendar_id: str, start: dt.datetime, end: dt.datetime) -> int:
    cal = calendar_service()
    # events.list timeMin/timeMax: includono eventi che intersecano la finestra se singleEvents=True
    # Nota: Google tende a ritornare eventi che iniziano nella finestra; per sicurezza:
    # - prendiamo quelli con start < end e end > start
    resp = cal.events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50
    ).execute()
    items = resp.get("items", []) or []
    overlap = 0
    for ev in items:
        s = ev.get("start", {}).get("dateTime")
        e = ev.get("end", {}).get("dateTime")
        if not s or not e:
            continue
        try:
            sdt = dt.datetime.fromisoformat(s)
            edt = dt.datetime.fromisoformat(e)
        except Exception:
            continue
        if sdt < end and edt > start:
            overlap += 1
    return overlap

def is_slot_available(calendar_id: str, start: dt.datetime, end: dt.datetime, capacity: int) -> bool:
    try:
        busy_count = count_overlapping_events(calendar_id, start, end)
        return busy_count < capacity
    except HttpError:
        # se API ha problemi, meglio non prenotare “a caso”
        return False

def create_event(calendar_id: str, start: dt.datetime, end: dt.datetime, summary: str, phone: str, service_id: str) -> str:
    cal = calendar_service()
    event = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "description": f"Prenotazione WhatsApp\nCliente: {phone}\nServizio: {summary}",
        "extendedProperties": {"private": {"phone": phone, "service_id": service_id}},
    }
    created = cal.events().insert(calendarId=calendar_id, body=event).execute()
    return created.get("id","")

# =========================
# SLOT SEARCH
# =========================
def iter_candidate_slots(
    tzname: str,
    hours_by_wd: Dict[int, List[Tuple[dt.time, dt.time]]],
    start_date: dt.date,
    slot_minutes: int,
    duration_minutes: int,
    after: Optional[dt.time],
    before: Optional[dt.time],
    max_days: int
):
    tz = tzinfo(tzname)
    today = now_local(tzname).date()
    nowdt = now_local(tzname)

    for day_offset in range(0, max_days + 1):
        d = start_date + dt.timedelta(days=day_offset)
        intervals = hours_by_wd.get(d.weekday(), []) or []
        if not intervals:
            continue

        for (st, en) in intervals:
            start_dt = dt.datetime.combine(d, st, tzinfo=tz)
            end_dt = dt.datetime.combine(d, en, tzinfo=tz)

            if after:
                start_dt = max(start_dt, dt.datetime.combine(d, after, tzinfo=tz))
            if before:
                end_dt = min(end_dt, dt.datetime.combine(d, before, tzinfo=tz))

            # se oggi, evita passato
            if d == today:
                start_dt = max(start_dt, round_up_minutes(nowdt, slot_minutes))

            cur = round_up_minutes(start_dt, slot_minutes)

            while cur + dt.timedelta(minutes=duration_minutes) <= end_dt:
                yield cur
                cur = cur + dt.timedelta(minutes=slot_minutes)

def find_best_slots(
    shop: Dict[str, str],
    service: Dict[str, str],
    preferred_date: Optional[dt.date],
    preferred_time: Optional[dt.time],
    after: Optional[dt.time],
    before: Optional[dt.time],
    max_days: int,
    limit: int = 5
) -> List[dt.datetime]:
    tzname = shop.get("timezone") or DEFAULT_TIMEZONE
    hours = get_shop_hours(shop["shop_id"])
    slot_minutes = int(shop.get("slot_minutes") or 30)
    capacity = int(shop.get("capacity") or 1)
    duration = int(service.get("duration") or service.get("duration_minutes") or slot_minutes)
    cal_id = shop.get("calendar_id","")

    if not cal_id:
        return []

    base_date = preferred_date or now_local(tzname).date()

    candidates = []
    # strategia:
    # 1) se ho data+ora precisa: provo quello, poi vicini nello stesso giorno (slot+1, slot+2, slot-1...),
    #    poi stesso orario nei prossimi giorni
    tz = tzinfo(tzname)
    if preferred_date and preferred_time:
        start0 = dt.datetime.combine(preferred_date, preferred_time, tzinfo=tz)
        end0 = start0 + dt.timedelta(minutes=duration)
        if is_slot_available(cal_id, start0, end0, capacity):
            return [start0]

        # vicini nello stesso giorno: +slot, +2slot, -slot, -2slot (ma solo dentro orari negozio)
        # per non complicare: rigeneriamo slot del giorno e prendiamo quelli più vicini all'orario
        day_slots = []
        for s in iter_candidate_slots(
            tzname, hours, preferred_date, slot_minutes, duration,
            after=None, before=None, max_days=0
        ):
            day_slots.append(s)
        day_slots.sort(key=lambda x: abs((x - start0).total_seconds()))
        for s in day_slots[:10]:
            e = s + dt.timedelta(minutes=duration)
            if is_slot_available(cal_id, s, e, capacity):
                candidates.append(s)
                if len(candidates) >= limit:
                    return candidates

        # stesso orario nei prossimi giorni
        for i in range(1, max_days + 1):
            d = preferred_date + dt.timedelta(days=i)
            s = dt.datetime.combine(d, preferred_time, tzinfo=tz)
            e = s + dt.timedelta(minutes=duration)
            # controlla anche che sia dentro orari negozio usando generator (semplice check)
            ok = False
            for slot in iter_candidate_slots(tzname, hours, d, slot_minutes, duration, None, None, 0):
                if slot.time() == s.time():
                    ok = True
                    break
            if not ok:
                continue
            if is_slot_available(cal_id, s, e, capacity):
                candidates.append(s)
                if len(candidates) >= limit:
                    return candidates

        # fallback: prime disponibilità nei prossimi giorni
        for s in iter_candidate_slots(tzname, hours, preferred_date, slot_minutes, duration, after, before, max_days):
            e = s + dt.timedelta(minutes=duration)
            if is_slot_available(cal_id, s, e, capacity):
                candidates.append(s)
                if len(candidates) >= limit:
                    return candidates
        return candidates

    # 2) se ho solo data + fascia / solo fascia / solo “domani”: proponi prime disponibilità
    for s in iter_candidate_slots(
        tzname, hours, base_date, slot_minutes, duration, after, before, max_days
    ):
        e = s + dt.timedelta(minutes=duration)
        if is_slot_available(cal_id, s, e, capacity):
            candidates.append(s)
            if len(candidates) >= limit:
                return candidates

    return candidates

# =========================
# BOT LOGIC
# =========================
CONFIRM_WORDS = {"ok", "va bene", "confermo", "conferma", "sì", "si", "perfetto", "certo"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no", "non va bene", "non confermo"}

def render_services(services: List[Dict[str, str]]) -> str:
    lines = []
    for s in services:
        name = s.get("name","").strip()
        dur = s.get("duration") or s.get("duration_minutes") or ""
        price = s.get("price","")
        extra = []
        if dur:
            extra.append(f"{dur} min")
        if price:
            extra.append(f"{price}€")
        meta = f" ({', '.join(extra)})" if extra else ""
        if name:
            lines.append(f"• {name}{meta}")
    return "\n".join(lines)

def pick_service_from_text(services: List[Dict[str, str]], text: str) -> Optional[Dict[str, str]]:
    t = text.lower()
    # match semplice: se il nome è contenuto nel testo
    for s in services:
        n = (s.get("name","") or "").strip()
        if n and n.lower() in t:
            return s
    # euristica uomo: se scrive "barba" e abbiamo un servizio barba/combo
    if "barba" in t:
        for s in services:
            n = (s.get("name","") or "").lower()
            if "barba" in n:
                return s
    # "taglio" / "capelli"
    if "taglio" in t or "capelli" in t:
        for s in services:
            n = (s.get("name","") or "").lower()
            if "taglio" in n or "capelli" in n:
                return s
    return None

def handle_message(shop: Dict[str, str], customer_phone_raw: str, text: str) -> str:
    shop_id = shop["shop_id"]
    tzname = shop.get("timezone") or DEFAULT_TIMEZONE
    slot_minutes = int(shop.get("slot_minutes") or 30)

    customer_phone = normalize_phone(customer_phone_raw)
    msg = (text or "").strip()
    tlow = msg.lower().strip()

    services = get_shop_services(shop_id)
    customer = load_customer(shop_id, customer_phone)
    sess = load_session(shop_id, customer_phone)
    state = (sess.get("state") if sess else "") or ""
    data = {}
    if sess and sess.get("data"):
        try:
            data = json.loads(sess["data"])
        except Exception:
            data = {}

    # cancel
    if any(w in tlow for w in CANCEL_WORDS):
        reset_session(shop_id, customer_phone)
        return "Va bene 👍 Ho annullato. Dimmi giorno e ora (es. “domani 18:00”) oppure una fascia (es. “mercoledì dopo le 18”)."

    # greeting / help
    if tlow in {"ciao","salve","buongiorno","buonasera","hey"} and not state:
        base = f"Ciao! 👋\nSei in contatto con *{shop.get('name','il salone')}* 💈\n"
        if customer and customer.get("last_service"):
            base += f"\nUltimo servizio: *{customer.get('last_service')}*.\n"
        # se un solo servizio (classico barber “taglio uomo”), non chiedere
        if len(services) <= 1:
            return base + "\nDimmi quando vuoi prenotare 😊"
        # se più servizi, chiedi servizio
        return base + "\nChe servizio desideri?\n" + render_services(services)

    # =========================
    # STEP 1: capire servizio
    # =========================
    chosen_service = None
    if len(services) <= 1:
        chosen_service = services[0] if services else {"name": "Appuntamento", "duration": str(slot_minutes), "service_id":"default"}
    else:
        chosen_service = pick_service_from_text(services, msg)
        if not chosen_service and not data.get("service"):
            save_session(shop_id, customer_phone, "need_service", data)
            return "Che servizio desideri?\n" + render_services(services)
        if not chosen_service:
            chosen_service = data.get("service")

    if not chosen_service:
        chosen_service = {"name": "Appuntamento", "duration": str(slot_minutes), "service_id":"default"}

    # salva service in session se non c’è
    data["service"] = chosen_service

    # =========================
    # STEP 2: estrai data/ora/vincoli
    # =========================
    preferred_date = parse_date(msg, tzname)
    preferred_time = parse_time(msg)
    after, before = parse_time_window(msg)

    # se scrive "domani alle 18" -> date + time ok
    # se scrive "stasera" senza ora -> metti fascia sera e chiedi ora se necessario
    wants_some_time = bool(preferred_time or after or before)
    wants_some_date = bool(preferred_date)

    # =========================
    # STATE: confirm
    # =========================
    if state == "confirm":
        if tlow in CONFIRM_WORDS:
            chosen_iso = data.get("chosen_iso")
            if not chosen_iso:
                reset_session(shop_id, customer_phone)
                return "Ops, ho perso lo slot 😅 Dimmi di nuovo giorno e ora."
            try:
                start = dt.datetime.fromisoformat(chosen_iso)
            except Exception:
                reset_session(shop_id, customer_phone)
                return "Ops, errore interno. Dimmi giorno e ora."
            duration = int(chosen_service.get("duration") or chosen_service.get("duration_minutes") or slot_minutes)
            end = start + dt.timedelta(minutes=duration)
            cal_id = shop.get("calendar_id","")
            cap = int(shop.get("capacity") or 1)
            if not is_slot_available(cal_id, start, end, cap):
                reset_session(shop_id, customer_phone)
                return "Quello slot è appena stato preso 😅 Vuoi che ti proponga altri orari?"

            summary = chosen_service.get("name","Appuntamento")
            ev_id = create_event(cal_id, start, end, summary, customer_phone, chosen_service.get("service_id",""))
            upsert_customer_after_booking(shop_id, customer_phone, summary, start.isoformat())
            reset_session(shop_id, customer_phone)
            return f"✅ Appuntamento confermato!\n💈 {summary}\n🕒 {fmt_dt_local(start, tzname)}\n\nA presto 👋"

        # se non conferma, lo trattiamo come nuova preferenza
        state = ""
        save_session(shop_id, customer_phone, "", data)

    # =========================
    # STATE: choose (lista)
    # =========================
    if state == "choose":
        options = data.get("options", []) or []
        m = re.search(r"\b(\d{1,2})\b", tlow)
        if m and options:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(options):
                chosen_iso = options[idx]
                data["chosen_iso"] = chosen_iso
                save_session(shop_id, customer_phone, "confirm", data)
                start = dt.datetime.fromisoformat(chosen_iso)
                return (
                    "Confermi questo appuntamento?\n"
                    f"💈 {chosen_service.get('name','Appuntamento')}\n"
                    f"🕒 {fmt_dt_local(start, tzname)}\n\n"
                    "Rispondi OK per confermare oppure “annulla”."
                )

        # se non ha scelto numero, continuiamo interpretando msg come nuova richiesta
        save_session(shop_id, customer_phone, "", data)

    # =========================
    # Se manca la data
    # =========================
    if not wants_some_date and wants_some_time:
        save_session(shop_id, customer_phone, "need_date", data | {"after": (after.isoformat() if after else ""), "before": (before.isoformat() if before else ""), "preferred_time": (preferred_time.isoformat() if preferred_time else "")})
        return "Ok 👍 Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # =========================
    # Se manca l’ora
    # =========================
    if wants_some_date and not wants_some_time:
        save_session(shop_id, customer_phone, "need_time", data | {"preferred_date": preferred_date.isoformat()})
        return "Perfetto 👍 A che ora preferisci? (es. 18:00) oppure dimmi una fascia (es. “dopo le 18”)."

    # =========================
    # STATE: need_date
    # =========================
    if state == "need_date":
        if preferred_date:
            # recupera eventuale time/fascia salvata
            pt = data.get("preferred_time","")
            af = data.get("after","")
            bf = data.get("before","")
            pt_t = dt.time.fromisoformat(pt) if pt else None
            af_t = dt.time.fromisoformat(af) if af else None
            bf_t = dt.time.fromisoformat(bf) if bf else None

            slots = find_best_slots(shop, chosen_service, preferred_date, pt_t, af_t, bf_t, MAX_DAYS_LOOKAHEAD, limit=5)
            if not slots:
                return "Non vedo disponibilità in quel giorno/fascia. Vuoi un altro giorno o un altro orario?"
            options = [s.isoformat() for s in slots]
            data["options"] = options
            save_session(shop_id, customer_phone, "choose", data)
            return render_slots(slots, tzname, title="Perfetto 👍 Ecco alcune disponibilità:")

        return "Ok 👍 Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # =========================
    # STATE: need_time
    # =========================
    if state == "need_time":
        pd_iso = data.get("preferred_date","")
        pd = dt.date.fromisoformat(pd_iso) if pd_iso else preferred_date
        if not pd:
            return "Che giorno preferisci? (es. “domani”, “17/12”)."

        # qui l’utente ora dovrebbe dare ora o fascia
        if preferred_time:
            slots = find_best_slots(shop, chosen_service, pd, preferred_time, None, None, MAX_DAYS_LOOKAHEAD, limit=5)
        else:
            a2, b2 = parse_time_window(msg)
            slots = find_best_slots(shop, chosen_service, pd, None, a2, b2, MAX_DAYS_LOOKAHEAD, limit=5)

        if not slots:
            return "Non vedo disponibilità in quella fascia. Vuoi un altro orario o un altro giorno?"

        # se era una richiesta “precisa” ed è pieno, la prima riga spiega bene l’alternativa
        options = [s.isoformat() for s in slots]
        data["options"] = options
        save_session(shop_id, customer_phone, "choose", data)

        if preferred_time:
            return render_slots(slots, tzname, title=f"A quell’ora non posso 😕 Ma posso proporti questi orari vicini:")
        return render_slots(slots, tzname, title="Perfetto 👍 Ecco alcune disponibilità:")

    # =========================
    # FLOW NORMALE: ho data+ora o data+fascia
    # =========================
    if preferred_date and preferred_time:
        slots = find_best_slots(shop, chosen_service, preferred_date, preferred_time, None, None, MAX_DAYS_LOOKAHEAD, limit=5)
        if not slots:
            return "A quell’ora non ho disponibilità. Dimmi una fascia (es. “dopo le 18”) o un altro giorno."
        # se il primo è l’orario preciso, la funzione avrebbe già ritornato [start0]
        # quindi se siamo qui, è pieno e stiamo proponendo alternative.
        data["options"] = [s.isoformat() for s in slots]
        save_session(shop_id, customer_phone, "choose", data)
        return render_slots(slots, tzname, title="A quell’ora non posso 😕 Se vuoi posso proporti:")

    if preferred_date and (after or before):
        slots = find_best_slots(shop, chosen_service, preferred_date, None, after, before, MAX_DAYS_LOOKAHEAD, limit=5)
        if not slots:
            return "Non vedo disponibilità in quella fascia. Vuoi un altro orario o un altro giorno?"
        data["options"] = [s.isoformat() for s in slots]
        save_session(shop_id, customer_phone, "choose", data)
        return render_slots(slots, tzname, title="Perfetto 👍 Ecco alcune disponibilità:")

    # se non ha dato abbastanza info
    save_session(shop_id, customer_phone, "", data)
    if len(services) > 1 and not pick_service_from_text(services, msg):
        return "Che servizio desideri?\n" + render_services(services)
    return "Dimmi giorno e ora (es. “domani 18:00”) oppure una fascia (es. “mercoledì dopo le 18”)."


def render_slots(slots: List[dt.datetime], tzname: str, title: str) -> str:
    lines = [title]
    for i, s in enumerate(slots, start=1):
        lines.append(f"{i}) {fmt_dt_local(s, tzname)}")
    lines.append("\nRispondi con il numero (1,2,3…) oppure scrivi un’altra preferenza.")
    return "\n".join(lines)

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"

@app.route("/test", methods=["GET"])
def test():
    """
    Test via browser:
    /test?shop_id=barber_test&phone=393481111111&msg=domani%20alle%2018
    """
    shop_id = (request.args.get("shop_id") or "").strip()
    phone = request.args.get("phone","").strip()
    msg = request.args.get("msg","").strip()

    # se non passo shop_id: usa il primo shop del foglio
    if not shop_id:
        shops = load_shops()
        if not shops:
            return jsonify({"error":"nessuno shop configurato"}), 400
        shop = shops[0]
    else:
        shop = shop_by_id(shop_id)
        if not shop:
            return jsonify({"error":"shop_id non trovato"}), 404

    if not phone or not msg:
        return jsonify({
            "error":"usa ?shop_id=...&phone=...&msg=...",
            "example":"/test?shop_id=barber_test&phone=393481111111&msg=domani%20alle%2018"
        }), 400

    reply = handle_message(shop, phone, msg)
    return jsonify({
        "shop": shop.get("name",""),
        "shop_id": shop.get("shop_id",""),
        "phone": normalize_phone(phone),
        "message_in": msg,
        "bot_reply": reply
    })

@app.route("/meta/webhook", methods=["GET", "POST"])
def meta_webhook():
    """
    Endpoint per WhatsApp Cloud API (Meta).
    - GET: verifica webhook (hub.challenge)
    - POST: riceve messaggi (JSON)
    """
    # verifica webhook
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        verify_token = os.getenv("META_VERIFY_TOKEN","")

        if mode == "subscribe" and token == verify_token:
            return challenge or "", 200
        return "forbidden", 403

    # ricezione messaggi
    payload = request.get_json(silent=True) or {}
    _log("META payload", payload)

    # Nota: per rispondere davvero via Meta serve chiamare Graph API /messages.
    # Qui facciamo solo parsing e log (perché la tua WABA è in restrizione).
    # Quando sarà sbloccata, aggiungiamo send_message().
    return "ok", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT","8080"))
    app.run(host="0.0.0.0", port=port)
