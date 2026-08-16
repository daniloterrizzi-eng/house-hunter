from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sqlite3
import time
import pandas as pd
import requests
from bs4 import BeautifulSoup
from DrissionPage import ChromiumOptions, ChromiumPage

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
TURSO_URL = os.getenv(
    "TURSO_URL",
    "libsql://house-hunter-daniloterrizzi-eng.aws-eu-west-1.turso.io",
)
TURSO_TOKEN = os.getenv(
    "TURSO_TOKEN",
    "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODY4ODE2NjcsImlkIjoiMDFhMDBhNjAtODkwMS03MDI0LWEyMWYtNTY1OTA2YTYwNThiIiwia2lkIjoiQVl3eDdVUktWLV90SjEyUnFnNHYzYW1RVGszWnc0Z042UnR1ZFdwNWgtMCIsInJpZCI6ImU5NDFlZjAyLWU0MWYtNDJiOS05MTRhLWY5NDM5NGNiMzM5YSJ9.rROVpRRKgMekGE6dSiKoDiPUK9lqU0eLAXTVDO56pXc2fo_Cfwy6rGaaGPIv11uAGSv_dOZ3c_1wNdBtp8eiBQ",
)

MAX_PAGINE = 50

RICERCHE = {
    "Coppia": (
        "https://www.immobiliare.it/vendita-case/torino/?prezzoMassimo=340000&superficieMinima=80&localiMinimo=3&stato=6&balconeOterrazzo=1&tipoProprieta=1&noAste=1&classeEnergetica=8&idMZona[0]=194&idMZona[1]=181&idMZona[2]=173&idMZona[3]=174&idMZona[4]=172&idMZona[5]=177&idMZona[6]=178&idMZona[7]=183&idQuartiere[0]=682&idQuartiere[1]=667&idQuartiere[2]=663"
    )
}


# ==========================================
# BATCH-ENABLED TURSO DB CLASS
# ==========================================
class TursoDB:

  def __init__(self, db_url, token):
    self.endpoint = db_url.replace("libsql://", "https://") + "/v2/pipeline"
    self.headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

  def _build_stmt(self, sql, params=None):
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
    return {"sql": sql, "args": args}

  def execute(self, sql, params=None):
    payload = {
        "requests": [
            {"type": "execute", "stmt": self._build_stmt(sql, params)},
            {"type": "close"},
        ]
    }
    res = requests.post(
        self.endpoint, json=payload, headers=self.headers, timeout=15
    )
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

  def execute_batch(self, statements: list[tuple[str, list]], chunk_size=100):
    """Executes hundreds of SQL statements in bundled HTTP payload chunks."""
    if not statements:
      return

    for i in range(0, len(statements), chunk_size):
      chunk = statements[i : i + chunk_size]
      requests_payload = [
          {"type": "execute", "stmt": self._build_stmt(sql, params)}
          for sql, params in chunk
      ]
      requests_payload.append({"type": "close"})

      res = requests.post(
          self.endpoint,
          json={"requests": requests_payload},
          headers=self.headers,
          timeout=30,
      )
      res.raise_for_status()


def get_db():
  if TURSO_URL and TURSO_TOKEN and "yourusername" not in TURSO_URL:
    return TursoDB(TURSO_URL, TURSO_TOKEN)
  return sqlite3.connect("case.db")


