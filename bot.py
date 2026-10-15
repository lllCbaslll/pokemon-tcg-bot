import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import urllib.parse
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# Configuración de zona horaria local (Perú)
ZONA_HORARIA_LOCAL = ZoneInfo("America/Lima")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
}

# Palabras clave ampliadas para cubrir productos TCG 30 Aniversario en Inglés
TARGET_KEYWORDS = [
    ["30", "booster"], ["30", "bundle"], ["30", "ultra"], ["30", "premium"],
    ["30", "binder"], ["30", "tin"], ["30", "suelta"], ["30", "aniversario"],
    ["30", "pack"], ["30", "caja"], ["30", "sobre"], ["30", "álbum"], ["30", "album"],
    ["30", "poster"], ["30", "entrenador"], ["30", "élite"], ["30", "elite"],
    ["30", "ex"], ["30", "pokeball"], ["30", "colección"], ["30", "coleccion"],
    ["tcg", "30", "ingles"], ["cartas", "pokemon", "30"]
]

# Exclusiones estrictas
EXCLUDE_KEYWORDS = [
    "español", "espanol", "spa", "peluche", "ron", "sorpresa", "brillante",
    "ps5", "pokeball", "buzz", "pack"
]

hallazgos = []

def generar_link_carrito(tienda, sku, url_directa, domain=""):
    """Genera una URL directa de añadir al carrito basada en la plataforma y SKU"""
    if not sku or sku == "N/A":
        return url_directa
        
    tienda_lower = tienda.lower()
    
    # Shopify (Ejemplo: Ilahui)
    if "ilahui" in tienda_lower or "shopify" in tienda_lower:
        return f"https://ilahuiperu.com/cart/add?id={sku}&quantity=1"
        
    # VTEX (Ejemplo: Plaza Vea, Oechsle)
    elif "plaza vea" in tienda_lower or "oechsle" in tienda_lower or domain:
        base_domain = domain if domain else "https://" + urllib.parse.urlparse(url_directa).netloc
        return f"{base_domain}/checkout/cart/add?sku={sku}&qty=1&seller=1"
        
    # WooCommerce / Magento (Ejemplo: Phantom, Tai Loy)
    elif "phantom" in tienda_lower:
        return f"{url_directa}?add-to-cart={sku}"
        
    return url_directa

def es_producto_valido(texto, url=""):
    """Filtra productos que coincidan con las keywords y no estén en la lista de exclusión"""
    if not texto:
        return False
    
    texto_lower = f"{texto} {url}".lower()
    
    # 1. Exclusión de palabras prohibidas
    if any(ex in texto_lower for ex in EXCLUDE_KEYWORDS):
        return False
        
    # 2. Si el texto o URL indica explícitamente "ingles", "inglés" o "english", se valida directamente si menciona 30
    if ("30" in texto_lower or "30°" in texto_lower) and any(lang in texto_lower for lang in ["ingles", "inglés", "english"]):
        return True

    # 3. Validación por conjuntos de palabras clave objetivo
    for conjunto in TARGET_KEYWORDS:
        if all(kw in texto_lower for kw in conjunto):
            return True
            
    return False

def registrar_producto(tienda, titulo, url_directa, tiene_stock, precio="N/A", cart_url=None, sku="N/A", domain=""):
    # Evitar duplicados por URL
    for h in hallazgos:
        if h["url"] == url_directa:
            return

    # Si no nos pasan un cart_url precalculado, lo generamos dinámicamente con el SKU
    final_cart_url = cart_url or generar_link_carrito(tienda, sku, url_directa, domain)

    hallazgos.append({
        "tienda": tienda,
        "titulo": titulo,
        "url": url_directa,
        "stock": tiene_stock,
        "precio": precio,
        "cart_url": final_cart_url,
        "sku": sku
    })

