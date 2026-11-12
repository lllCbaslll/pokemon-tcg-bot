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

# 4 reportes consolidados al día: 08:00, 12:00, 16:00 y 20:00 (Hora Perú)
REPORT_HOURS = tuple(
    int(h.strip())
    for h in os.getenv("REPORT_HOURS", "8,12,16,20").split(",")
    if h.strip().isdigit()
)

# COMPLETO (alertas de stock + 4 reportes) | SOLO_ALERTAS | FORZAR_REPORTE (1 reporte único y sale)
MODO = os.getenv("MODO_EJECUCION", "COMPLETO").strip().upper()

LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "25"))
DEEP_SCAN_EVERY = int(os.getenv("DEEP_SCAN_EVERY", "4"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "0"))

# Tiendas especializadas TCG con dominios activos verificados
TIENDAS_TCG = [
    ("Koko Station", "https://kokostation.com"),
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

EXCLUDE_RE = re.compile(
    r"espa[ñn]ol|\bspa\b|spanish|peluche|\bron\b|sorpresa|brillante|pok[eé]\s*ball",
    re.I,
)
THIRTY_RE = re.compile(r"(?<!\d)30(?!\d)")
ENGLISH_RE = re.compile(r"ingl[eé]s|english", re.I)
POKEMON_RE = re.compile(r"pok[eé]mon|\btcg\b", re.I)

# ---------- HTTP con pooling persistente ----------
def make_session():
    s = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(HEADERS)
    return s

SESSION = make_session()
SCRAPER = cloudscraper.create_scraper() if cloudscraper else SESSION
CURL_SESSION = curl_requests.Session(impersonate="chrome") if curl_requests else None

def browser_get(url, headers=None, use_proxy=False, timeout=12):
    req_headers = headers or BROWSER_HEADERS
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if (use_proxy and PROXY_URL) else None

    if CURL_SESSION is not None:
        return CURL_SESSION.get(
            url,
            headers=req_headers,
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

SHOPIFY_HANDLES_CACHE = {}
DISCOVERY_TTL = 600
DEEP_RESULTS_CACHE = {}

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
        return res.status_code < 300
    except Exception as e:
        print(f"Error enviando a Discord: {e}")
        return False

def enviar_alerta_stock_consolidado(items):
    """Envía un único embed con todos los productos que acaban de entrar en stock."""
    if not items:
        return False

    ordenados = sorted(items, key=lambda x: (x["tienda"], x["titulo"]))
    bloques = []
    actual = ""

    for it in ordenados:
        fila = f"[{it['tienda']}] | [{it['titulo']}]({it['url']}) | {it['precio']} | [🛒 Añadir al carrito]({it['cart_url']})\n"
        if len(actual) + len(fila) > 3800:
            bloques.append(actual.strip())
            actual = fila
        else:
            actual += fila

    if actual.strip():
        bloques.append(actual.strip())

    embeds = []
    for idx, desc in enumerate(bloques[:10]):
        embed = {
            "description": desc,
            "color": 3066993,
        }
        if idx == len(bloques[:10]) - 1:
            embed["footer"] = {
                "text": f"Detectado: {datetime.now(TZ).strftime('%d/%m/%Y %H:%M:%S')} PET"
            }
        embeds.append(embed)

    return post_discord({
        "content": "🚨¡STOCK EN INGLÉS DETECTADO!🟢@everyone",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": embeds,
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

# ---------- Carril Rápido (APIs ligeras cada 25s) ----------

def buscar_vtex(nombre, domain):
    for term in ["30 aniversario", "tcg 30"]:
        q = urllib.parse.quote(term)
        api = f"{domain}/api/catalog_system/pub/products/search?ft={q}&_from=0&_to=25"
        try:
            r = SESSION.get(api, timeout=8)
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
                registrar_producto(nombre, nom, link, stock, precio, cart, sku=sku_id)
        except Exception as e:
            print(f"[{nombre}] Error '{term}': {e}")


def buscar_shopify_rapido(nombre, domain, urls_fijas=None, forzar_descubrimiento=False):
    domain = domain.rstrip("/")
    ahora_ts = time.monotonic()
    cached = SHOPIFY_HANDLES_CACHE.get(domain)

    if forzar_descubrimiento or not cached or (ahora_ts - cached[0]) > DISCOVERY_TTL:
        handles = set(urls_fijas or [])
        es_shopify = bool(urls_fijas)

        try:
            r_all = browser_get(
                f"{domain}/products.json?limit=250",
                headers={**BROWSER_HEADERS, "Referer": f"{domain}/"},
                timeout=9,
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

        for term in ["pokemon 30", "tcg 30"]:
            try:
                q = urllib.parse.quote(term)
                api = f"{domain}/search/suggest.json?q={q}&resources[type]=product&resources[limit]=15"
                r = browser_get(api, headers={**BROWSER_HEADERS, "Referer": f"{domain}/"}, timeout=9)
                if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
                    es_shopify = True
                    for p in r.json().get("resources", {}).get("results", {}).get("products", []):
                        h = p.get("handle")
                        if h and es_producto_valido(p.get("title", ""), f"{domain}/products/{h}"):
                            handles.add(h)
            except Exception:
                pass

        SHOPIFY_HANDLES_CACHE[domain] = (ahora_ts, handles, es_shopify)
    else:
        _, handles, es_shopify = cached

    if not es_shopify:
        return False

    for handle in handles:
        prod_url = f"{domain}/products/{handle}"
        try:
            rj = browser_get(f"{prod_url}.js", headers={**BROWSER_HEADERS, "Referer": f"{domain}/"}, timeout=8)
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
            registrar_producto(nombre, titulo, prod_url, stock, precio, cart, sku=vid)
        except Exception as e:
            print(f"[{nombre}] Error detalle {handle}: {e}")

    return True

# ---------- Carril Profundo (Tiendas HTML / WAF) ----------

def _ejecutar_html_tcg(nombre, domain):
    encontrados = []
    domain = domain.rstrip("/")
    for term in ["pokemon 30", "tcg 30"]:
        q = urllib.parse.quote_plus(term)
        for search_url in (f"{domain}/?s={q}&post_type=product", f"{domain}/search?q={q}"):
            try:
                r = browser_get(
                    search_url,
                    headers={**BROWSER_HEADERS, "Referer": f"{domain}/", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"},
                    timeout=11,
                )
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                cards = soup.select("li.product, div.product, article.product, .product-card, .grid-product, .js-item-product")
                if not cards:
                    continue
                for card in cards:
                    t = card.select_one(".woocommerce-loop-product__title, .product-title, .card__heading, .js-item-name, h2, h3")
                    a = card.select_one("a[href]")
                    if not (t and a):
                        continue
                    titulo = t.get_text(strip=True)
                    link = a.get("href", "")
                    if link.startswith("/"):
                        link = f"{domain}{link}"
                    if not es_producto_valido(titulo, link):
                        continue
                    pr = card.select_one(".price ins .amount, .price .amount, .price, .money")
                    precio = pr.get_text(strip=True) if pr else "N/A"
                    txt = card.get_text(" ", strip=True).lower()
                    classes = " ".join(card.get("class", [])).lower()
                    stock = ("outofstock" not in classes) and ("sold-out" not in classes) and ("agotado" not in txt) and ("sin stock" not in txt)
                    encontrados.append((nombre, titulo, link, stock, precio, link, "N/A"))
                break
            except Exception:
                pass
    DEEP_RESULTS_CACHE[nombre] = encontrados
    for item in encontrados:
        registrar_producto(*item)


def buscar_phantom():
    encontrados = []
    for term in ["tcg 30 aniversario", "pokemon 30"]:
        q = urllib.parse.quote_plus(term)
        url = f"https://phantom.pe/?s={q}&post_type=product"
        try:
            r = browser_get(url, headers={**BROWSER_HEADERS, "Referer": "https://phantom.pe/"}, timeout=12)
            if r.status_code != 200:
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
                encontrados.append(("Phantom", titulo, link, stock, precio, cart, sku))
        except Exception:
            pass
    DEEP_RESULTS_CACHE["Phantom"] = encontrados
    for item in encontrados:
        registrar_producto(*item)


def buscar_tailoy():
    encontrados = []
    url = "https://www.tailoy.com.pe/catalogsearch/result/?q=tcg+30+aniversario"
    try:
        r = browser_get(url, headers={**BROWSER_HEADERS, "Referer": "https://www.tailoy.com.pe/"}, timeout=12)
        if r.status_code == 200:
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
                    encontrados.append(("Tai Loy", nombre, link, stock, precio, link, "N/A"))
    except Exception:
        pass
    DEEP_RESULTS_CACHE["Tai Loy"] = encontrados
    for item in encontrados:
        registrar_producto(*item)


def buscar_saga():
    encontrados = []
    for term in ["pokemon 30 aniversario", "tcg 30"]:
        q = urllib.parse.quote(term)
        url = f"https://www.falabella.com.pe/falabella-pe/search?Ntt={q}"
        saga_headers = {
            **BROWSER_HEADERS,
            "Referer": "https://www.falabella.com.pe/falabella-pe",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }
        try:
            r = browser_get(url, headers=saga_headers, use_proxy=True, timeout=13)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                nd = json.loads(next_script.string)
                results = nd.get("props", {}).get("pageProps", {}).get("results", [])
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
                        precio = f"S/ {p_val[0]}" if isinstance(p_val, list) and p_val else (f"S/ {p_val}" if p_val else "N/A")
                    stock = not bool(prod.get("isOutOfStock", False))
                    encontrados.append(("Saga Falabella", titulo, link, stock, precio, link, sku))
        except Exception:
            pass
    DEEP_RESULTS_CACHE["Saga Falabella"] = encontrados
    for item in encontrados:
        registrar_producto(*item)


def buscar_ripley():
    encontrados = []
    for term in ["pokemon 30 aniversario", "tcg 30"]:
        q = urllib.parse.quote(term)
        url = f"https://simple.ripley.com.pe/search/{q}"
        ripley_headers = {
            **BROWSER_HEADERS,
            "Referer": "https://simple.ripley.com.pe/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }
        try:
            r = browser_get(url, headers=ripley_headers, use_proxy=True, timeout=14)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
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
                    sku = str(prod.get("partNumber") or prod.get("sku") or "N/A")
                    prices = prod.get("prices") or {}
                    p_num = prices.get("offerPrice") or prices.get("listPrice") or prod.get("price")
                    precio = f"S/ {p_num}" if p_num else "N/A"
                    stock = not bool(prod.get("isOutOfStock", False))
                    encontrados.append(("Ripley", titulo, link, stock, precio, link, sku))
        except Exception:
            pass
    DEEP_RESULTS_CACHE["Ripley"] = encontrados
    for item in encontrados:
        registrar_producto(*item)


def buscar_pharmax():
    encontrados = []
    for term in ["pokemon 30", "tcg 30"]:
        q = urllib.parse.quote_plus(term)
        for url in (
            f"https://www.pharmax.com.pe/catalogsearch/result/?q={q}",
            f"https://pharmax.com.pe/?s={q}&post_type=product",
        ):
            try:
                r = browser_get(url, headers={**BROWSER_HEADERS, "Referer": "https://www.pharmax.com.pe/"}, timeout=12)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                items = soup.select(".product-item, .product-item-info, li.product, div.product")
                if not items:
                    continue
                for item in items:
                    el = item.select_one(".product-item-link, .woocommerce-loop-product__title, .product-title, h2 a, h3 a")
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
                    encontrados.append(("Pharmax", titulo, link, stock, precio, link, "N/A"))
                break
            except Exception:
                pass
    DEEP_RESULTS_CACHE["Pharmax"] = encontrados
    for item in encontrados:
        registrar_producto(*item)

# ---------- Ejecución ----------
def ejecutar_ciclo(num_ciclo=1):
    t0 = time.monotonic()
    hallazgos.clear()
    ahora = datetime.now(TZ)

    es_reporte_forzado = MODO in ("FORZAR_REPORTE", "REPORTE")
    es_profundo = (num_ciclo == 1) or (num_ciclo % max(1, DEEP_SCAN_EVERY) == 0) or es_reporte_forzado

    tareas_rapidas = [
        lambda: buscar_shopify_rapido(
            "Ilahui",
            "https://ilahuiperu.com",
            urls_fijas=["pokemon-tcg-30-aniversario-binder-collection-en-ingles"],
            forzar_descubrimiento=es_profundo,
        ),
        lambda: buscar_vtex("Oechsle", "https://www.oechsle.pe"),
        lambda: buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe"),
    ]

    tcg_no_shopify = []
    for nom, dom in TIENDAS_TCG:
        cached = SHOPIFY_HANDLES_CACHE.get(dom.rstrip("/"))
        if cached and not cached[2]:
            tcg_no_shopify.append((nom, dom))
        else:
            tareas_rapidas.append(
                lambda n=nom, d=dom: buscar_shopify_rapido(n, d, forzar_descubrimiento=es_profundo)
            )

    tareas_profundas = []
    if es_profundo:
        tareas_profundas = [
            buscar_phantom,
            buscar_tailoy,
            buscar_saga,
            buscar_ripley,
            buscar_pharmax,
        ] + [
            (lambda n=nom, d=dom: _ejecutar_html_tcg(n, d))
            for nom, dom in tcg_no_shopify
        ]
    else:
        for items_cached in DEEP_RESULTS_CACHE.values():
            for item in items_cached:
                registrar_producto(*item)

    todas = tareas_rapidas + tareas_profundas
    with ThreadPoolExecutor(max_workers=12) as pool:
        for f in [pool.submit(fn) for fn in todas]:
            f.result()

    state = load_state()

    # 1) Alerta inmediata consolidada SOLO cuando hay productos nuevos en stock
    if not es_reporte_forzado:
        nuevos_en_stock = []
        for item in hallazgos:
            key = item["url_key"]
            antes = state["stock"].get(key, False)
            if item["stock"] and not antes:
                print(f"🚨 ¡STOCK NUEVO en {item['tienda']}: {item['titulo']}!")
                nuevos_en_stock.append(item)
            elif not item["stock"]:
                state["stock"][key] = False

        if nuevos_en_stock:
            if enviar_alerta_stock_consolidado(nuevos_en_stock):
                for item in nuevos_en_stock:
                    state["stock"][item["url_key"]] = True

    # 2) Reportes consolidados: únicamente 1 vez en cada hora de REPORT_HOURS (08, 12, 16, 20)
    if es_reporte_forzado:
        enviar_reporte_programado(f"{ahora:%H:%M}")
    elif MODO != "SOLO_ALERTAS":
        slot = f"{ahora:%Y-%m-%d}-{ahora.hour:02d}"
        if ahora.hour in REPORT_HOURS and slot not in state["reports"]:
            if enviar_reporte_programado(f"{ahora.hour:02d}:00"):
                state["reports"].append(slot)

    save_state(state)
    dur = time.monotonic() - t0
    modo_txt = "PROFUNDO+API" if es_profundo else "RÁPIDO (API)"
    con_stock = sum(1 for h in hallazgos if h["stock"])
    print(f"[{ahora:%H:%M:%S}] Ciclo #{num_ciclo} [{modo_txt}] en {dur:.2f}s | {len(hallazgos)} productos ({con_stock} con stock).")


def main():
    if not WEBHOOK_URL:
        print("❌ Falta DISCORD_WEBHOOK")
        sys.exit(1)

    if LOOP_SECONDS <= 0 or MODO in ("FORZAR_REPORTE", "REPORTE"):
        ejecutar_ciclo(1)
        return

    inicio_total = time.monotonic()
    ciclo = 0
    print(
        f"🚀 Monitor activo: carril rápido cada {LOOP_SECONDS}s | "
        f"carril profundo cada {LOOP_SECONDS * DEEP_SCAN_EVERY}s | "
        f"reportes a las {','.join(f'{h:02d}:00' for h in REPORT_HOURS)} PET"
    )

    while True:
        ciclo += 1
        inicio_ciclo = time.monotonic()
        try:
            ejecutar_ciclo(ciclo)
        except Exception as e:
            print(f"❌ Error en ciclo #{ciclo}: {e}")

        if MAX_RUNTIME_SECONDS > 0:
            if (time.monotonic() - inicio_total + LOOP_SECONDS) >= MAX_RUNTIME_SECONDS:
                break

        jitter = random.uniform(-2.0, 2.0)
        espera = max(5.0, LOOP_SECONDS - (time.monotonic() - inicio_ciclo) + jitter)
        time.sleep(espera)


if __name__ == "__main__":
    main()
