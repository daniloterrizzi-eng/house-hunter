import json
import os
import re
import sqlite3
import time
import pandas as pd
import requests
from bs4 import BeautifulSoup
from DrissionPage import ChromiumPage, ChromiumOptions

# Default Turso Credentials with Environment Variable Overrides
TURSO_URL = os.getenv("TURSO_URL", "libsql://house-hunter-daniloterrizzi-eng.aws-eu-west-1.turso.io")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODY4ODE2NjcsImlkIjoiMDFhMDBhNjAtODkwMS03MDI0LWEyMWYtNTY1OTA2YTYwNThiIiwia2lkIjoiQVl3eDdVUktWLV90SjEyUnFnNHYzYW1RVGszWnc0Z042UnR1ZFdwNWgtMCIsInJpZCI6ImU5NDFlZjAyLWU0MWYtNDJiOS05MTRhLWY5NDM5NGNiMzM5YSJ9.rROVpRRKgMekGE6dSiKoDiPUK9lqU0eLAXTVDO56pXc2fo_Cfwy6rGaaGPIv11uAGSv_dOZ3c_1wNdBtp8eiBQ")

MAX_PAGINE = 50

RICERCHE = {
    "Coppia": "https://www.immobiliare.it/vendita-case/torino/?prezzoMassimo=340000&superficieMinima=80&localiMinimo=3&stato=6&balconeOterrazzo=1&tipoProprieta=1&noAste=1&classeEnergetica=8&idMZona[0]=194&idMZona[1]=181&idMZona[2]=173&idMZona[3]=174&idMZona[4]=172&idMZona[5]=177&idMZona[6]=178&idMZona[7]=183&idQuartiere[0]=682&idQuartiere[1]=667&idQuartiere[2]=663"
}

class TursoDB:
    def __init__(self, db_url, token):
        self.endpoint = db_url.replace("libsql://", "https://") + "/v2/pipeline"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def execute(self, sql, params=None):
        args = []
        if params:
            for p in params:
                if p is None:
                    args.append({"type": "null"})
                elif isinstance(p, int):
                    args.append({"type": "integer", "value": str(p)})
                elif isinstance(p, float):
                    args.append({"type": "float", "value": p})
                else:
                    args.append({"type": "text", "value": str(p)})

        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": args}},
                {"type": "close"}
            ]
        }

        res = requests.post(self.endpoint, json=payload, headers=self.headers)
        res.raise_for_status()
        data = res.json()

        result = data["results"][0]["response"]["result"]
        cols = [col["name"] for col in result.get("cols", [])]
        rows = []
        for raw_row in result.get("rows", []):
            parsed_row = []
            for cell in raw_row:
                c_type = cell.get("type")
                if c_type == "null":
                    parsed_row.append(None)
                elif c_type == "integer":
                    parsed_row.append(int(cell.get("value")))
                elif c_type == "float":
                    parsed_row.append(float(cell.get("value")))
                else:
                    parsed_row.append(cell.get("value"))
            rows.append(parsed_row)

        return pd.DataFrame(rows, columns=cols)

def get_db():
    if TURSO_URL and TURSO_TOKEN and "yourusername" not in TURSO_URL:
        print("🌐 Connecting to Turso Cloud Database via HTTP...")
        return TursoDB(TURSO_URL, TURSO_TOKEN)
    print("📁 Connecting to local SQLite database (case.db)...")
    return sqlite3.connect("case.db")