def init_db():
  db = get_db()
  sql_create = """
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
            descrizione TEXT DEFAULT '',
            stato TEXT DEFAULT 'in review',
            note TEXT DEFAULT '',
            voto_danilo INTEGER DEFAULT 0,
            voto_ragazza INTEGER DEFAULT 0,
            data_scoperta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
  sql_index = """
        CREATE INDEX IF NOT EXISTS idx_annunci_stato_data 
        ON annunci (stato, data_scoperta DESC);
    """
  sql_alter = "ALTER TABLE annunci ADD COLUMN descrizione TEXT DEFAULT ''"

  if isinstance(db, TursoDB):
    db.execute(sql_create)
    db.execute(sql_index)
    try:
      db.execute(sql_alter)
    except Exception:
      pass
  else:
    conn = db
    cursor = conn.cursor()
    cursor.execute(sql_create)
    cursor.execute(sql_index)
    try:
      cursor.execute(sql_alter)
    except Exception:
      pass
    conn.commit()
    conn.close()


def load_db_cache(db) -> dict:
  print("⚡ Loading database cache into RAM...")
  sql = "SELECT id, descrizione FROM annunci"
  cache = {}
  try:
    if isinstance(db, TursoDB):
      df = db.execute(sql)
      if not df.empty and "id" in df.columns:
        for _, row in df.iterrows():
          cache[str(row["id"])] = (
              str(row["descrizione"]) if row["descrizione"] is not None else ""
          )
    else:
      conn = db if not isinstance(db, str) else sqlite3.connect("case.db")
      cursor = conn.cursor()
      cursor.execute(sql)
      for row in cursor.fetchall():
        cache[str(row[0])] = str(row[1]) if row[1] is not None else ""
  except Exception as e:
    print(f"⚠️ Cache notice: {e}")
  print(f"✅ RAM Cache ready: {len(cache)} existing records loaded.")
  return cache


# ==========================================
# PARSERS & HELPERS
# ==========================================
def parse_digits(val):
  if not val or val == "N/D":
    return 0
  digits = re.sub(r"[^\d]", "", str(val))
  return int(digits) if digits else 0


def convert_to_hd_url(url: str) -> str:
  if not url:
    return url
  clean_url = url.split("?")[0]
  clean_url = re.sub(
      r"/(\d+x\d+|xxs|xs|s|m|l|xl)(-c)?\.(jpg|jpeg|webp|png)$",
      r"/l-c.\3",
      clean_url,
      flags=re.IGNORECASE,
  )
  clean_url = re.sub(r"/thumbnails?/", "/images/", clean_url)
  clean_url = re.sub(r"-thumb\.", "-large.", clean_url)
  clean_url = re.sub(r"/small/", "/large/", clean_url)
  return clean_url


def format_full_url(url: str, listing_id: str = "") -> str:
  if url:
    if not url.startswith("http"):
      return (
          f"https://www.immobiliare.it{url if url.startswith('/') else '/' + url}"
      )
    return url
  if listing_id:
    return f"https://www.immobiliare.it/annunci/{listing_id}/"
  return ""


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


def extract_listing_url(real_estate, listing_id):
  url = real_estate.get("url") or real_estate.get("seoUrl")
  if url:
    return format_full_url(url, listing_id)

  properties = real_estate.get("properties", [{}])
  if properties and isinstance(properties, list):
    prop = properties[0]
    urls_dict = prop.get("urls", {})
    if isinstance(urls_dict, dict):
      url = (
          urls_dict.get("express")
          or urls_dict.get("ita")
          or urls_dict.get("default")
      )
      if url:
        return format_full_url(url, listing_id)

  return format_full_url("", listing_id)


def gestisci_cookie_banner(page):
  try:
    btn = page.ele('text:Accetta', timeout=2) or page.ele(
        '#didomi-notice-agree-button', timeout=1
    )
    if btn:
      btn.click()
      time.sleep(0.5)
  except Exception:
    pass


def fetch_html_via_browser(page, url: str) -> str:
  js_script = r"""
        return (async () => {
            try {
                const res = await fetch(arguments[0], {
                    headers: { 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' }
                });
                return await res.text();
            } catch(e) {
                return '';
            }
        })();
    """
  return page.run_js(js_script, url) or ""


def batch_fetch_descriptions_js(page, listing_ids: list) -> dict:
  if not listing_ids:
    return {}

  js_script = r"""
        return (async () => {
            const ids = JSON.parse(arguments[0]);
            const promises = ids.map(async (id) => {
                try {
                    const res = await fetch(`https://www.immobiliare.it/annunci/${id}/`);
                    if (!res.ok) return { id, desc: '' };
                    const html = await res.text();
                    
                    const match = html.match(/<script id="__NEXT_DATA__" type="application\/json">(.*?)<\/script>/s);
                    if (match && match[1]) {
                        const data = JSON.parse(match[1]);
                        const prop = data?.props?.pageProps?.detailData?.realEstate?.properties?.[0];
                        if (prop && prop.description) {
                            return { id, desc: prop.description.trim() };
                        }
                    }
                    
                    const descMatch = html.match(/class="in-readAll[^"]*">(.*?)<\/div>/s) || html.match(/class="in-realEstateDescription__text[^"]*">(.*?)<\/div>/s);
                    if (descMatch && descMatch[1]) {
                        return { id, desc: descMatch[1].replace(/<[^>]+>/g, '').trim() };
                    }
                } catch (e) {}
                return { id, desc: '' };
            });
            return await Promise.all(promises);
        })();
    """
  start_time = time.time()
  print(
      f"  🚀 Batch fetching {len(listing_ids)} full descriptions in browser"
      " engine..."
  )

  ids_json = json.dumps(listing_ids)
  raw_results = page.run_js(js_script, ids_json)
  results = {}
  if raw_results and isinstance(raw_results, list):
    for item in raw_results:
      if isinstance(item, dict) and item.get("id") and item.get("desc"):
        results[str(item["id"])] = item["desc"]

  elapsed = time.time() - start_time
  print(
      f"  ⚡ Extracted {len(results)}/{len(listing_ids)} full descriptions in"
      f" {elapsed:.2f}s!"
  )
  return results


# ==========================================
# MAIN SCRAPER EXECUTION
# ==========================================
def esegui_scraping():
  init_db()
  db = get_db()
  db_cache = load_db_cache(db)

  print("🚀 Launching DrissionPage Stealth Browser Context...")
  co = ChromiumOptions()
  co.set_argument("--blink-settings=imagesEnabled=false")
  co.mute(True)

  page = ChromiumPage(co)
  all_scraped_listings = {}

  try:
    print("🔗 Solving Anti-Bot challenge natively...")
    page.get("https://www.immobiliare.it/")
    gestisci_cookie_banner(page)
    print("✅ Stealth Session Ready!")

    # STEP 1: Crawl all pages into memory
    for profilo, url_ricerca in RICERCHE.items():
      print("\n==================================================")
      print(f"Scraping profile: {profilo}")
      print("==================================================")

      for pag in range(1, MAX_PAGINE + 1):
        url_corrente = (
            f"{url_ricerca}&pag={pag}"
            if "?" in url_ricerca
            else f"{url_ricerca}?pag={pag}"
        )

        print(f"--- [Page {pag}/{MAX_PAGINE}] Crawling... ---")
        start_page_time = time.time()

        html_content = fetch_html_via_browser(page, url_corrente)
        annunci_pagina = {}

        if html_content:
          soup = BeautifulSoup(html_content, "html.parser")
          next_data = soup.find("script", id="__NEXT_DATA__")
          if next_data and next_data.string:
            try:
              data_json = json.loads(next_data.string)
              risultati = cerca_annunci_nel_json(data_json)
              for r in risultati:
                re_obj = r.get("realEstate", r)
                if isinstance(re_obj, dict) and "id" in re_obj:
                  annunci_pagina[str(re_obj["id"])] = (re_obj, profilo)
            except Exception:
              pass

        if not annunci_pagina:
          print(f"⚠️ No listings found on page {pag}. Stopping pagination.")
          break

        print(
            f"✅ Found {len(annunci_pagina)} listings in"
            f" {time.time() - start_page_time:.2f}s."
        )
        all_scraped_listings.update(annunci_pagina)

    print(
        f"\n🎯 Total unique listings gathered in RAM:"
        f" {len(all_scraped_listings)}"
    )

    # STEP 2: Batch fetch missing descriptions across all gathered items
    needs_detail_ids = []
    for id_annuncio in all_scraped_listings.keys():
      cached_desc = db_cache.get(id_annuncio, "")
      if (
          not cached_desc
          or len(cached_desc) < 150
          or cached_desc.endswith("...")
      ):
        needs_detail_ids.append(id_annuncio)

    fetched_descriptions = batch_fetch_descriptions_js(page, needs_detail_ids)

    # STEP 3: Unified UPSERT logic for DB protection
    sql_upsert = """
        INSERT INTO annunci (
            id, profilo, titolo, prezzo, prezzo_num, superficie, superficie_num, locali, url, foto, descrizione, stato
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in review')
        ON CONFLICT(id) DO UPDATE SET
            profilo        = excluded.profilo,
            titolo         = excluded.titolo,
            prezzo         = excluded.prezzo,
            prezzo_num     = excluded.prezzo_num,
            superficie     = excluded.superficie,
            superficie_num = excluded.superficie_num,
            locali         = excluded.locali,
            url            = excluded.url,
            foto           = excluded.foto,
            descrizione    = CASE 
                                WHEN length(excluded.descrizione) > length(annunci.descrizione) THEN excluded.descrizione 
                                ELSE annunci.descrizione 
                             END;
    """

    sql_batch = []
    new_count = 0
    updated_count = 0

    for id_annuncio, (real_estate, profilo) in all_scraped_listings.items():
      titolo = real_estate.get("title", "N/D")

      price_data = real_estate.get("price", {})
      prezzo = (
          f"{price_data.get('value', 'N/D')} €"
          if isinstance(price_data, dict)
          else str(price_data)
      )
      prezzo_num = parse_digits(prezzo)

      properties = real_estate.get("properties", [{}])
      prop = (
          properties[0]
          if isinstance(properties, list) and len(properties) > 0
          else {}
      )

      superficie_raw = str(prop.get("surface", "N/D")).replace("m²", "").strip()
      superficie_num = parse_digits(superficie_raw)
      superficie = f"{superficie_num}" if superficie_num > 0 else "N/D"

      locali = str(prop.get("rooms", "N/D"))
      link = extract_listing_url(real_estate, id_annuncio)

      foto_urls = []
      multimedia = prop.get("multimedia", {})
      photos = (
          multimedia.get("photos", []) if isinstance(multimedia, dict) else []
      )

      for p in photos:
        urls_dict = p.get("urls", {})
        best_url = (
            urls_dict.get("extra_large")
            or urls_dict.get("large")
            or urls_dict.get("medium")
            or urls_dict.get("small")
        )
        if best_url:
          foto_urls.append(convert_to_hd_url(best_url))

      foto_json = json.dumps(foto_urls)

      descrizione = (
          fetched_descriptions.get(id_annuncio)
          or (
              prop.get("caption")
              or prop.get("description")
              or real_estate.get("caption")
              or real_estate.get("description")
              or ""
          ).strip()
          or db_cache.get(id_annuncio, "")
      )

      cached_desc = db_cache.get(id_annuncio)

      if cached_desc is None:
        new_count += 1
        print(f"   ✨ [NEW] {titolo} - {prezzo} ({len(foto_urls)} photos)")
      else:
        updated_count += 1

      params = (
          id_annuncio,
          profilo,
          titolo,
          prezzo,
          prezzo_num,
          superficie,
          superficie_num,
          locali,
          link,
          foto_json,
          descrizione,
      )
      sql_batch.append((sql_upsert, params))

    # STEP 4: Flush all DB operations in a single fast batch pipeline call
    print(
        f"\n💾 Flushing {len(sql_batch)} database operations ({new_count} new,"
        f" {updated_count} checked/updated)..."
    )
    start_db_time = time.time()

    if isinstance(db, TursoDB):
      db.execute_batch(sql_batch)
    else:
      conn = db if not isinstance(db, str) else sqlite3.connect("case.db")
      cursor = conn.cursor()
      for sql, params in sql_batch:
        cursor.execute(sql, params)
      conn.commit()
      conn.close()

    print(
        f"⚡ Database sync complete in {time.time() - start_db_time:.2f}s!"
    )
    return new_count

  except Exception as e:
    print(f"❌ Error during scraping run: {e}")
  finally:
    page.quit()


if __name__ == "__main__":
  esegui_scraping()