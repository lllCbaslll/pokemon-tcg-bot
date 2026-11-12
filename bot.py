import json
import os
import random
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

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

# ---------- Configuración ----------
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
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

# 0 = ejecución única (GitHub Actions / cron) | >0 = bucle continuo en segundos (VPS)
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "0"))

# Lista configurable de tiendas especializadas de TCG en Perú
# Formato: ("Nombre visible", "https://dominio-sin-slash-final")
TIENDAS_TCG = [
    ("Koko Station", "https://kokostation.com"),
    ("MagiCards", "https://magicards.pe"),
    ("Lima TCG", "https://limatcg.com"),
    ("Tiendas Élite", "https://tiendaselite.com"),
    ("TCG Collectibles Perú", "https://tcgcollectiblesperu.com"),
    ("PokéStore Perú", "https://pokestoreperu.com"),
    ("D20 Games", "https://d20games.pe"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
}

BROWSER_HEADERS = {
    **HEADERS,
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

TARGET_KEYWORDS = [
    ["booster"], ["bundle"], ["ultra"], ["premium"], ["binder"], ["tin"],
    ["suelta"], ["aniversario"], ["pack"], ["caja"], ["sobre"], ["álbum"],
    ["album"], ["poster"], ["entrenador"], ["élite"], ["elite"], ["ex"],
    ["colección"], ["coleccion"], ["tcg"], ["cartas", "pokemon"],
]

# Excluye español, peluches, sorpresas, brillante y cualquier variante de Pokeball
EXCLUDE_RE = re.compile(
    r"espa[ñn]ol|\bspa\b|spanish|peluche|\bron\b|sorpresa|brillante|pok[eé]\s*ball",
    re.I,
)
THIRTY_RE = re.compile(r"(?<!\d)30(?!\d)")
ENGLISH_RE = re.compile(r"ingl[eé]s|english", re.I)
POKEMON_RE = re.compile(r"pok[eé]mon|\btcg\b", re.I)

# ---------- HTTP con reintentos e impersonación TLS ----------
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

def browser_get(url, headers=None, use_proxy=False, timeout=14):
    """Realiza GET con huella TLS de Chrome (curl_cffi) y proxy opcional."""
    req_headers = headers or BROWSER_HEADERS
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if (use_proxy and PROXY_URL) else None

    if curl_requests is not None:
        return curl_requests.get(
            url,
            headers=req_headers,
            impersonate="chrome",
            proxies=proxies,
            timeout=timeout,
        )

    return SCRAPER.get(
        url,
        headers=req_headers,
        proxies=proxies,
        timeout=timeout,
    )

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
    if not url:
        return
    url_limpia = url.split("?")[0].rstrip("/")
    with hallazgos_lock:
        if any(h["url_key"] == url_limpia for h in hallazgos):
            return
        hallazgos.append({
            "tienda": tienda,
            "titulo": titulo.strip(),
            "url": url,
            "url_key": url_limpia,
            "stock": bool(stock),
            "precio": precio or "N/A",
            "cart_url": cart_url or url,
            "sku": sku or "N/A",
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
                {"name": "🛒 Comprar", "value": f"[Ir / Añadir al Carrito]({item['cart_url']})", "inline": False},
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

# ---------- Scrapers Retail ----------

def buscar_ilahui():
    """Shopify: revisa URLs directas + búsqueda dinámica en Ilahui Perú."""
    urls_directas = {
        "https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-binder-collection-en-ingles"
    }
    for term in ["pokemon 30", "tcg 30"]:
        try:
            q = urllib.parse.quote(term)
            sr = SESSION.get(
                f"https://ilahuiperu.com/search/suggest.json?q={q}&resources[type]=product",
                timeout=10,
            )
            if sr.status_code == 200:
                prods = sr.json().get("resources", {}).get("results", {}).get("products", [])
                for p in prods:
                    handle = p.get("handle")
                    if handle:
                        urls_directas.add(f"https://ilahuiperu.com/products/{handle}")
        except Exception as e:
            print(f"[Ilahui] Error búsqueda '{term}': {e}")

    for url in urls_directas:
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
            print(f"[Ilahui] Error en {url}: {e}")


def buscar_vtex(nombre, domain):
    """VTEX API: usado por Oechsle y Plaza Vea."""
    for term in ["30 aniversario", "tcg 30", "pokemon 30"]:
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


def buscar_phantom():
    """WooCommerce: Phantom Perú."""
    for idx, term in enumerate(["tcg 30 aniversario", "pokemon 30"]):
        if idx > 0:
            time.sleep(random.uniform(1.0, 2.2))
        q = urllib.parse.quote_plus(term)
        url = f"https://phantom.pe/?s={q}&post_type=product"
        try:
            r = browser_get(
                url,
                headers={**BROWSER_HEADERS, "Referer": "https://phantom.pe/"},
                use_proxy=False,
                timeout=14,
            )
            if r.status_code != 200:
                print(f"[Phantom] HTTP {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for item in soup.select("li.product, div.product, article.product"):
                t = item.select_one(".woocommerce-loop-product__title, .product-title, h2, h3")
                a = item.select_one("a")
                if not (t and a):
                    continue
                titulo, link = t.get_text(strip=True), a.get("href", "")
                if not es_producto_valido(titulo, link):
                    continue
                pr = item.select_one(".price ins .amount, .price .amount, .price")
                precio = pr.get_text(strip=True) if pr else "N/A"
                txt = item.get_text(" ", strip=True).lower()
                classes = " ".join(item.get("class", [])).lower()
                stock = ("outofstock" not in classes) and ("agotado" not in txt) and ("sin stock" not in txt)
                add_btn = item.select_one("a[data-product_id]")
                sku = add_btn.get("data-product_id") if add_btn else "N/A"
                cart = f"https://phantom.pe/?add-to-cart={sku}" if (stock and sku != "N/A") else link
                print(f"[Phantom] {titulo} | Stock: {stock}")
                registrar_producto("Phantom", titulo, link, stock, precio, cart, sku=sku)
        except Exception as e:
            print(f"[Phantom] Error con '{term}': {e}")


def buscar_tailoy():
    """Magento: Tai Loy Perú."""
    url = "https://www.tailoy.com.pe/catalogsearch/result/?q=tcg+30+aniversario"
    try:
        r = browser_get(
            url,
            headers={**BROWSER_HEADERS, "Referer": "https://www.tailoy.com.pe/"},
            use_proxy=False,
            timeout=14,
        )
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


def buscar_saga():
    """Saga Falabella Perú: API interna con huella TLS Chrome y soporte PROXY_URL."""
    for idx, term in enumerate(["pokemon 30 aniversario", "tcg 30"]):
        if idx > 0:
            time.sleep(random.uniform(1.2, 2.5))
        q = urllib.parse.quote(term)
        api = f"https://www.falabella.com.pe/s/browse/v1/search/pe?Ntt={q}"
        saga_headers = {
            **BROWSER_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://www.falabella.com.pe/falabella-pe/search?Ntt={q}",
            "Origin": "https://www.falabella.com.pe",
        }
        try:
            r = browser_get(api, headers=saga_headers, use_proxy=True, timeout=15)
            if r.status_code != 200:
                print(f"[Saga Falabella] HTTP {r.status_code} en '{term}'")
                continue
            data = r.json()
            results = (
                data.get("data", {}).get("results")
                or data.get("results")
                or []
            )
            for prod in results:
                titulo = prod.get("displayName") or prod.get("title") or ""
                link = prod.get("url") or ""
                if link.startswith("/"):
                    link = f"https://www.falabella.com.pe{link}"
                if not es_producto_valido(titulo, link):
                    continue

                sku = str(prod.get("skuId") or prod.get("productId") or "N/A")
                prices = prod.get("prices") or []
                precio = "N/A"
                if prices and isinstance(prices, list):
                    p_val = prices[0].get("price")
                    if isinstance(p_val, list) and p_val:
                        precio = f"S/ {p_val[0]}"
                    elif p_val:
                        precio = f"S/ {p_val}"

                stock = not bool(prod.get("isOutOfStock", False))
                print(f"[Saga Falabella] {titulo} | Stock: {stock}")
                registrar_producto("Saga Falabella", titulo, link, stock, precio, link, sku=sku)
        except Exception as e:
            print(f"[Saga Falabella] Error con '{term}': {e}")


def buscar_ripley():
    """Ripley Perú: __NEXT_DATA__ / HTML con huella TLS Chrome y soporte PROXY_URL."""
    for idx, term in enumerate(["pokemon 30 aniversario", "tcg 30"]):
        if idx > 0:
            time.sleep(random.uniform(1.2, 2.5))
        q = urllib.parse.quote(term)
        url = f"https://simple.ripley.com.pe/search/{q}"
        ripley_headers = {
            **BROWSER_HEADERS,
            "Referer": "https://simple.ripley.com.pe/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }
        try:
            r = browser_get(url, headers=ripley_headers, use_proxy=True, timeout=16)
            if r.status_code != 200:
                print(f"[Ripley] HTTP {r.status_code} en '{term}'")
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            encontrados_json = False

            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    nd = json.loads(next_script.string)
                    props = nd.get("props", {}).get("pageProps", {})
                    productos = (
                        props.get("products")
                        or props.get("searchData", {}).get("products")
                        or props.get("initialState", {}).get("search", {}).get("products")
                        or []
                    )
                    for prod in productos:
                        titulo = prod.get("name") or prod.get("title") or ""
                        link = prod.get("url") or ""
                        if link.startswith("/"):
                            link = f"https://simple.ripley.com.pe{link}"
                        if not es_producto_valido(titulo, link):
                            continue
                        encontrados_json = True
                        sku = str(prod.get("partNumber") or prod.get("sku") or "N/A")
                        prices = prod.get("prices") or {}
                        p_num = prices.get("offerPrice") or prices.get("listPrice") or prod.get("price")
                        precio = f"S/ {p_num}" if p_num else "N/A"
                        stock = not bool(prod.get("isOutOfStock", False))
                        print(f"[Ripley] {titulo} | Stock: {stock}")
                        registrar_producto("Ripley", titulo, link, stock, precio, link, sku=sku)
                except Exception:
                    pass

            if not encontrados_json:
                for card in soup.select(".catalog-product-item, [data-partnumber]"):
                    a = card.select_one("a[href]")
                    t = card.select_one(".catalog-product-details__name, .product-name, h3, h4")
                    if not (a and t):
                        continue
                    titulo = t.get_text(strip=True)
                    link = a.get("href", "")
                    if link.startswith("/"):
                        link = f"https://simple.ripley.com.pe{link}"
                    if not es_producto_valido(titulo, link):
                        continue
                    pr = card.select_one(".catalog-prices__offer-price, .catalog-prices__list-price, .price")
                    precio = pr.get_text(strip=True) if pr else "N/A"
                    sku = card.get("data-partnumber", "N/A")
                    txt = card.get_text(" ", strip=True).lower()
                    stock = ("agotado" not in txt) and ("sin stock" not in txt)
                    print(f"[Ripley] {titulo} | Stock: {stock}")
                    registrar_producto("Ripley", titulo, link, stock, precio, link, sku=sku)
        except Exception as e:
            print(f"[Ripley] Error con '{term}': {e}")


def buscar_pharmax():
    """Pharmax Perú: búsqueda en catálogo web."""
    for idx, term in enumerate(["pokemon 30", "tcg 30"]):
        if idx > 0:
            time.sleep(random.uniform(1.0, 2.0))
        q = urllib.parse.quote_plus(term)
        urls = [
            f"https://www.pharmax.com.pe/catalogsearch/result/?q={q}",
            f"https://pharmax.com.pe/?s={q}&post_type=product",
        ]
        for url in urls:
            try:
                r = browser_get(
                    url,
                    headers={**BROWSER_HEADERS, "Referer": "https://www.pharmax.com.pe/"},
                    use_proxy=False,
                    timeout=14,
                )
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                items = soup.select(".product-item, .product-item-info, li.product, div.product")
                if not items:
                    continue
                for item in items:
                    el = item.select_one(
                        ".product-item-link, .woocommerce-loop-product__title, .product-title, h2 a, h3 a"
                    )
                    if not el:
                        continue
                    titulo = el.get_text(strip=True)
                    link = el.get("href") or (item.select_one("a") or {}).get("href", "")
                    if link.startswith("/"):
                        link = f"https://www.pharmax.com.pe{link}"
                    if not es_producto_valido(titulo, link):
                        continue
                    pr = item.select_one(".price, .amount")
                    precio = pr.get_text(strip=True) if pr else "N/A"
                    txt = item.get_text(" ", strip=True).lower()
                    stock = ("agotado" not in txt) and ("sin stock" not in txt) and ("outofstock" not in txt)
                    print(f"[Pharmax] {titulo} | Stock: {stock}")
                    registrar_producto("Pharmax", titulo, link, stock, precio, link)
                break
            except Exception as e:
                print(f"[Pharmax] Error en {url}: {e}")


# ---------- Scraper Genérico para Tiendas Especializadas TCG ----------

def buscar_tienda_tcg(nombre, domain):
    """
    Motor unificado para tiendas especializadas de cartas en Perú (Koko Station, MagiCards, etc.).
    1) Feed JSON de Shopify (/products.json) + búsqueda predictiva (/search/suggest.json) + detalle (.js)
    2) Si no es Shopify, hace fallback a búsqueda HTML (WooCommerce / Jumpseller / Tiendanube)
    """
    domain = domain.rstrip("/")
    handles = set()
    es_shopify = False

    # Paso 1A: Feed general de productos Shopify
    try:
        r_all = browser_get(
            f"{domain}/products.json?limit=250",
            headers={**BROWSER_HEADERS, "Referer": f"{domain}/"},
            timeout=10,
        )
        if r_all.status_code == 200 and "application/json" in r_all.headers.get("Content-Type", ""):
            es_shopify = True
            for p in r_all.json().get("products", []):
                h = p.get("handle")
                t = p.get("title", "")
                if h and es_producto_valido(t, f"{domain}/products/{h}"):
                    handles.add(h)
    except Exception:
        pass

    # Paso 1B: Endpoint predictivo de Shopify
    for term in ["pokemon 30", "tcg 30", "30 aniversario"]:
        try:
            q = urllib.parse.quote(term)
            api = f"{domain}/search/suggest.json?q={q}&resources[type]=product&resources[limit]=20"
            r = browser_get(api, headers={**BROWSER_HEADERS, "Referer": f"{domain}/"}, timeout=10)
            if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
                es_shopify = True
                prods = r.json().get("resources", {}).get("results", {}).get("products", [])
                for p in prods:
                    h = p.get("handle")
                    if h:
                        handles.add(h)
        except Exception:
            pass

    if es_shopify:
        for handle in handles:
            prod_url = f"{domain}/products/{handle}"
            try:
                rj = browser_get(f"{prod_url}.js", headers={**BROWSER_HEADERS, "Referer": f"{domain}/"}, timeout=10)
                if rj.status_code != 200:
                    continue
                p = rj.json()
                titulo = p.get("title", "")
                if not es_producto_valido(titulo, prod_url):
                    continue
                variants = p.get("variants", [])
                disp_var = next((v for v in variants if v.get("available")), variants[0] if variants else None)
                vid = disp_var.get("id") if disp_var else None
                stock = any(v.get("available") for v in variants) if variants else bool(p.get("available", False))
                raw_price = (disp_var.get("price") if disp_var else p.get("price")) or 0
                precio = f"S/ {raw_price / 100:.2f}" if raw_price else "N/A"
                cart = f"{domain}/cart/add?id={vid}&quantity=1" if (stock and vid) else prod_url
                print(f"[{nombre}] {titulo} | Stock: {stock}")
                registrar_producto(nombre, titulo, prod_url, stock, precio, cart, sku=vid)
            except Exception as e:
                print(f"[{nombre}] Error detalle {handle}: {e}")
        return

    # Paso 2: Fallback HTML (WooCommerce, Jumpseller, BinderPOS, etc.)
    for term in ["pokemon 30", "tcg 30"]:
        q = urllib.parse.quote_plus(term)
        for search_url in (f"{domain}/?s={q}&post_type=product", f"{domain}/search?q={q}"):
            try:
                r = browser_get(
                    search_url,
                    headers={
                        **BROWSER_HEADERS,
                        "Referer": f"{domain}/",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                    },
                    timeout=12,
                )
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                cards = soup.select(
                    "li.product, div.product, article.product, .product-card, .grid-product, .product-block, .js-item-product"
                )
                if not cards:
                    continue
                for card in cards:
                    t = card.select_one(
                        ".woocommerce-loop-product__title, .product-title, .card__heading, .product-block__title, .js-item-name, h2, h3"
                    )
                    a = card.select_one("a[href]")
                    if not (t and a):
                        continue
                    titulo = t.get_text(strip=True)
                    link = a.get("href", "")
                    if link.startswith("/"):
                        link = f"{domain}{link}"
                    if not es_producto_valido(titulo, link):
                        continue
                    pr = card.select_one(".price ins .amount, .price .amount, .price, .money, .js-price-display")
                    precio = pr.get_text(strip=True) if pr else "N/A"
                    txt = card.get_text(" ", strip=True).lower()
                    classes = " ".join(card.get("class", [])).lower()
                    stock = (
                        ("outofstock" not in classes)
                        and ("sold-out" not in classes)
                        and ("agotado" not in txt)
                        and ("sin stock" not in txt)
                        and ("sold out" not in txt)
                    )
                    print(f"[{nombre}] {titulo} | Stock: {stock}")
                    registrar_producto(nombre, titulo, link, stock, precio, link)
                break
            except Exception as e:
                print(f"[{nombre}] Error en {search_url}: {e}")


# ---------- Ejecución ----------
def ejecutar_ciclo():
    hallazgos.clear()
    ahora = datetime.now(TZ)
    print(
        f"\n=== Rastreo Pokémon (Inglés): {ahora:%Y-%m-%d %H:%M:%S} PET | "
        f"curl_cffi={'ON' if curl_requests else 'OFF'} | proxy={'ON' if PROXY_URL else 'OFF'} ==="
    )

    state = load_state()

    tareas = [
        buscar_ilahui,
        lambda: buscar_vtex("Oechsle", "https://www.oechsle.pe"),
        lambda: buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe"),
        buscar_phantom,
        buscar_tailoy,
        buscar_saga,
        buscar_ripley,
        buscar_pharmax,
    ] + [
        (lambda nom=nom, dom=dom: buscar_tienda_tcg(nom, dom))
        for nom, dom in TIENDAS_TCG
    ]

    with ThreadPoolExecutor(max_workers=10) as pool:
        futuros = [pool.submit(fn) for fn in tareas]
        for f in futuros:
            f.result()

    es_reporte = MODO in ("FORZAR_REPORTE", "REPORTE")

    if not es_reporte:
        for item in hallazgos:
            key = item["url_key"]
            antes = state["stock"].get(key, False)
            if item["stock"] and not antes:
                print(f"🚨 ¡Stock nuevo en {item['tienda']}: {item['titulo']}!")
                if enviar_alerta_stock_inmediato(item):
                    state["stock"][key] = True
            elif not item["stock"]:
                state["stock"][key] = False

    if MODO == "FORZAR_REPORTE":
        enviar_reporte_programado(f"{ahora:%H:%M}")
    elif MODO != "SOLO_ALERTAS":
        slot = f"{ahora:%Y-%m-%d}-{ahora.hour:02d}"
        if ahora.hour in REPORT_HOURS and slot not in state["reports"]:
            if enviar_reporte_programado(f"{ahora.hour:02d}:00"):
                state["reports"].append(slot)

    save_state(state)
    print(f"Escaneo finalizado ({len(hallazgos)} productos evaluados en {len(tareas)} tiendas).")


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
