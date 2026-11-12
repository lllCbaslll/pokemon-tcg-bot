import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# ---------- Configuración ----------
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
TZ = ZoneInfo("America/Lima")  # Hora Perú (UTC-5)

# 4 reportes al día por defecto: 08:00, 12:00, 16:00 y 20:00 (Hora Perú)
REPORT_HOURS = tuple(
    int(h.strip())
    for h in os.getenv("REPORT_HOURS", "8,12,16,20").split(",")
    if h.strip().isdigit()
)

# SOLO_ALERTAS | FORZAR_REPORTE | REPORTE | COMPLETO
MODO = os.getenv("MODO_EJECUCION", "COMPLETO").strip().upper()

# 0 = ejecución única (ideal para GitHub Actions / cron cada 5 min)
# 300 = bucle infinito cada 5 minutos (ideal para VPS / Docker / PC local)
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "0"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

TARGET_KEYWORDS = [
    ["booster"], ["bundle"], ["ultra"], ["premium"], ["binder"], ["tin"],
    ["suelta"], ["aniversario"], ["pack"], ["caja"], ["sobre"], ["álbum"],
    ["album"], ["poster"], ["entrenador"], ["élite"], ["elite"], ["ex"],
    ["pokeball"], ["colección"], ["coleccion"], ["tcg"], ["cartas", "pokemon"],
]
EXCLUDE_RE = re.compile(
    r"espa[ñn]ol|\bspa\b|spanish|peluche|\bron\b|sorpresa|brillante", re.I
)
THIRTY_RE = re.compile(r"(?<!\d)30(?!\d)")
ENGLISH_RE = re.compile(r"ingl[eé]s|english", re.I)
POKEMON_RE = re.compile(r"pok[eé]mon|\btcg\b", re.I)

# ---------- HTTP con reintentos ----------
def make_session():
    s = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

SESSION = make_session()
SCRAPER = cloudscraper.create_scraper() if cloudscraper else SESSION

# ---------- Estado persistente ----------
def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("stock", {})
        data.setdefault("reports", [])
        return data
    except Exception:
        return {"stock": {}, "reports": []}

