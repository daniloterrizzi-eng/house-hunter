import json
import os
import re
import sqlite3
import pandas as pd
import requests
import streamlit as st

TURSO_URL = st.secrets.get("TURSO_URL", os.getenv("TURSO_URL", "libsql://house-hunter-daniloterrizzi-eng.aws-eu-west-1.turso.io"))
TURSO_TOKEN = st.secrets.get("TURSO_TOKEN", os.getenv("TURSO_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODY4ODE2NjcsImlkIjoiMDFhMDBhNjAtODkwMS03MDI0LWEyMWYtNTY1OTA2YTYwNThiIiwia2lkIjoiQVl3eDdVUktWLV90SjEyUnFnNHYzYW1RVGszWnc0Z042UnR1ZFdwNWgtMCIsInJpZCI6ImU5NDFlZjAyLWU0MWYtNDJiOS05MTRhLWY5NDM5NGNiMzM5YSJ9.rROVpRRKgMekGE6dSiKoDiPUK9lqU0eLAXTVDO56pXc2fo_Cfwy6rGaaGPIv11uAGSv_dOZ3c_1wNdBtp8eiBQ"))

st.set_page_config(page_title="House Hunter Turin", page_icon="🏠", layout="wide")

STATI_DISPONIBILI = ["in review", "saved", "maybe", "discarded"]

if "sort_col" not in st.session_state:
    st.session_state.sort_col = "data_scoperta"
if "sort_dir" not in st.session_state:
    st.session_state.sort_dir = "DESC"

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
        return TursoDB(TURSO_URL, TURSO_TOKEN)
    return sqlite3.connect("case.db", check_same_thread=False)

def read_query_df(query, params=None):
    db = get_db()
    if isinstance(db, TursoDB):
        return db.execute(query, params)
    
    conn = db
    cursor = conn.cursor()
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    return pd.DataFrame(rows, columns=columns)

def execute_query(query, params=None):
    db = get_db()
    if isinstance(db, TursoDB):
        db.execute(query, params)
    else:
        conn = db
        cursor = conn.cursor()
        cursor.execute(query, params or ())
        conn.commit()
        conn.close()