def enviar_alerta_stock_inmediato(item):
    """Notificación instantánea cuando hay stock"""
    payload = {
        "content": f"🚨 **¡STOCK EN INGLÉS DETECTADO EN {item['tienda'].upper()}!** @everyone",
        "embeds": [{
            "title": f"🟢 {item['titulo']}",
            "url": item['url'],
            "color": 3066993,  # Verde
            "fields": [
                {"name": "Estado", "value": "🟢 CON STOCK DISPONIBLE", "inline": True},
                {"name": "Tienda", "value": item['tienda'], "inline": True},
                {"name": "Precio", "value": str(item['precio']), "inline": True},
                {"name": "SKU / Variant ID", "value": str(item['sku']), "inline": True},
                {"name": "🛒 Comprar Directo (SKU)", "value": f"[Añadir al Carrito con SKU {item['sku']}]({item['cart_url']})", "inline": False}
            ]
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Alerta enviada para {item['tienda']} -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando alerta instantánea: {e}")

def enviar_reporte_programado(hora_label):
    """Reporte consolidado programado con enlace directo al carrito vía SKU"""
    if not hallazgos:
        descripcion = "No se encontraron publicaciones activas coincidentes en inglés hoy."
    else:
        lineas = []
        for item in hallazgos:
            estado = "🟢 CON STOCK" if item['stock'] else "🔴 SIN STOCK"
            sku_info = f" (SKU: {item['sku']})" if item['sku'] != "N/A" else ""
            lineas.append(
                f"• **[{item['tienda']}]** [{item['titulo']}]({item['url']}) - **{estado}** | Precio: {item['precio']}\n"
                f"  └ 🛒 [Añadir Directo al Carrito{sku_info}]({item['cart_url']})"
            )
        descripcion = "\n".join(lineas)

    payload = {
        "content": f"📊 **REPORTE CONSOLIDADO POKÉMON TCG 30TH ({hora_label} PET)**",
        "embeds": [{
            "title": "Consolidado de Tiendas Rastreadas",
            "description": descripcion,
            "color": 3447003,  # Azul
            "footer": {"text": f"Generado el {datetime.now(ZONA_HORARIA_LOCAL).strftime('%Y-%m-%d %H:%M:%S')} PET"}
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"Reporte de las {hora_label} enviado a Discord -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando reporte programado: {e}")

# --- MÓDULOS DE RASTREO ---

def buscar_ilahui():
    print("\n--- [Ilahui Perú (Shopify)] ---")
    urls_referencia = [
        "https://ilahuiperu.com/products/pokemon-tcg-30-aniversario-binder-collection-en-ingles"
    ]
    
    for url in urls_referencia:
        try:
            res_js = requests.get(f"{url}.js", headers=HEADERS, timeout=10)
            if res_js.status_code == 200:
                p_data = res_js.json()
                titulo = p_data.get("title", "")
                
                if es_producto_valido(titulo, url):
                    precio = f"S/ {p_data.get('price', 0) / 100:.2f}"
                    variants = p_data.get("variants", [])
                    
                    variant_id = str(variants[0].get("id")) if variants else "N/A"
                    tiene_stock = any(v.get("available", False) for v in variants) if variants else p_data.get("available", False)
                    
                    print(f"[Ilahui] {titulo} | Stock: {tiene_stock} | SKU/Variant: {variant_id}")
                    registrar_producto("Ilahui", titulo, url, tiene_stock, precio, sku=variant_id)
        except Exception as e:
            print(f"[Ilahui Warning]: {e}")

def buscar_vtex(nombre, domain):
    print(f"\n--- [{nombre} (VTEX)] ---")
    terminos = ["30 aniversario", "tcg 30"]
    for term in terminos:
        query = urllib.parse.quote(term)
        api_url = f"{domain}/api/catalog_system/pub/products/search?ft={query}&_from=0&_to=30"
        try:
            res = requests.get(api_url, headers=HEADERS, timeout=10)
            if res.status_code in [200, 206]:
                productos = res.json()
                if isinstance(productos, list):
                    for prod in productos:
                        nombre_p = prod.get("productName", "")
                        link_prod = prod.get("link", domain)
                        
                        if es_producto_valido(nombre_p, link_prod):
                            tiene_stock = False
                            precio = "N/A"
                            sku_id = "N/A"
                            
                            items = prod.get("items", [])
                            if items:
                                sku_id = str(items[0].get("itemId", "N/A"))
                                for item in items:
                                    for seller in item.get("sellers", []):
                                        comm = seller.get("commertialOffer", {})
                                        if comm.get("AvailableQuantity", 0) > 0 and comm.get("IsAvailable", False):
                                            tiene_stock = True
                                            sku_id = str(item.get("itemId"))
                                            if "Price" in comm:
                                                precio = f"S/ {comm['Price']}"
                                            break
                            
                            print(f"[{nombre}] {nombre_p} | Stock: {tiene_stock} | SKU: {sku_id}")
                            registrar_producto(nombre, nombre_p, link_prod, tiene_stock, precio, sku=sku_id, domain=domain)
        except Exception as e:
            print(f"[{nombre}] Error con consulta '{term}': {e}")

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
                sku_el = prod.select_one("[data-product-id]")
                
                if nombre_el:
                    nombre = nombre_el.get_text(strip=True)
                    link_directo = nombre_el.get("href", "")
                    precio = precio_el.get_text(strip=True) if precio_el else "N/A"
                    sku_id = sku_el.get("data-product-id", "N/A") if sku_el else "N/A"
                    
                    if es_producto_valido(nombre, link_directo):
                        tiene_stock = "sin stock" not in prod.text.lower() and "agotado" not in prod.text.lower()
                        print(f"[Tai Loy] {nombre} | Stock: {tiene_stock} | SKU: {sku_id}")
                        registrar_producto("Tai Loy", nombre, link_directo, tiene_stock, precio, sku=sku_id)
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
                add_to_cart_el = item.select_one("[data-product_id]")
                
                if titulo_el and link_el:
                    titulo = titulo_el.get_text(strip=True)
                    link_directo = link_el.get("href", "")
                    precio = precio_el.get_text(strip=True) if precio_el else "N/A"
                    sku_id = add_to_cart_el.get("data-product_id", "N/A") if add_to_cart_el else "N/A"
                    
                    if es_producto_valido(titulo, link_directo):
                        tiene_stock = "agotado" not in item.text.lower()
                        print(f"[Phantom] {titulo} | Stock: {tiene_stock} | SKU: {sku_id}")
                        registrar_producto("Phantom", titulo, link_directo, tiene_stock, precio, sku=sku_id)
    except Exception as e:
        print(f"[Phantom] Error: {e}")

if __name__ == "__main__":
    hora_actual = datetime.now(ZONA_HORARIA_LOCAL)
    print(f"Ejecutando rastreo Pokémon (Inglés): {hora_actual.strftime('%Y-%m-%d %H:%M:%S')} {hora_actual.tzname()}")
    
    # 1. Rastrear tiendas
    buscar_ilahui()
    buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe")
    buscar_vtex("Oechsle", "https://www.oechsle.pe")
    buscar_tailoy()
    buscar_phantom()
    
    # 2. Alerta instantánea en Discord (solo si hay stock)
    stock_encontrado = False
    for item in hallazgos:
        if item["stock"]:
            stock_encontrado = True
            print(f"¡Atención! Stock en inglés encontrado en {item['tienda']}. Enviando alerta...")
            enviar_alerta_stock_inmediato(item)

    if not stock_encontrado:
        print("Sin stock disponible para productos válidos. Finalizado en silencio.")

    # 3. Reportes consolidados (04:00 AM, 08:00 AM, 12:00 PM, 04:00 PM, 08:00 PM y 12:00 AM Hora Perú / PET)
    if hora_actual.hour == 4 and hora_actual.minute < 5:
        enviar_reporte_programado("04:00 AM")
    elif hora_actual.hour == 8 and hora_actual.minute < 5:
        enviar_reporte_programado("08:00 AM")
    elif hora_actual.hour == 12 and hora_actual.minute < 5:
        enviar_reporte_programado("12:00 PM")
    elif hora_actual.hour == 16 and hora_actual.minute < 5:
        enviar_reporte_programado("04:00 PM")
    elif hora_actual.hour == 20 and hora_actual.minute < 5:
        enviar_reporte_programado("08:00 PM")
    elif hora_actual.hour == 0 and hora_actual.minute < 5:
        enviar_reporte_programado("12:00 AM")

    print("Escaneo finalizado.")
