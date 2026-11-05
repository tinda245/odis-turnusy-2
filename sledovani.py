import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import warnings
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# Úplné potlačení jakýchkoliv systémových varování a chybových výpisů
warnings.filterwarnings("ignore")
sys.stderr = open(os.devnull, "w")

# --- NASTAVENÍ ---
DOBA_BEHU_HODIN = 5.8
INTERVAL_SEKUND = 45
VYSTUPNI_SOUBOR = "turnusy_hotovo.csv"
DIAGNOSTIKA_SOUBOR = "ukazka_dat.json"
DB_SOUBOR = "odis_data.db"
MAPA_URL = "https://mpvnet.cz/odis/map"

# Regulární výraz pro rozpoznání české SPZ (RZ), např. 8T2 1234 nebo 1TI 5432
SPZ_REGEX = re.compile(r"\b[1-9][A-Z0-9]{1,2}[\s\-]?\d{4}\b", re.IGNORECASE)


def inicializuj_db():
    conn = sqlite3.connect(DB_SOUBOR)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS odjete_spoje_v2 (
            datum TEXT,
            predvolba TEXT,
            spz TEXT,
            linka TEXT,
            spoj TEXT,
            zacatek TEXT,
            konec TEXT,
            PRIMARY KEY (datum, predvolba, spz, linka, spoj)
        )
    """)
    conn.commit()
    return conn


def klic_pro_razeni_predvoleb(hodnota):
    """Převede předvolbu na klíč pro přirozené číselné i abecední seřazení."""
    casti = re.split(r"(\d+)", str(hodnota or ""))
    return [int(cast) if cast.isdigit() else cast.lower() for cast in casti]


def vyexportuj_turnusy_do_csv(conn):
    try:
        c = conn.cursor()
        c.execute("""
            SELECT datum, predvolba, spz, linka, spoj, zacatek, konec
            FROM odjete_spoje_v2
            ORDER BY datum, zacatek
        """)
        radky = c.fetchall()

        turnusy = {}
        for datum, predvolba, spz, linka, spoj, zacatek, konec in radky:
            klic = (datum, predvolba, spz)
            if klic not in turnusy:
                turnusy[klic] = []
            turnusy[klic].append(f"{zacatek[:5]}-{konec[:5]} (L{linka}/{spoj})")

        # Seřazení všech turnusů primárně podle PŘEDVOLBY, sekundárně podle SPZ
        serazene_klice = sorted(
            turnusy.keys(),
            key=lambda x: (x[0], klic_pro_razeni_predvoleb(x[1]), x[2])
        )

        with open(VYSTUPNI_SOUBOR, mode="w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow([
                "Předvolba",
                "SPZ",
                "Datum",
                "Počet spojů",
                "Celý denní turnus (Od-Do Linka/Spoj)"
            ])
            for klic in serazene_klice:
                datum, predvolba, spz = klic
                spoje = turnusy[klic]
                writer.writerow([predvolba, spz, datum, len(spoje), " | ".join(spoje)])

        return len(serazene_klice)
    except Exception:
        return 0


def uloz_na_github(zprava):
    try:
        FNULL = subprocess.DEVNULL
        subprocess.run(["git", "config", "user.name", "ODIS-Automat"], stdout=FNULL, stderr=FNULL, check=False)
        subprocess.run(["git", "config", "user.email", "automat@users.noreply.github.com"], stdout=FNULL, stderr=FNULL, check=False)
        subprocess.run(["git", "add", VYSTUPNI_SOUBOR, DB_SOUBOR, DIAGNOSTIKA_SOUBOR], stdout=FNULL, stderr=FNULL, check=False)
        subprocess.run(["git", "commit", "-m", zprava], stdout=FNULL, stderr=FNULL, check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], stdout=FNULL, stderr=FNULL, check=False)
        subprocess.run(["git", "push"], stdout=FNULL, stderr=FNULL, check=False)
    except Exception:
        pass


def najdi_spz_v_objektu(v):
    """Získá SPZ z klíčů objektu nebo ji vyhledá v textových polích."""
    for k in ("rz", "spz", "reg", "plate", "regNum", "registration", "car", "evc", "vId", "vehicle", "label", "name"):
        hodnota = v.get(k)
        if hodnota and str(hodnota).strip():
            return str(hodnota).strip()

    for hodnota in v.values():
        if isinstance(hodnota, str):
            shoda = SPZ_REGEX.search(hodnota)
            if shoda:
                return shoda.group(0).upper()

    return str(v.get("id") or v.get("num") or "Neznámá SPZ").strip()


def najdi_predvolbu_v_objektu(v, linka, spoj):
    """Získá předvolbu (kurz/pořadí) z dat vozidla, případně ji složí z linky a kurzu."""
    for k in ("predvolba", "preset", "kurz", "course", "poradi", "order", "turnus", "block", "duty", "code", "cnum", "obeh", "p"):
        hodnota = v.get(k)
        if hodnota and str(hodnota).strip():
            return str(hodnota).strip()

    # Hledání předvolby v popisku / tooltipu, pokud je součástí textu
    for hodnota in v.values():
        if isinstance(hodnota, str):
            m = re.search(r"(?:předvolba|kurz|pořadí|oběh)\s*[:\-]?\s*([A-Z0-9\/\-]+)", hodnota, re.IGNORECASE)
            if m:
                return m.group(1).strip()

    # Standardizovaná předvolba z linky a pořadí spoje (např. 045/01), aby šlo vždy přesně řadit
    cista_linka = str(linka).zfill(3) if str(linka).isdigit() else str(linka)
    cisty_spoj = str(spoj).zfill(2) if str(spoj).isdigit() else str(spoj)
    return f"{cista_linka}/{cisty_spoj}"


def extrahuj_vozy_z_dat(obj):
    """Rekurzivně projde JSON data z mapy a vytáhne všechny objekty autobusů."""
    nalezeno = []

    def prohledej(o):
        if isinstance(o, dict):
            klice_malymi = {str(k).lower(): v for k, v in o.items()}
            ma_linku = any(k in klice_malymi for k in ("line", "linka", "l", "route", "ln", "linename", "lineshort"))
            ma_polohu_nebo_spoj = any(
                k in klice_malymi for k in ("lat", "lon", "lng", "x", "y", "conn", "spoj", "cn", "trip", "c", "rz", "spz", "reg", "id")
            )
            if ma_linku and ma_polohu_nebo_spoj:
                nalezeno.append(o)
            else:
                for hodnota in o.values():
                    prohledej(hodnota)
        elif isinstance(o, list):
            for polozka in o:
                prohledej(polozka)

    try:
        prohledej(obj)
    except Exception:
        pass
    return nalezeno


def spust_automat():
    conn = inicializuj_db()
    c = conn.cursor()
    pocet_cyklu = int((DOBA_BEHU_HODIN * 3600) / INTERVAL_SEKUND)
    ulozit_kazdych_cyklu = 20

    zachycene_jsony = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 2560, "height": 1440},
                locale="cs-CZ",
                timezone_id="Europe/Prague"
            )
            page = context.new_page()

            def zachyt_odpoved(response):
                try:
                    if response.request.resource_type in ("xhr", "fetch", "document", "other"):
                        url_low = response.url.lower()
                        if any(koncovka in url_low for koncovka in (".png", ".jpg", ".css", ".woff", ".svg", ".ico", "tile")):
                            return
                        text = response.text()
                        if text:
                            ocisteny = text.strip()
                            if ocisteny.startswith("{") or ocisteny.startswith("["):
                                data = json.loads(ocisteny)
                                zachycene_jsony.append({"url": response.url, "data": data})
                except Exception:
                    pass

            page.on("response", zachyt_odpoved)

            try:
                page.goto(MAPA_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)

                # Přiblížení mapy, aby server aktivoval posílání jednotlivých vozidel
                page.mouse.move(1280, 720)
                for _ in range(3):
                    page.mouse.wheel(0, -500)
                    page.wait_for_timeout(1000)
            except Exception:
                pass

            for i in range(pocet_cyklu):
                nyni = datetime.now(ZoneInfo("Europe/Prague"))
                datum_str = nyni.strftime("%Y-%m-%d")
                cas_str = nyni.strftime("%H:%M:%S")

                try:
                    # Aktivní impulz do mapy pro vyžádání čerstvých poloh vozů
                    page.mouse.move(1200 + (i % 60), 700 + (i % 60))
                    if not zachycene_jsony:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(5000)
                        page.mouse.move(1280, 720)
                        page.mouse.wheel(0, -600)
                        page.wait_for_timeout(2000)

                    while zachycene_jsony:
                        balicek = zachycene_jsony.pop(0)
                        data = balicek["data"]

                        if not os.path.exists(DIAGNOSTIKA_SOUBOR):
                            try:
                                with open(DIAGNOSTIKA_SOUBOR, "w", encoding="utf-8") as f:
                                    json.dump(balicek, f, ensure_ascii=False, indent=2)
                            except Exception:
                                pass

                        vozidla = extrahuj_vozy_z_dat(data)

                        for v in vozidla:
                            linka = str(
                                v.get("line") or v.get("linka") or v.get("l")
                                or v.get("route") or v.get("ln") or v.get("lineName") or ""
                            ).strip()

                            if not linka:
                                continue

                            spoj = str(
                                v.get("conn") or v.get("spoj") or v.get("cn")
                                or v.get("trip") or v.get("c") or v.get("course") or "1"
                            ).strip()

                            spz = najdi_spz_v_objektu(v)
                            predvolba = najdi_predvolbu_v_objektu(v, linka, spoj)

                            c.execute("""
                                INSERT INTO odjete_spoje_v2 (datum, predvolba, spz, linka, spoj, zacatek, konec)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(datum, predvolba, spz, linka, spoj)
                                DO UPDATE SET konec = excluded.konec
                            """, (datum_str, predvolba, spz, linka, spoj, cas_str, cas_str))

                    conn.commit()
                    pocet_turnusu = vyexportuj_turnusy_do_csv(conn)
                    print(f"[{cas_str}] Cyklus {i+1}/{pocet_cyklu}: Evidováno {pocet_turnusu} turnusů (seřazeno podle předvoleb).")

                    if i == 0 or (i + 1) % ulozit_kazdych_cyklu == 0:
                        uloz_na_github(f"Aktualizace turnusů ({cas_str})")

                except Exception:
                    pass

                time.sleep(INTERVAL_SEKUND)

            try:
                browser.close()
            except Exception:
                pass
    except Exception:
        pass

    uloz_na_github("Uložení po dokončení bloku")
    try:
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        spust_automat()
    except Exception:
        pass
    sys.exit(0)