def save_state(state):
    state["reports"] = state.get("reports", [])[-40:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = STATE_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(STATE_FILE)

# ---------- Filtros y registro thread-safe ----------
def _kw_match(kw, texto):
    pat = rf"\b{re.escape(kw)}\b" if len(kw) <= 3 else rf"\b{re.escape(kw)}"
    return re.search(pat, texto) is not None

def es_producto_valido(texto, url=""):
    if not texto:
        return False
    t = f"{texto} {url}".lower().replace("-", " ").replace("/", " ").replace("_", " ")
    if EXCLUDE_RE.search(t):
        return False
    if not THIRTY_RE.search(t) or not POKEMON_RE.search(t):
        return False
    if ENGLISH_RE.search(t):
        return True
    return any(all(_kw_match(k, t) for k in conj) for conj in TARGET_KEYWORDS)

hallazgos = []
hallazgos_lock = Lock()

def registrar_producto(tienda, titulo, url, stock, precio="N/A", cart_url=None, sku="N/A"):
    url_limpia = url.split("?")[0].rstrip("/")
    with hallazgos_lock:
        if any(h["url_key"] == url_limpia for h in hallazgos):
            return
        hallazgos.append({
            "tienda": tienda,
            "titulo": titulo,
            "url": url,
            "url_key": url_limpia,
            "stock": bool(stock),
            "precio": precio,
            "cart_url": cart_url or url,
            "sku": sku,
        })

# ---------- Discord ----------
def post_discord(payload):
    if not WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK no configurado; no se envía nada.")
        return False
    try:
        res = SESSION.post(WEBHOOK_URL, json=payload, timeout=10)
        if res.status_code == 429:
            retry_after = float(res.json().get("retry_after", 2))
            time.sleep(min(retry_after, 5))
            res = SESSION.post(WEBHOOK_URL, json=payload, timeout=10)

        print(f"Discord -> HTTP {res.status_code}")
        if res.status_code == 404:
            print("❌ Webhook inexistente: revisa DISCORD_WEBHOOK.")
        elif res.status_code >= 300:
            print(f"❌ Discord respondió: {res.text[:200]}")
        return res.status_code < 300
    except Exception as e:
        print(f"Error enviando a Discord: {e}")
        return False

def enviar_alerta_stock_inmediato(item):
    return post_discord({
        "content": f"🚨 **¡STOCK EN INGLÉS DETECTADO EN {item['tienda'].upper()}!** @everyone",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [{
            "title": f"🟢 {item['titulo']}",
            "url": item["url"],
            "color": 3066993,
            "fields": [
                {"name": "Estado", "value": "🟢 CON STOCK DISPONIBLE", "inline": True},
                {"name": "Tienda", "value": item["tienda"], "inline": True},
                {"name": "Precio", "value": str(item["precio"]), "inline": True},
                {"name": "SKU / Variant ID", "value": str(item["sku"]), "inline": True},
                {"name": "🛒 Comprar", "value": f"[Añadir al Carrito]({item['cart_url']})", "inline": False},
            ],
            "footer": {"text": f"Detectado: {datetime.now(TZ).strftime('%H:%M:%S')} PET"},
        }],
    })

def enviar_reporte_programado(hora_label):
    if not hallazgos:
        embeds = [{
            "title": "Consolidado de Tiendas Rastreadas",
            "description": "No se encontraron publicaciones activas coincidentes en inglés.",
            "color": 3447003,
            "footer": {"text": f"Generado el {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} (hora Perú)"},
        }]
    else:
        # Ordena primero los que tienen stock
        ordenados = sorted(hallazgos, key=lambda x: (not x["stock"], x["tienda"], x["titulo"]))
        bloques = []
        actual = ""
        for it in ordenados:
            estado = "🟢 CON STOCK" if it["stock"] else "🔴 SIN STOCK"
            cart = f" | [🛒 Carrito]({it['cart_url']})" if it["stock"] else ""
            linea = f"• **[{it['tienda']}]** [{it['titulo']}]({it['url']}) - **{estado}** ({it['precio']}){cart}\n"
            if len(actual) + len(linea) > 3800:
                bloques.append(actual.strip())
                actual = linea
            else:
                actual += linea
        if actual.strip():
            bloques.append(actual.strip())

        embeds = []
        for idx, desc in enumerate(bloques[:10]):
            embed = {
                "title": "Consolidado de Tiendas Rastreadas" if idx == 0 else f"Consolidado (Parte {idx + 1})",
                "description": desc,
                "color": 3447003,
            }
            if idx == len(bloques[:10]) - 1:
                embed["footer"] = {
                    "text": f"Total: {len(ordenados)} productos | {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} PET"
                }
            embeds.append(embed)

    return post_discord({
        "content": f"📊 **REPORTE POKÉMON TCG 30TH (INGLÉS) - {hora_label} PET**",
        "embeds": embeds,
    })

# ---------- Scrapers ----------
def buscar_ilahui():
    urls = ["https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-binder-collection-en-ingles"]
    for url in urls:
        try:
            r = SESSION.get(f"{url}.js", timeout=10)
            if r.status_code != 200:
                continue
            p = r.json()
            titulo = p.get("title", "")
            if not es_producto_valido(titulo, url):
                continue
            variants = p.get("variants", [])
            vid = variants[0].get("id") if variants else None
            stock = any(v.get("available") for v in variants) if variants else p.get("available", False)
            precio = f"S/ {p.get('price', 0) / 100:.2f}"
            cart = f"https://ilahuiperu.com/cart/add?id={vid}&quantity=1" if vid else url
            print(f"[Ilahui] {titulo} | Stock: {stock}")
            registrar_producto("Ilahui", titulo, url, stock, precio, cart, sku=vid)
        except Exception as e:
            print(f"[Ilahui] Error: {e}")

def buscar_vtex(nombre, domain):
    for term in ["30 aniversario", "tcg 30"]:
        q = urllib.parse.quote(term)
        api = f"{domain}/api/catalog_system/pub/products/search?ft={q}&_from=0&_to=30"
        try:
            r = SESSION.get(api, timeout=10)
            if r.status_code not in (200, 206):
                continue
            productos = r.json()
            if not isinstance(productos, list):
                continue
            for prod in productos:
                nom = prod.get("productName", "")
                link = prod.get("link", domain)
                if not es_producto_valido(nom, link):
                    continue
                stock, precio, sku_id = False, "N/A", None
                for item in prod.get("items", []):
                    for seller in item.get("sellers", []):
                        comm = seller.get("commertialOffer", {})
                        if comm.get("AvailableQuantity", 0) > 0 and comm.get("IsAvailable", False):
                            stock = True
                            sku_id = item.get("itemId")
                            if "Price" in comm:
                                precio = f"S/ {comm['Price']}"
                            break
                    if stock:
                        break
                cart = f"{domain}/checkout/cart/add?sku={sku_id}&qty=1&seller=1" if sku_id else link
                print(f"[{nombre}] {nom} | Stock: {stock}")
                registrar_producto(nombre, nom, link, stock, precio, cart, sku=sku_id)
        except Exception as e:
            print(f"[{nombre}] Error con '{term}': {e}")

def buscar_tailoy():
    url = "https://www.tailoy.com.pe/catalogsearch/result/?q=tcg+30+aniversario"
    try:
        r = SCRAPER.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            print(f"[Tai Loy] HTTP {r.status_code}")
            return
        soup = BeautifulSoup(r.text, "html.parser")
        for prod in soup.select(".product-item, .product-item-info"):
            el = prod.select_one(".product-item-link")
            if not el:
                continue
            nombre, link = el.get_text(strip=True), el.get("href", "")
            pr = prod.select_one(".price")
            precio = pr.get_text(strip=True) if pr else "N/A"
            if es_producto_valido(nombre, link):
                txt = prod.text.lower()
                stock = "sin stock" not in txt and "agotado" not in txt
                print(f"[Tai Loy] {nombre} | Stock: {stock}")
                registrar_producto("Tai Loy", nombre, link, stock, precio)
    except Exception as e:
        print(f"[Tai Loy] Error: {e}")

def buscar_phantom():
    url = "https://phantom.pe/?s=tcg+30+aniversario&post_type=product"
    try:
        r = SCRAPER.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            print(f"[Phantom] HTTP {r.status_code}")
            return
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("li.product, div.product, article"):
            t = item.select_one(".woocommerce-loop-product__title, .product-title, h2, h3")
            a = item.select_one("a")
            if not (t and a):
                continue
            titulo, link = t.get_text(strip=True), a.get("href", "")
            pr = item.select_one(".price, .amount")
            precio = pr.get_text(strip=True) if pr else "N/A"
            if es_producto_valido(titulo, link):
                stock = "agotado" not in item.text.lower()
                print(f"[Phantom] {titulo} | Stock: {stock}")
                registrar_producto("Phantom", titulo, link, stock, precio)
    except Exception as e:
        print(f"[Phantom] Error: {e}")

# ---------- Ejecución de un ciclo ----------
def ejecutar_ciclo():
    hallazgos.clear()
    ahora = datetime.now(TZ)
    print(f"\n=== Rastreo Pokémon (Inglés): {ahora:%Y-%m-%d %H:%M:%S} PET ===")

    state = load_state()

    tareas = [
        buscar_ilahui,
        lambda: buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe"),
        lambda: buscar_vtex("Oechsle", "https://www.oechsle.pe"),
        buscar_tailoy,
        buscar_phantom,
    ]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futuros = [pool.submit(fn) for fn in tareas]
        for f in futuros:
            f.result()

    es_reporte = MODO in ("FORZAR_REPORTE", "REPORTE")

    # Alertas inmediatas (solo transición False -> True)
    if not es_reporte:
        for item in hallazgos:
            key = item["url_key"]
            antes = state["stock"].get(key, False)
            if item["stock"] and not antes:
                print(f"🚨 ¡Stock nuevo en {item['tienda']}: {item['titulo']}!")
                if enviar_alerta_stock_inmediato(item):
                    state["stock"][key] = True
            elif not item["stock"]:
                # Solo baja a False cuando confirmamos que la página cargó y dice sin stock
                state["stock"][key] = False

    # Reporte consolidado (4 veces al día: 08:00, 12:00, 16:00, 20:00 PET)
    if MODO == "FORZAR_REPORTE":
        enviar_reporte_programado(f"{ahora:%H:%M}")
    elif MODO != "SOLO_ALERTAS":
        slot = f"{ahora:%Y-%m-%d}-{ahora.hour:02d}"
        if ahora.hour in REPORT_HOURS and slot not in state["reports"]:
            if enviar_reporte_programado(f"{ahora.hour:02d}:00"):
                state["reports"].append(slot)

    save_state(state)
    print(f"Escaneo finalizado ({len(hallazgos)} productos evaluados).")

def main():
    if not WEBHOOK_URL:
        print("❌ Falta el secret DISCORD_WEBHOOK")
        sys.exit(1)

    if LOOP_SECONDS <= 0:
        ejecutar_ciclo()
        return

    print(f"Modo demonio activo: ejecutando cada {LOOP_SECONDS}s...")
    while True:
        inicio = time.monotonic()
        try:
            ejecutar_ciclo()
        except Exception as e:
            print(f"❌ Error inesperado en el ciclo: {e}")
        transcurrido = time.monotonic() - inicio
        espera = max(5.0, LOOP_SECONDS - transcurrido)
        time.sleep(espera)

if __name__ == "__main__":
    main()
