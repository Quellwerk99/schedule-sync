#!/usr/bin/env python3
"""
schedule-sync
=============
Fragt einen öffentlichen Termin-Endpunkt ab und schickt eine Push-Nachricht
(ntfy), sobald ein Eintrag frei und buchbar ist.

Zwei Betriebsarten:
  python sync.py           -> Dauerschleife (lokal / Raspberry Pi)
  python sync.py --once    -> Einmal prüfen und beenden (CI-Zeitplan)

Konfiguration ausschliesslich über Umgebungsvariablen / Secrets – es stehen
bewusst keine Endpunkte im Code:
  NTFY_TOPIC            (Pflicht)  Ziel-Topic für die Push-Nachricht
  DATA_URL              (Pflicht)  Quell-Endpunkt
  ITEM_PREFIX           (optional) Präfix-Filter auf die Eintrags-Nummer
  DETAIL_URL_TEMPLATE   (optional) Link pro Eintrag, mit {} als ID-Platzhalter
  PAGE_URL              (optional) Übersichtsseite als Fallback-Link
"""

import os
import re
import sys
import json
import time
import datetime as dt
import xml.etree.ElementTree as ET

import requests

# ============================== KONFIG ======================================

# Quell-Endpunkt – kommt aus dem Secret DATA_URL.
DATA_URL = (os.environ.get("DATA_URL") or "").strip()

# Präfix der Kursnummer, auf das gefiltert wird.
ITEM_PREFIX = os.environ.get("ITEM_PREFIX") or "95"

# ntfy-Topic – kommt aus dem Secret, nie hart eintragen.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"

# Titel der Push-Nachricht (bewusst unspezifisch gehalten)
NOTIFY_TITLE = "🎾 Slot frei"

# Nur bestimmte Termine melden? Beispiele:
#   FILTER_DAYS  = ["Di", "Do"]        -> nur Dienstag & Donnerstag
#   FILTER_HOURS = [18, 19, 20]        -> nur Startzeiten 18-20 Uhr
# Leere Listen = alles melden.
FILTER_DAYS: list[str] = []
FILTER_HOURS: list[int] = []

# Nur Termine innerhalb der nächsten X Tage melden. Weiter entfernte Einträge
# sind Karteileichen alter Termine (im Datensatz fehlt die Jahresangabe).
MAX_DAYS_AHEAD = 14

# Maximale Anzahl Einträge in der Push-Nachricht (längere Nachrichten stellt
# ntfy sonst als Datei-Anhang zu)
MAX_ITEMS_IN_MESSAGE = 8

# Prüfintervall in Sekunden im Dauerschleifen-Modus
CHECK_INTERVAL = 120

# Merker-Datei, damit nicht mehrfach für denselben Eintrag benachrichtigt wird
STATE_FILE = "state.json"

# Wiederholungsversuche bei Abruf-Fehlern (Quell-Server ist zeitweise überlastet)
FETCH_RETRIES = 3
FETCH_RETRY_WAIT = 20  # Sekunden zwischen den Versuchen

# Links pro Eintrag bzw. Übersichtsseite – ebenfalls aus Secrets.
DETAIL_URL = (os.environ.get("DETAIL_URL_TEMPLATE") or "").strip()
FALLBACK_URL = (os.environ.get("PAGE_URL") or "").strip()

# ============================================================================

WEEKDAYS = {"Mo": 0, "Di": 1, "Mi": 2, "Do": 3, "Fr": 4, "Sa": 5, "So": 6}
HEADERS = {"User-Agent": "Mozilla/5.0 (availability check)"}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%d.%m. %H:%M:%S')}] {msg}",
          flush=True)


def iso_week_monday(year: int, week: int) -> dt.date:
    return dt.date.fromisocalendar(year, week, 1)


def parse_item_datetime(kursnr: str, tag: str, uhrzeit: str) -> dt.datetime | None:
    """Rekonstruiert Datum+Uhrzeit. Die Kalenderwoche steckt in kursnr[2:4],
    der Wochentag in <tag>, die Zeit in <uhrzeit> – ein Jahr gibt es im
    Datensatz nicht! Deshalb: beide Jahreskandidaten bilden und den ersten
    nehmen, der in der Zukunft liegt. Ob das Ergebnis plausibel nah ist,
    prüft später das MAX_DAYS_AHEAD-Fenster."""
    try:
        week = int(kursnr[2:4])
        m = re.match(r"(\d{1,2})[.:](\d{2})", uhrzeit.strip())
        hour, minute = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        now = dt.datetime.now()
        for year in (now.year, now.year + 1):
            try:
                day = (iso_week_monday(year, week)
                       + dt.timedelta(days=WEEKDAYS.get(tag, 0)))
            except ValueError:
                continue  # z. B. KW 53 in einem Jahr ohne KW 53
            candidate = dt.datetime(day.year, day.month, day.day, hour, minute)
            if candidate > now:
                return candidate
        return None
    except Exception:
        return None