def init_db():
    sql = '''
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
    execute_query(sql)

init_db()

def format_full_url(url: str, listing_id: str = "") -> str:
    if url and str(url).strip():
        clean_url = str(url).strip()
        if not clean_url.startswith("http"):
            return f"https://www.immobiliare.it{clean_url if clean_url.startswith('/') else '/' + clean_url}"
        return clean_url
    if listing_id:
        return f"https://www.immobiliare.it/annunci/{listing_id}/"
    return "#"

def convert_to_hd_url(url: str) -> str:
    """Converts low-res thumbnail image URLs (e.g., xxs-c, small, thumb) to 1024x768 HD images."""
    if not url: return url
    clean_url = url.split('?')[0]
    
    # Replace low-res size suffixes like /xxs-c.jpg, /xxs.jpg, /s-c.jpg, /m-c.jpg
    clean_url = re.sub(r'/(xxs|xs|s|m)(-c)?\.(jpg|jpeg|webp|png)$', r'/1024x768.\3', clean_url, flags=re.IGNORECASE)
    
    # Standard replacement rules
    clean_url = re.sub(r'/thumbnails?/', '/images/', clean_url)
    clean_url = re.sub(r'c-\d+x\d+', 'c-1024x768', clean_url)
    clean_url = re.sub(r'_\d+x\d+\.', '_1024x768.', clean_url)
    clean_url = re.sub(r'/\d+x\d+/', '/1024x768/', clean_url)
    clean_url = re.sub(r'/shape/\d+x\d+/', '/shape/1024x768/', clean_url)
    clean_url = re.sub(r'-thumb\.', '-large.', clean_url)
    clean_url = re.sub(r'/small/', '/large/', clean_url)
    
    return clean_url

def batch_update_listing(id_annuncio, voto_danilo, voto_aurelia, stato, note):
    sql = "UPDATE annunci SET voto_danilo = ?, voto_ragazza = ?, stato = ?, note = ? WHERE id = ?"
    execute_query(sql, [voto_danilo, voto_aurelia, stato, note, id_annuncio])

def toggle_sort(column_db_name):
    if st.session_state.sort_col == column_db_name:
        st.session_state.sort_dir = "ASC" if st.session_state.sort_dir == "DESC" else "DESC"
    else:
        st.session_state.sort_col = column_db_name
        st.session_state.sort_dir = "DESC"

@st.dialog("🏠 Dettaglio Casa", width="large")
def mostra_modal_dettaglio(item_id):
    df_single = read_query_df("SELECT * FROM annunci WHERE id = ?", params=[item_id])

    if df_single.empty:
        st.error("Immobile non trovato!")
        return

    item = df_single.iloc[0]
    item_url = format_full_url(item["url"], item["id"])

    st.subheader(item["titolo"])

    foto_raw = item.get("foto", "[]")
    foto_list = []
    try:
        foto_list = json.loads(foto_raw) if foto_raw else []
    except Exception:
        if isinstance(foto_raw, str) and foto_raw.startswith("http"):
            foto_list = [foto_raw]

    # Convert all photos dynamically to HD
    foto_list = [convert_to_hd_url(u) for u in foto_list if u]

    if foto_list:
        img_key = f"idx_img_{item['id']}"
        if img_key not in st.session_state:
            st.session_state[img_key] = 0

        def prev_pic():
            st.session_state[img_key] = (st.session_state[img_key] - 1) % len(foto_list)

        def next_pic():
            st.session_state[img_key] = (st.session_state[img_key] + 1) % len(foto_list)

        c_prev, c_info, c_next = st.columns([1.5, 3, 1.5])
        with c_prev:
            st.button("◀ Precedente (←)", key=f"btn_p_{item['id']}", on_click=prev_pic, use_container_width=True)
        with c_info:
            st.markdown(f"<p style='text-align: center; margin-top: 8px;'><b>Foto {st.session_state[img_key] + 1} di {len(foto_list)}</b></p>", unsafe_allow_html=True)
        with c_next:
            st.button("Successiva ▶ (→)", key=f"btn_n_{item['id']}", on_click=next_pic, use_container_width=True)

        st.image(foto_list[st.session_state[img_key]], use_container_width=True)

        st.components.v1.html(
            f"""
            <script>
            const topWin = window.top;
            const topDoc = topWin.document;

            function onKey(e) {{
                if (e.key === 'ArrowLeft') {{
                    const btnPrev = topDoc.querySelector('button[key="btn_p_{item['id']}"]');
                    if (btnPrev) {{
                        e.preventDefault();
                        btnPrev.click();
                    }}
                }} else if (e.key === 'ArrowRight') {{
                    const btnNext = topDoc.querySelector('button[key="btn_next_{item['id']}"]');
                    if (btnNext) {{
                        e.preventDefault();
                        btnNext.click();
                    }}
                }}
            }}

            topWin.removeEventListener('keydown', topWin._stKeyHandler);
            topWin._stKeyHandler = onKey;
            topWin.addEventListener('keydown', topWin._stKeyHandler);
            topWin.focus();
            </script>
            """,
            height=0,
        )
    else:
        st.info("📷 Nessuna foto disponibile per questo annuncio.")

    st.markdown(f"🔗 **[Apri scheda originale su Immobiliare.it]({item_url})**")
    st.divider()

    st.markdown("### ✏️ Voti, Stato e Note")
    col_d, col_a, col_s = st.columns(3)

    with col_d:
        curr_vd = int(item["voto_danilo"]) if item["voto_danilo"] else 0
        voto_d = st.selectbox("Voto Danilo (1-10)", list(range(0, 11)), index=min(max(curr_vd, 0), 10), key=f"dialog_vd_{item['id']}")

    with col_a:
        curr_va = int(item["voto_ragazza"]) if item["voto_ragazza"] else 0
        voto_a = st.selectbox("Voto Aurelia (1-10)", list(range(0, 11)), index=min(max(curr_va, 0), 10), key=f"dialog_va_{item['id']}")

    with col_s:
        curr_status = item["stato"] if item["stato"] in STATI_DISPONIBILI else "in review"
        new_status = st.selectbox("Stato", STATI_DISPONIBILI, index=STATI_DISPONIBILI.index(curr_status), key=f"dialog_st_{item['id']}")

    note_input = st.text_area("Note e Commenti:", value=item["note"] or "", height=100, key=f"dialog_note_{item['id']}")

    if st.button("💾 Salva Modifiche", type="primary", use_container_width=True, key=f"save_btn_{item['id']}"):
        batch_update_listing(item["id"], voto_d, voto_a, new_status, note_input)
        st.toast("✅ Modifiche salvate!")

# --- SIDEBAR FILTERS ---
st.sidebar.title("🎛️ Filtri")

show_status = st.sidebar.multiselect(
    "Filtra per Stato",
    options=STATI_DISPONIBILI,
    default=["in review", "saved", "maybe"]
)

search_term = st.sidebar.text_input("🔍 Cerca nel Titolo / Via", "")

if show_status:
    query_stati = show_status.copy()
    if "in review" in query_stati: query_stati.append("nuovo")

    placeholders = ','.join(['?'] * len(query_stati))
    
    sort_column = st.session_state.sort_col
    sort_direction = st.session_state.sort_dir
    
    query = f"""
        SELECT * FROM annunci 
        WHERE stato IN ({placeholders})
        ORDER BY {sort_column} {sort_direction}
    """
    df = read_query_df(query, params=query_stati)
else:
    df = pd.DataFrame()

if not df.empty:
    df["stato"] = df["stato"].replace({"nuovo": "in review", "salvato": "saved", "scartato": "discarded"})
    if search_term:
        df = df[df["titolo"].str.contains(search_term, case=False, na=False)]

# --- SCHERMATA PRINCIPALE ---
st.title(f"🏠 House Hunter Turin ({len(df)} immobili)")

if df.empty:
    st.info("💡 Nessun immobile trovato. Modifica i filtri o esegui lo scraper!")
else:
    st.caption("👇 Clicca sulle intestazioni per ordinare. Clicca sul **Titolo** per aprire la scheda originale su Immobiliare.it.")

    h_cols = st.columns([3.5, 1.5, 1.2, 0.8, 1.2, 1, 1, 1])
    arrow = "🔽" if st.session_state.sort_dir == "DESC" else "🔼"
    
    if h_cols[0].button(f"Titolo {arrow if st.session_state.sort_col == 'titolo' else ''}", use_container_width=True):
        toggle_sort("titolo")
        st.rerun()

    if h_cols[1].button(f"Prezzo {arrow if st.session_state.sort_col == 'prezzo_num' else ''}", use_container_width=True):
        toggle_sort("prezzo_num")
        st.rerun()

    if h_cols[2].button(f"Superficie {arrow if st.session_state.sort_col == 'superficie_num' else ''}", use_container_width=True):
        toggle_sort("superficie_num")
        st.rerun()

    h_cols[3].markdown("**Locali**")

    if h_cols[4].button(f"Stato {arrow if st.session_state.sort_col == 'stato' else ''}", use_container_width=True):
        toggle_sort("stato")
        st.rerun()

    if h_cols[5].button(f"Danilo {arrow if st.session_state.sort_col == 'voto_danilo' else ''}", use_container_width=True):
        toggle_sort("voto_danilo")
        st.rerun()

    if h_cols[6].button(f"Aurelia {arrow if st.session_state.sort_col == 'voto_ragazza' else ''}", use_container_width=True):
        toggle_sort("voto_ragazza")
        st.rerun()

    h_cols[7].markdown("**Azione**")
    st.divider()

    items_per_page = 20
    total_pages = max(1, (len(df) + items_per_page - 1) // items_per_page)
    
    page_num = st.number_input("Pagina", min_value=1, max_value=total_pages, value=1) if total_pages > 1 else 1

    start_idx = (page_num - 1) * items_per_page
    end_idx = start_idx + items_per_page
    df_page = df.iloc[start_idx:end_idx]

    for idx, item in df_page.iterrows():
        with st.container(border=True):
            cols = st.columns([3.5, 1.5, 1.2, 0.8, 1.2, 1, 1, 1])
            
            # Clickable Title Link with Fallback to ID
            item_url = format_full_url(item["url"], item["id"])
            cols[0].markdown(f"🔗 [{item['titolo']}]({item_url})")

            cols[1].write(item["prezzo"])
            
            sup_clean = str(item['superficie']).replace('m²', '').strip()
            cols[2].write(f"{sup_clean} m²")
            
            cols[3].write(item["locali"])
            
            status_colors = {"in review": "🔵", "saved": "🟢", "maybe": "🟡", "discarded": "🔴"}
            badge = f"{status_colors.get(item['stato'], '⚪')} {item['stato']}"
            cols[4].write(badge)

            cols[5].write(f"⭐ {item['voto_danilo']}" if item['voto_danilo'] else "-")
            cols[6].write(f"⭐ {item['voto_ragazza']}" if item['voto_ragazza'] else "-")
            
            if cols[7].button("👁️ Apri", key=f"btn_open_{item['id']}"):
                mostra_modal_dettaglio(item["id"])