import os
import time
from datetime import datetime
import requests
import urllib.parse
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# Carga la URL de la variable secreta configurada en GitHub Actions
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
}

# Conjuntos de palabras clave ampliados (incluyendo bundles, binder, mini tin y cartas sueltas)
TARGET_KEYWORDS = [
    ["30", "booster", "bundle"],
    ["30", "ultra", "premium"],
    ["tcg", "30", "ingles"],
    ["cartas", "pokemon", "30"],
    ["30th", "celeb", "booster"],
    ["30", "binder"],
    ["30", "mini", "tin"],
    ["30", "carta", "suelta"],
    ["30", "aniversario"]
]

# Estructura global para acumular los hallazgos durante el rastreo
hallazgos = []

def coincide_palabras_clave(texto):
    if not texto:
        return False
    texto_lower = texto.lower()
    for conjunto in TARGET_KEYWORDS:
        if all(kw in texto_lower for kw in conjunto):
            return True
    return False

def registrar_producto(tienda, titulo, url_directa, tiene_stock, precio="N/A"):
    hallazgos.append({
        "tienda": tienda,
        "titulo": titulo,
        "url": url_directa,
        "stock": tiene_stock,
        "precio": precio
    })

def enviar_alerta_stock_inmediato(item):
    """Notificación instantánea a Discord enviada únicamente cuando hay stock"""
    payload = {
        "content": f"🚨 **¡STOCK DETECTADO EN {item['tienda'].upper()}!** @everyone",
        "embeds": [{
            "title": f"🟢 {item['titulo']}",
            "url": item['url'],
            "color": 3066993,  # Verde
            "fields": [
                {"name": "Estado", "value": "🟢 CON STOCK DISPONIBLE", "inline": True},
                {"name": "Tienda", "value": item['tienda'], "inline": True},
                {"name": "Precio", "value": str(item['precio']), "inline": True}
            ]
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Alerta enviada para {item['tienda']} -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando alerta instantánea: {e}")

def enviar_reporte_programado(hora_label):
    """Reporte consolidado con todos los productos encontrados"""
    if not hallazgos:
        descripcion = "No se encontraron publicaciones activas coincidentes en las tiendas rastreadas."
    else:
        lineas = []
        for item in hallazgos:
            estado = "🟢 CON STOCK" if item['stock'] else "🔴 SIN STOCK"
            lineas.append(f"• **[{item['tienda']}]** [{item['titulo']}]({item['url']}) - **{estado}** (Precio: {item['precio']})")
        descripcion = "\n".join(lineas)

    payload = {
        "content": f"📊 **REPORTE DE ESTADO POKÉMON TCG 30TH ({hora_label} HORA PERÚ)**",
        "embeds": [{
            "title": "Consolidado de Tiendas Rastreadas",
            "description": descripcion,
            "color": 3447003,  # Azul
            "footer": {"text": f"Generado el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"}
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Reporte de las {hora_label} enviado exitosamente a Discord -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando reporte programado: {e}")

# --- MÓDULOS DE RASTREO DE TIENDAS ---

def buscar_ilahui():
    print("\n--- [Ilahui Perú] ---")
    
    # 1. Monitoreo por API pública de Shopify (búsqueda dinámica)
    api_url = "https://ilahuiperu.com/search/suggest.json?q=pokemon+30&resources[type]=product"
    try:
        res = requests.get(api_url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            products = data.get("resources", {}).get("results", {}).get("products", [])
            for p in products:
                titulo = p.get("title", "")
                handle = p.get("handle", "")
                url_directa = f"https://ilahuiperu.com/products/{handle}" if handle else "https://ilahuiperu.com"
                precio = f"S/ {p.get('price', 'N/A')}"
                
                if coincide_palabras_clave(titulo):
                    tiene_stock = p.get("available", False)
                    print(f"[Ilahui API] {titulo} | Stock: {tiene_stock} | Precio: {precio}")
                    registrar_producto("Ilahui", titulo, url_directa, tiene_stock, precio)
    except Exception as e:
        print(f"[Ilahui API Warning]: {e}")

    # 2. Respaldo directo a enlaces específicos proporcionados
    urls_referencia = [
        "https://ilahuiperu.com/products/pokemon-tcg-30th-celeb-boster-bundle-espanol",
        "https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-binder-collection-en-ingles",
        "https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-mini-tin-en-espanol",
        "https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-carta-suelta-en-espanol"
    ]
    
    urls_registradas = [item['url'] for item in hallazgos if item['tienda'] == 'Ilahui']
    
    for url in urls_referencia:
        if url in urls_registradas:
            continue
        try:
            res_js = requests.get(f"{url}.js", headers=HEADERS, timeout=10)
            if res_js.status_code == 200:
                p_data = res_js.json()
                titulo = p_data.get("title", "Pokémon TCG 30th")
                precio = f"S/ {p_data.get('price', 0) / 100:.2f}"
                tiene_stock = p_data.get("available", False)
                print(f"[Ilahui JS] {titulo} | Stock: {tiene_stock} | Precio: {precio}")
                registrar_producto("Ilahui", titulo, url, tiene_stock, precio)
        except Exception as e:
            print(f"[Ilahui Direct Warning]: {e}")

def buscar_vtex(nombre, domain):
    print(f"\n--- [{nombre}] ---")
    query = urllib.parse.quote("tcg 30 aniversario")
    api_url = f"{domain}/api/catalog_system/pub/products/search?ft={query}&_from=0&_to=15"
    try:
        res = requests.get(api_url, headers=HEADERS, timeout=10)
        if res.status_code in [200, 206]:
            productos = res.json()
            if isinstance(productos, list):
                for prod in productos:
                    nombre_p = prod.get("productName", "")
                    if coincide_palabras_clave(nombre_p):
                        tiene_stock = False
                        precio = "N/A"
                        for item in prod.get("items", []):
                            for seller in item.get("sellers", []):
                                comm = seller.get("commertialOffer", {})
                                if comm.get("AvailableQuantity", 0) > 0 and comm.get("IsAvailable", False):
                                    tiene_stock = True
                                    if "Price" in comm:
                                        precio = f"S/ {comm['Price']}"
                                    break
                        link_prod = prod.get("link", domain)
                        print(f"[{nombre}] {nombre_p} | Stock: {tiene_stock} | Precio: {precio}")
                        registrar_producto(nombre, nombre_p, link_prod, tiene_stock, precio)
    except Exception as e:
        print(f"[{nombre}] Error: {e}")

def buscar_tailoy():
    print("\n--- [Tai Loy] ---")
    url_search = "https://www.tailoy.com.pe/catalogsearch/result/?q=tcg+30+aniversario"
    try:
        scraper = cloudscraper.create_scraper() if cloudscraper else requests
        res = scraper.get(url_search, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for prod in soup.select(".product-item, .product-item-info"):
                nombre_el = prod.select_one(".product-item-link")
                precio_el = prod.select_one(".price")
                if nombre_el:
                    nombre = nombre_el.get_text(strip=True)
                    link_directo = nombre_el.get("href", "")
                    precio = precio_el.get_text(strip=True) if precio_el else "N/A"
                    if coincide_palabras_clave(nombre):
                        tiene_stock = "sin stock" not in prod.text.lower() and "agotado" not in prod.text.lower()
                        print(f"[Tai Loy] {nombre} | Stock: {tiene_stock} | Precio: {precio}")
                        registrar_producto("Tai Loy", nombre, link_directo, tiene_stock, precio)
    except Exception as e:
        print(f"[Tai Loy] Error: {e}")

def buscar_phantom():
    print("\n--- [Phantom] ---")
    url_search = "https://phantom.pe/?s=tcg+30+aniversario&post_type=product"
    try:
        scraper = cloudscraper.create_scraper() if cloudscraper else requests
        res = scraper.get(url_search, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for item in soup.select("li.product, div.product, article"):
                titulo_el = item.select_one(".woocommerce-loop-product__title, .product-title, h2, h3")
                link_el = item.select_one("a")
                precio_el = item.select_one(".price, .amount")
                if titulo_el and link_el:
                    titulo = titulo_el.get_text(strip=True)
                    link_directo = link_el.get("href", "")
                    precio = precio_el.get_text(strip=True) if precio_el else "N/A"
                    if coincide_palabras_clave(titulo):
                        tiene_stock = "agotado" not in item.text.lower()
                        print(f"[Phantom] {titulo} | Stock: {tiene_stock} | Precio: {precio}")
                        registrar_producto("Phantom", titulo, link_directo, tiene_stock, precio)
    except Exception as e:
        print(f"[Phantom] Error: {e}")

if __name__ == "__main__":
    hora_actual = datetime.now()
    print(f"Ejecutando rastreo: {hora_actual.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    
    # 1. Rastrear tiendas activas (incluyendo Ilahui)
    buscar_ilahui()
    buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe")
    buscar_vtex("Oechsle", "https://www.oechsle.pe")
    buscar_tailoy()
    buscar_phantom()
    
    # 2. Alerta instantánea: SOLO notifica si se detecta stock disponible
    stock_encontrado = False
    for item in hallazgos:
        if item["stock"]:
            stock_encontrado = True
            print(f"¡Atención! Stock encontrado en {item['tienda']}. Enviando alerta instantánea...")
            enviar_alerta_stock_inmediato(item)

    if not stock_encontrado:
        print("Sin novedades de stock en ninguna tienda. Escaneo finalizado en silencio.")

    # 3. Reportes consolidados programados (3 veces al día Hora Perú)
    # 08:00 AM PET = 13:00 UTC | 01:00 PM PET = 18:00 UTC | 08:00 PM PET = 01:00 UTC
    if hora_actual.hour == 13 and hora_actual.minute < 5:
        enviar_reporte_programado("08:00 AM")
    elif hora_actual.hour == 18 and hora_actual.minute < 5:
        enviar_reporte_programado("01:00 PM")
    elif hora_actual.hour == 1 and hora_actual.minute < 5:
        enviar_reporte_programado("08:00 PM")

    print("Escaneo finalizado exitosamente.")