def fetch_xml_with_retries() -> ET.Element:
    """Holt die Daten mit Wiederholungsversuchen, da der Quell-Server
    zeitweise überlastet ist oder Fehler liefert."""
    last_error: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            r = requests.post(DATA_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return ET.fromstring(r.content)
        except Exception as e:
            last_error = e
            log(f"Abruf fehlgeschlagen (Versuch {attempt}/{FETCH_RETRIES}): {e}")
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_WAIT)
    raise last_error  # type: ignore[misc]


def fetch_items() -> list[dict]:
    """Holt alle buchbaren, zukünftigen Einträge."""
    root = fetch_xml_with_retries()

    items = []
    now = dt.datetime.now()

    for angebot in root.iter("angebot"):
        kursnr = (angebot.findtext("kursnr") or "").strip()
        if not kursnr.startswith(ITEM_PREFIX):
            continue

        frei = (angebot.findtext("frei") or "").strip() == "1"
        buchung = (angebot.findtext("buchung") or "").strip() == "1"
        if not (frei and buchung):
            continue

        tag = (angebot.findtext("tag") or "").strip()
        uhrzeit = (angebot.findtext("uhrzeit") or "").strip()
        kursid = (angebot.findtext("kursid") or "").strip()
        details = (angebot.findtext("details") or "").strip()
        raum = (angebot.findtext("raum") or "").strip()

        start = parse_item_datetime(kursnr, tag, uhrzeit)
        if start is None or start <= now:
            continue  # vergangene Termine ignorieren
        if start > now + dt.timedelta(days=MAX_DAYS_AHEAD):
            continue  # Karteileichen / unplausibel weit entfernte Termine

        if FILTER_DAYS and tag not in FILTER_DAYS:
            continue
        if FILTER_HOURS and start.hour not in FILTER_HOURS:
            continue

        items.append({
            "id": kursid or kursnr,
            "start": start.isoformat(),
            "text": (f"{tag} {start.strftime('%d.%m.')} um "
                     f"{uhrzeit.replace('.', ':')} Uhr"
                     + (f" – {raum}" if raum else "")
                     + (f" ({details})" if details else "")),
            "link": (DETAIL_URL.format(kursid)
                     if (DETAIL_URL and kursid) else FALLBACK_URL),
        })

    return items


def load_state() -> set[str]:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_state(ids: set[str]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f)


def notify(items: list[dict]) -> None:
    items = sorted(items, key=lambda s: s["start"])
    shown = items[:MAX_ITEMS_IN_MESSAGE]
    lines = [f"• {s['text']}" + (f"\n  {s['link']}" if s["link"] else "")
             for s in shown]
    if len(items) > len(shown):
        lines.append(f"… und {len(items) - len(shown)} weitere Einträge.")
    message = "\n".join(lines)
    title = f"{NOTIFY_TITLE} ({len(items)})"
    log(f"FREIE EINTRÄGE:\n{message}")
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "high",
                "Tags": "tennis,tada",
                "Click": (items[0]["link"] if len(items) == 1
                          else (FALLBACK_URL or items[0]["link"])),
            },
            timeout=15,
        )
        log("→ Push gesendet")
    except Exception as e:
        log(f"ntfy-Fehler: {e}")


def check_once() -> None:
    items = fetch_items()
    notified = load_state()

    current_ids = {s["id"] for s in items}
    new_items = [s for s in items if s["id"] not in notified]

    if new_items:
        notify(new_items)
    else:
        log(f"Nichts Neues ({len(items)} freie(r) Eintrag/Einträge insgesamt).")

    # Nur noch aktuelle Einträge merken -> wird einer wieder belegt und später
    # erneut frei, gibt es wieder eine Benachrichtigung.
    save_state(current_ids)


def main() -> None:
    missing = [n for n, v in (("NTFY_TOPIC", NTFY_TOPIC),
                              ("DATA_URL", DATA_URL)) if not v]
    if missing:
        print(f"⚠️  Fehlende Konfiguration: {', '.join(missing)}")
        sys.exit(1)

    if "--once" in sys.argv:
        try:
            check_once()
        except Exception as e:
            # Transienter Fehler (z. B. Quell-Server down): sauber beenden,
            # der nächste geplante Lauf versucht es erneut.
            log(f"Prüfung diesmal nicht möglich: {e}")
        return

    log(f"Gestartet – prüfe alle {CHECK_INTERVAL}s.")
    errors = 0
    while True:
        try:
            check_once()
            errors = 0
        except Exception as e:
            errors += 1
            log(f"Fehler ({errors}x): {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
