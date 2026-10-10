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
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
}

TARGET_KEYWORDS = [
    ["30", "booster", "bundle"],
    ["30", "ultra", "premium"],
    ["tcg", "30", "ingles"],
    ["cartas", "pokemon", "30"]
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
            "color": 3066993, # Verde
            "fields": [
                {"name": "Estado", "value": "🟢 CON STOCK DISPONIBLE", "inline": True},
                {"name": "Tienda", "value": item['tienda'], "inline": True},
                {"name": "Precio", "value": str(item['precio']), "inline": True}
            ]
        }]
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Error enviando alerta instantánea: {e}")

def enviar_reporte_diario():
    """Reporte consolidado diario con todos los links encontrados (con o sin stock)"""
    if not hallazgos:
        descripcion = "No se encontraron publicaciones activas coincidentes en las tiendas rastreadas hoy."
    else:
        lineas = []
        for item in hallazgos:
            estado = "🟢 CON STOCK" if item['stock'] else "🔴 SIN STOCK"
            lineas.append(f"• [{item['tienda']}] [{item['titulo']}]({item['url']}) - **{estado}**")
        descripcion = "\n".join(lineas)

    payload = {
        "content": "📊 **REPORTE DIARIO DE MONITOREO (13:00 HORA PERÚ)**",
        "embeds": [{
            "title": "Estado General de Productos Pokémon TCG 30th",
            "description": descripcion,
            "color": 3447003, # Azul
            "footer": {"text": f"Generado el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"}
        }]
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print("Reporte diario enviado exitosamente a Discord.")
    except Exception as e:
        print(f"Error enviando reporte diario: {e}")

# --- MÓDULOS DE RASTREO DE TIENDAS ---

def buscar_vtex(nombre, domain):
    print(f"\n--- [{nombre}] ---")
    query = urllib.parse.quote("tcg 30 aniversario ingles")
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
                        for item in prod.get("items", []):
                            for seller in item.get("sellers", []):
                                comm = seller.get("commertialOffer", {})
                                if comm.get("AvailableQuantity", 0) > 0 and comm.get("IsAvailable", False):
                                    tiene_stock = True
                                    break
                        link_prod = prod.get("link", domain)
                        print(f"[{nombre}] {nombre_p} | Stock: {tiene_stock}")
                        registrar_producto(nombre, nombre_p, link_prod, tiene_stock)
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
                if nombre_el:
                    nombre = nombre_el.get_text(strip=True)
                    link_directo = nombre_el.get("href", "")
                    if coincide_palabras_clave(nombre):
                        tiene_stock = "sin stock" not in prod.text.lower() and "agotado" not in prod.text.lower()
                        print(f"[Tai Loy] {nombre} | Stock: {tiene_stock}")
                        registrar_producto("Tai Loy", nombre, link_directo, tiene_stock)
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
                if titulo_el and link_el:
                    titulo = titulo_el.get_text(strip=True)
                    link_directo = link_el.get("href", "")
                    if coincide_palabras_clave(titulo):
                        tiene_stock = "agotado" not in item.text.lower()
                        print(f"[Phantom] {titulo} | Stock: {tiene_stock}")
                        registrar_producto("Phantom", titulo, link_directo, tiene_stock)
    except Exception as e:
        print(f"[Phantom] Error: {e}")

if __name__ == "__main__":
    hora_actual = datetime.now()
    print(f"Ejecutando rastreo: {hora_actual.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    
    # 1. Rastrear tiendas activas
    buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe")
    buscar_vtex("Oechsle", "https://www.oechsle.pe")
    buscar_tailoy()
    buscar_phantom()
    
    # 2. Monitoreo continuo (cada 5 min): SOLO notifica si se detecta stock disponible
    for item in hallazgos:
        if item["stock"]:
            print(f"¡Atención! Stock encontrado en {item['tienda']}. Enviando alerta...")
            enviar_alerta_stock_inmediato(item)

    # 3. Reporte consolidado diario a las 13:00 (Hora Perú / 18:00 UTC)
    if hora_actual.hour == 18 and hora_actual.minute < 5:
        print("Enviando reporte diario consolidado a las 13:00 (Hora Perú)...")
        enviar_reporte_diario()

    print("Escaneo finalizado.")
