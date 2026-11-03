import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
TZ = ZoneInfo("America/Lima")  # Hora Perú (UTC-5)
REPORT_HOURS = (8, 13, 20)     # Horas Perú en las que se envía el reporte

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
THIRTY_RE = re.compile(r"(?<!\d)30(?!\d)")  # "30", "30th", "30°" pero no "300" ni "130"
ENGLISH_RE = re.compile(r"ingl[eé]s|english", re.I)

# ---------- HTTP con reintentos ----------
def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

SESSION = make_session()
SCRAPER = cloudscraper.create_scraper() if cloudscraper else SESSION

# ---------- Estado persistente (anti-spam) ----------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"stock": {}, "reports": []}

def save_state(state):
    state["reports"] = state.get("reports", [])[-30:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- Filtros ----------
def _kw_match(kw, texto):
    # Palabras cortas: palabra completa. Largas: prefijo de palabra.
    pat = rf"\b{re.escape(kw)}\b" if len(kw) <= 3 else rf"\b{re.escape(kw)}"
    return re.search(pat, texto) is not None

def es_producto_valido(texto, url=""):
    if not texto:
        return False
    t = f"{texto} {url}".lower().replace("-", " ").replace("/", " ").replace("_", " ")
    if EXCLUDE_RE.search(t):
        return False
    if not THIRTY_RE.search(t):
        return False
    if ENGLISH_RE.search(t):
        return True
    return any(all(_kw_match(k, t) for k in conj) for conj in TARGET_KEYWORDS)

hallazgos = []

def registrar_producto(tienda, titulo, url, stock, precio="N/A", cart_url=None, sku="N/A"):
    if any(h["url"] == url for h in hallazgos):
        return
    hallazgos.append({
        "tienda": tienda, "titulo": titulo, "url": url, "stock": stock,
        "precio": precio, "cart_url": cart_url or url, "sku": sku,
    })

# ---------- Discord ----------
def post_discord(payload):
    if not WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK no configurado; no se envía nada.")
        return False
    try:
        res = SESSION.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Discord -> HTTP {res.status_code}")
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
        }],
    })

def enviar_reporte_programado(hora_label):
    if not hallazgos:
        descripcion = "No se encontraron publicaciones activas coincidentes en inglés."
    else:
        lineas = []
        for it in hallazgos:
            estado = "🟢 CON STOCK" if it["stock"] else "🔴 SIN STOCK"
            cart = f" | [🛒 Carrito]({it['cart_url']})" if it["stock"] else ""
            lineas.append(f"• **[{it['tienda']}]** [{it['titulo']}]({it['url']}) - **{estado}** ({it['precio']}){cart}")
        descripcion = "\n".join(lineas)[:4000]
    return post_discord({
        "content": f"📊 **REPORTE POKÉMON TCG 30TH (INGLÉS) - {hora_label} PET**",
        "embeds": [{
            "title": "Consolidado de Tiendas Rastreadas",
            "description": descripcion,
            "color": 3447003,
            "footer": {"text": f"Generado el {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} (hora Perú)"},
        }],
    })

# ---------- Scrapers ----------
def buscar_ilahui():
    print("\n--- [Ilahui Perú (Shopify)] ---")
    urls = ["https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-binder-collection-en-ingles"]
    for url in urls:
        try:
            r = SESSION.get(f"{url}.js", timeout=12)
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
    print(f"\n--- [{nombre} (VTEX)] ---")
    for term in ["30 aniversario", "tcg 30"]:
        q = urllib.parse.quote(term)
        api = f"{domain}/api/catalog_system/pub/products/search?ft={q}&_from=0&_to=30"
        try:
            r = SESSION.get(api, timeout=12)
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
    print("\n--- [Tai Loy] ---")
    url = "https://www.tailoy.com.pe/catalogsearch/result/?q=tcg+30+aniversario"
    try:
        r = SCRAPER.get(url, headers=HEADERS, timeout=15)
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
    print("\n--- [Phantom] ---")
    url = "https://phantom.pe/?s=tcg+30+aniversario&post_type=product"
    try:
        r = SCRAPER.get(url, headers=HEADERS, timeout=15)
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

# ---------- Main ----------
def main():
    ahora = datetime.now(TZ)
    print(f"Rastreo Pokémon (Inglés): {ahora:%Y-%m-%d %H:%M:%S} hora Perú")
    if not WEBHOOK_URL:
        print("❌ Falta el secret DISCORD_WEBHOOK")
        sys.exit(1)

    state = load_state()

    buscar_ilahui()
    buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe")
    buscar_vtex("Oechsle", "https://www.oechsle.pe")
    buscar_tailoy()
    buscar_phantom()

    # Alerta solo cuando pasa de SIN stock -> CON stock (evita spam cada 10 min)
    for item in hallazgos:
        antes = state["stock"].get(item["url"], False)
        if item["stock"] and not antes:
            print(f"¡Stock nuevo en {item['tienda']}!")
            enviar_alerta_stock_inmediato(item)
        state["stock"][item["url"]] = item["stock"]

    # Reportes: tolerante a retrasos de GitHub Actions (cualquier minuto de la hora)
    slot = f"{ahora:%Y-%m-%d}-{ahora.hour:02d}"
    if ahora.hour in REPORT_HOURS and slot not in state["reports"]:
        if enviar_reporte_programado(f"{ahora.hour:02d}:00"):
            state["reports"].append(slot)

    save_state(state)
    print("Escaneo finalizado.")

if __name__ == "__main__":
    main()