def init_db():
    db = get_db()
    sql_create = '''
        CREATE TABLE IF NOT EXISTS annunci (
            id TEXT PRIMARY KEY,
            profilo TEXT,
            titolo TEXT,
            prezzo TEXT,
            prezzo_num INTEGER,
            superficie TEXT,
            superficie_num INTEGER,
            locali TEXT,
            url TEXT,
            foto TEXT DEFAULT '[]',
            stato TEXT DEFAULT 'in review',
            note TEXT DEFAULT '',
            voto_danilo INTEGER DEFAULT 0,
            voto_ragazza INTEGER DEFAULT 0,
            data_scoperta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''
    if isinstance(db, TursoDB):
        db.execute(sql_create)
    else:
        conn = db
        cursor = conn.cursor()
        cursor.execute(sql_create)
        conn.commit()
        conn.close()

def parse_digits(val):
    if not val or val == "N/D":
        return 0
    digits = re.sub(r'[^\d]', '', str(val))
    return int(digits) if digits else 0

def convert_to_hd_url(url: str) -> str:
    if not url:
        return url
    clean_url = url.split('?')[0]
    clean_url = re.sub(r'/thumbnails?/', '/images/', clean_url)
    clean_url = re.sub(r'c-\d+x\d+', 'c-1024x768', clean_url)
    clean_url = re.sub(r'_\d+x\d+\.', '_1024x768.', clean_url)
    clean_url = re.sub(r'/\d+x\d+/', '/1024x768/', clean_url)
    clean_url = re.sub(r'/shape/\d+x\d+/', '/shape/1024x768/', clean_url)
    clean_url = re.sub(r'-thumb\.', '-large.', clean_url)
    clean_url = re.sub(r'/small/', '/large/', clean_url)
    return clean_url

def format_full_url(url: str) -> str:
    if not url:
        return ""
    if not url.startswith("http"):
        return f"https://www.immobiliare.it{url if url.startswith('/') else '/' + url}"
    return url

def cerca_annunci_nel_json(obj):
    annunci = []
    if isinstance(obj, dict):
        if "realEstate" in obj:
            return [obj]
        if "results" in obj and isinstance(obj["results"], list):
            for item in obj["results"]:
                annunci.extend(cerca_annunci_nel_json(item))
        else:
            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    annunci.extend(cerca_annunci_nel_json(value))
    elif isinstance(obj, list):
        for item in obj:
            annunci.extend(cerca_annunci_nel_json(item))
    return annunci

def gestisci_cookie_banner(page):
    try:
        btn = page.ele('text:Accetta', timeout=2) or page.ele('#didomi-notice-agree-button', timeout=1)
        if btn:
            btn.click()
            time.sleep(1)
    except Exception:
        pass

def esegui_scraping():
    init_db()
    db = get_db()
    nuovi_totali = 0

    print("🚀 Avvio DrissionPage...")
    co = ChromiumOptions()
    page = ChromiumPage(co)

    try:
        print("🔗 Connessione iniziale...")
        page.get("https://www.immobiliare.it/")
        gestisci_cookie_banner(page)
        time.sleep(2)

        for profilo, url_ricerca in RICERCHE.items():
            print(f"\n==================================================")
            print(f"Scraping per profilo: {profilo}")
            print(f"==================================================")

            for pag in range(1, MAX_PAGINE + 1):
                url_corrente = f"{url_ricerca}&pag={pag}" if "?" in url_ricerca else f"{url_ricerca}?pag={pag}"

                print(f"\n--- [Pagina {pag}/{MAX_PAGINE}] Caricamento... ---")
                page.get(url_corrente)
                gestisci_cookie_banner(page)
                page.scroll.down(600)
                time.sleep(2.5)

                annunci_pagina = {}

                soup = BeautifulSoup(page.html, "html.parser")
                next_data = soup.find("script", id="__NEXT_DATA__")
                if next_data and next_data.string:
                    try:
                        data_json = json.loads(next_data.string)
                        risultati = cerca_annunci_nel_json(data_json)
                        for r in risultati:
                            re_obj = r.get("realEstate", r)
                            if isinstance(re_obj, dict) and "id" in re_obj:
                                annunci_pagina[str(re_obj["id"])] = re_obj
                    except Exception:
                        pass

                if not annunci_pagina:
                    cards = page.eles('.in-realEstateCard') or page.eles('article')
                    for card in cards:
                        try:
                            link_ele = card.ele('a[href*="/annunci/"]')
                            if not link_ele: continue
                            link = link_ele.attr('href')
                            id_match = re.search(r'/annunci/(\d+)', link)
                            if not id_match: continue
                            
                            id_annuncio = id_match.group(1)
                            titolo = link_ele.attr('title') or link_ele.text or "Annuncio Immobiliare"
                            
                            price_ele = card.ele('.in-realEstateCard__price') or card.ele('text:€')
                            prezzo = price_ele.text if price_ele else "N/D"

                            img_eles = card.eles('img')
                            foto_list = [convert_to_hd_url(img.attr('src') or img.attr('data-src')) for img in img_eles if img.attr('src') or img.attr('data-src')]

                            features = [f.text for f in card.eles('.in-realEstateCard__feature')]
                            superficie, locali = "N/D", "N/D"
                            for feat in features:
                                if "m²" in feat: superficie = feat.replace("m²", "").strip()
                                elif feat.isdigit(): locali = feat

                            annunci_pagina[id_annuncio] = {
                                "id": id_annuncio,
                                "title": titolo,
                                "price": prezzo,
                                "foto_list": foto_list,
                                "properties": [{"surface": superficie, "rooms": locali, "urls": {"express": link}}]
                            }
                        except Exception:
                            pass

                if not annunci_pagina:
                    print(f"⚠️ Nessun annuncio trovato alla pagina {pag}.")
                    break

                print(f"✅ Estratti {len(annunci_pagina)} annunci!")

                nuovi_pagina = 0
                for id_annuncio, real_estate in annunci_pagina.items():
                    titolo = real_estate.get("title", "N/D")
                    
                    price_data = real_estate.get("price", {})
                    prezzo = f"{price_data.get('value', 'N/D')} €" if isinstance(price_data, dict) else str(price_data)
                    prezzo_num = parse_digits(prezzo)

                    properties = real_estate.get("properties", [{}])
                    prop = properties[0] if isinstance(properties, list) and len(properties) > 0 else {}
                    
                    superficie_raw = str(prop.get("surface", "N/D")).replace("m²", "").strip()
                    superficie_num = parse_digits(superficie_raw)
                    superficie = f"{superficie_num}" if superficie_num > 0 else "N/D"

                    locali = str(prop.get("rooms", "N/D"))
                    
                    raw_link = prop.get("urls", {}).get("express", "") or real_estate.get("url", "")
                    link = format_full_url(raw_link)

                    foto_urls = []
                    multimedia = prop.get("multimedia", {})
                    photos = multimedia.get("photos", []) if isinstance(multimedia, dict) else []
                    
                    for p in photos:
                        urls_dict = p.get("urls", {})
                        best_url = urls_dict.get("extra_large") or urls_dict.get("large") or urls_dict.get("medium") or urls_dict.get("small")
                        if best_url:
                            foto_urls.append(convert_to_hd_url(best_url))

                    if not foto_urls and "foto_list" in real_estate:
                        foto_urls = [convert_to_hd_url(img) for img in real_estate["foto_list"]]

                    foto_json = json.dumps(foto_urls)

                    sql_check = "SELECT id FROM annunci WHERE id = ?"
                    sql_insert = '''
                        INSERT INTO annunci 
                        (id, profilo, titolo, prezzo, prezzo_num, superficie, superficie_num, locali, url, foto, stato) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in review')
                    '''
                    params_insert = (id_annuncio, profilo, titolo, prezzo, prezzo_num, superficie, superficie_num, locali, link, foto_json)

                    if isinstance(db, TursoDB):
                        existing = db.execute(sql_check, [id_annuncio])
                        if existing.empty:
                            db.execute(sql_insert, params_insert)
                            nuovi_totali += 1
                            nuovi_pagina += 1
                            print(f"   [NUOVO] {titolo} - {prezzo} ({len(foto_urls)} foto HD)")
                    else:
                        conn = db
                        cursor = conn.cursor()
                        cursor.execute(sql_check, (id_annuncio,))
                        if not cursor.fetchone():
                            cursor.execute(sql_insert, params_insert)
                            conn.commit()
                            nuovi_totali += 1
                            nuovi_pagina += 1
                            print(f"   [NUOVO] {titolo} - {prezzo} ({len(foto_urls)} foto HD)")

                print(f"   ↳ {nuovi_pagina} nuovi annunci inseriti.")

        page.quit()

    except Exception as e:
        print(f"❌ Errore: {e}")

    return nuovi_totali

if __name__ == "__main__":
    esegui_scraping()