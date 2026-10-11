import os
import time
from datetime import datetime, timezone
import requests
import urllib.parse
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# Webhook configurado con fallback directo
DEFAULT_WEBHOOK = "https://discord.com/api/webhooks/1558357066597539867/Q2JWn4-2j_5oaJ-fGYZNetTXKCHrkr5G8XFzIlBMHnXRchcc1_tXDgjbcBEU0EMPlgam"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", DEFAULT_WEBHOOK).strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
}

# Descarte estricto de productos etiquetados en español o términos/Pokémon excluidos
PALABRAS_EXCLUIDAS = [
    "español", "espanol", "spanish",
    "venusaur", "charizard", "mimikyu", "garchomp", "bellibolt",
    "eevee", "chaos rising",
    "portafolio",
    "klara", "greninja"
]

PALABRAS_OBLIGATORIAS = ["pokemon", "pokémon", "tcg", "colección", "coleccion"]
PALABRAS_SECUNDARIAS = [
    "30", "30th", "30a", "30.º", "30º", 
    "celebracion", "celebración", "celebration", "anniversary", "aniversario", 
    "booster", "bundle", "trainer", "etb", "ultra", "premium", "ingles", "inglés", "en inglés",
    "ditto", "ditto premium collection", "blinder", "binder", "tech sticker", "sticker"
]

hallazgos = []

def coincide_palabras_clave(texto):
    if not texto:
        return False
    texto_lower = texto.lower()
    
    # Excluir productos en español, portafolios o con términos no deseados
    if any(exc in texto_lower for exc in PALABRAS_EXCLUIDAS):
        return False
    
    tiene_obligatoria = any(ob in texto_lower for ob in PALABRAS_OBLIGATORIAS)
    if not tiene_obligatoria:
        return False
    
    return any(sec in texto_lower for sec in PALABRAS_SECUNDARIAS)

def registrar_producto(tienda, titulo, url_directa, tiene_stock, precio="N/A"):
    if any(item['url'] == url_directa for item in hallazgos):
        return
    hallazgos.append({
        "tienda": tienda,
        "titulo": titulo,
        "url": url_directa,
        "stock": tiene_stock,
        "precio": precio
    })

