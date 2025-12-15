from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os
from datetime import datetime, timedelta

app = Flask(__name__)

# =========================
# MEMORIA BREVE (per numero WhatsApp)
# =========================
SESSIONS = {}

# =========================
# CONFIG
# =========================
SLOT_MINUTES = 30
WORK_START = 9
WORK_END = 19

# =========================
# FUNZIONI
# =========================
def get_next_slots():
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    slots = []
    start = now.replace(hour=WORK_START)

    if now.hour >= WORK_START:
        start = now + timedelta(minutes=30)

    while start.hour < WORK_END:
        slots.append(start.strftime("%H:%M"))
        start += timedelta(minutes=SLOT_MINUTES)

    return slots[:3]

# =========================
# WEBHOOK WHATSAPP
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get("From")
    body = request.form.get("Body", "").strip().lower()

    session = SESSIONS.get(from_number, {"step": "start"})

    response = MessagingResponse()
    msg = response.message()

    # STEP 1 – Saluto
    if session["step"] == "start":
        msg.body(
            "Ciao 👋\n"
            "Sono il servizio prenotazioni del barbiere 💈\n\n"
            "Vuoi prenotare un *taglio uomo*?"
        )
        session["step"] = "ask_booking"

    # STEP 2 – Vuole prenotare
    elif session["step"] == "ask_booking":
        if "si" in body or "ok" in body or "prenota" in body:
            slots = get_next_slots()
            msg.body(
                "Perfetto ✂️\n"
                "Ecco i primi orari disponibili oggi:\n\n"
                f"1️⃣ {slots[0]}\n"
                f"2️⃣ {slots[1]}\n"
                f"3️⃣ {slots[2]}\n\n"
                "Rispondi con il numero dell’orario che preferisci."
            )
            session["slots"] = slots
            session["step"] = "choose_slot"
        else:
            msg.body("Dimmi pure quando vuoi prenotare 👍")

    # STEP 3 – Scelta slot
    elif session["step"] == "choose_slot":
        if body in ["1", "2", "3"]:
            slot = session["slots"][int(body) - 1]
            msg.body(
                f"✅ Appuntamento confermato!\n\n"
                f"📅 Oggi alle {slot}\n"
                "⏱ Durata: 30 minuti\n\n"
                "Ti aspettiamo 💈"
            )
            session["step"] = "done"
        else:
            msg.body("Per favore rispondi con 1, 2 o 3.")

    SESSIONS[from_number] = session
    return str(response)

# =========================
# HEALTH CHECK
# =========================
@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