def enviar_alerta_producto(item):
    """Notificación instantánea a Discord enviada para cada producto detectado (con o sin stock)"""
    if not WEBHOOK_URL:
        print("ERROR CRÍTICO: La URL del Webhook de Discord no se encuentra configurada.")
        return

    estado_txt = "🟢 CON STOCK DISPONIBLE" if item['stock'] else "🔴 SIN STOCK / AGOTADO"
    color_embed = 3066993 if item['stock'] else 15158332 # Verde o Rojo

    payload = {
        "content": f"📢 **PRODUCTO DETECTADO EN {item['tienda'].upper()}**",
        "embeds": [{
            "title": f"{item['titulo']}",
            "url": item['url'],
            "color": color_embed,
            "fields": [
                {"name": "Estado", "value": estado_txt, "inline": True},
                {"name": "Tienda", "value": item['tienda'], "inline": True},
                {"name": "Precio", "value": str(item['precio']), "inline": True}
            ]
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Alerta enviada para {item['tienda']} -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando alerta: {e}")

def enviar_reporte_programado(hora_label):
    """Reporte consolidado con todos los productos encontrados (con y sin stock)"""
    if not WEBHOOK_URL:
        print("ERROR CRÍTICO: La URL del Webhook de Discord no se encuentra configurada.")
        return

    if not hallazgos:
        descripcion = "No se encontraron publicaciones activas en inglés en las tiendas rastreadas."
    else:
        lineas = []
        for item in hallazgos:
            estado = "🟢 CON STOCK" if item['stock'] else "🔴 SIN STOCK"
            lineas.append(f"• **[{item['tienda']}]** [{item['titulo']}]({item['url']}) - **{estado}** (Precio: {item['precio']})")
        
        descripcion = "\n".join(lineas[:25]) 
        if len(lineas) > 25:
            descripcion += f"\n\n*...y {len(lineas) - 25} publicaciones más.*"

    payload = {
        "content": f"📊 **REPORTE DE ESTADO POKÉMON TCG 30TH ({hora_label} HORA PERÚ)**",
        "embeds": [{
            "title": "Consolidado de Tiendas Rastreadas (Inglés - Con y Sin Stock)",
            "description": descripcion,
            "color": 3447003,  # Azul
            "footer": {"text": f"Generado el {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"}
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Reporte de las {hora_label} enviado exitosamente a Discord -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando reporte programado: {e}")

# --- MÓDULOS DE RASTREO DE TIENDAS ---

def buscar_vtex(nombre, domain):
    print(f"\n--- [{nombre}] ---")
    queries = [
        "tcg 30 aniversario ingles",
        "30.º aniversario",
        "coleccion ultra premium tcg 30",
        "ditto premium collection"
    ]
    for q in queries:
        query_encoded = urllib.parse.quote(q)
        api_url = f"{domain}/api/catalog_system/pub/products/search?ft={query_encoded}&_from=0&_to=20"
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
            print(f"[{nombre}] Error en búsqueda '{q}': {e}")

    if "plazavea" in domain:
        skus_directos = [
            ("20711636", "Colección Ultra Premium TCG 30 Aniversario en Inglés", "https://www.plazavea.com.pe/coleccion-ultra-premium-tcg-30-aniversario-ingles-20711636/p")
        ]
        for prod_id, titulo_defecto, link_directo in skus_directos:
            try:
                api_sku = f"{domain}/api/catalog_system/pub/products/search?fq=productId:{prod_id}"
                res_sku = requests.get(api_sku, headers=HEADERS, timeout=10)
                if res_sku.status_code in [200, 206]:
                    prods = res_sku.json()
                    if isinstance(prods, list) and len(prods) > 0:
                        prod = prods[0]
                        nombre_p = prod.get("productName", titulo_defecto)
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
                            print(f"[{nombre} DIRECTO] {nombre_p} | Stock: {tiene_stock} | Precio: {precio}")
                            registrar_producto(nombre, nombre_p, link_directo, tiene_stock, precio)
            except Exception as e:
                print(f"[{nombre} DIRECTO] Error consultando ID {prod_id}: {e}")

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
    hora_actual = datetime.now(timezone.utc)
    print(f"Ejecutando rastreo: {hora_actual.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    
    # 1. Rastrear tiendas activas
    buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe")
    buscar_vtex("Oechsle", "https://www.oechsle.pe")
    buscar_tailoy()
    buscar_phantom()
    
    # 2. Mostrar/Alertar TODOS los productos encontrados en las tiendas
    if hallazgos:
        print(f"\n--- Resumen de Productos Encontrados ({len(hallazgos)}) ---")
        for item in hallazgos:
            print(f"[{item['tienda']}] {item['titulo']} | Stock: {item['stock']} | Precio: {item['precio']}")
            enviar_alerta_producto(item)
    else:
        print("No se encontró ningún producto coincidente.")

    # 3. Reportes consolidados programados (4 veces al día - Hora Perú UTC-5)
    if hora_actual.minute < 20:
        if hora_actual.hour == 13:      # 08:00 AM PET (13:00 UTC)
            enviar_reporte_programado("08:00 AM")
        elif hora_actual.hour == 17:    # 12:00 PM PET (17:00 UTC)
            enviar_reporte_programado("12:00 PM")
        elif hora_actual.hour == 21:    # 04:00 PM PET (21:00 UTC)
            enviar_reporte_programado("04:00 PM")
        elif hora_actual.hour == 1:     # 08:00 PM PET (01:00 UTC)
            enviar_reporte_programado("08:00 PM")

    print("Escaneo finalizado exitosamente.")
