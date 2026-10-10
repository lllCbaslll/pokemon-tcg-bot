import os
import time
import requests
import urllib.parse
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# Lee el webhook guardado en los Secrets de GitHub Actions
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1558357066597539867/Q2JWn4-2j_5oaJ-fGYZNetTXKCHrkr5G8XFzIlBMHnXRchcc1_tXDgjbcBEU0EMPlgam")

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

def coincide_palabras_clave(texto):
    texto_lower = texto.lower()
    for conjunto in TARGET_KEYWORDS:
        if all(kw in texto_lower for kw in conjunto):
            return True
    return False

def enviar_alerta_discord(tienda, titulo, url_directa, tiene_stock, precio="N/A"):
    estado_texto = "🟢 DISPONIBLE CON STOCK" if tiene_stock else "🔴 PUBLICADO PERO SIN STOCK"
    color_embed = 3066993 if tiene_stock else 15158332

    fields = [
        {"name": "Estado", "value": estado_texto, "inline": True},
        {"name": "Tienda", "value": tienda, "inline": True}
    ]
    if precio != "N/A":
        fields.append({"name": "Precio", "value": str(precio), "inline": True})

    payload = {
        "content": f"📢 @everyone ¡Notificación de Rastreo Automatizado 24/7 en **{tienda.upper()}**!",
        "embeds": [{
            "title": f"🔗 {titulo}",
            "description": "Enlace directo al producto:",
            "url": url_directa,
            "color": color_embed,
            "fields": fields
        }]
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Error al enviar a Discord: {e}")

def buscar_mercadolibre():
    print("\n--- [Mercado Libre Perú] ---")
    url_prod = "https://www.mercadolibre.com.pe/cartas-pokemon-30-aniversarios-tcg-booster-bundle-ingles/up/MPEU5458163652"
    try:
        scraper = cloudscraper.create_scraper() if cloudscraper else requests
        res = scraper.get(url_prod, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            titulo_el = soup.select_one("h1.ui-pdp-title")
            titulo = titulo_el.get_text(strip=True) if titulo_el else "Cartas Pokemon 30 Aniversarios TCG Booster Bundle Inglés"
            no_stock = "publicación pausada" in res.text.lower() or "sin stock" in res.text.lower()
            precio_el = soup.select_one(".ui-pdp-price__second-line .andes-money-amount__fraction")
            precio = f"S/ {precio_el.get_text(strip=True)}" if precio_el else "N/A"
            enviar_alerta_discord("Mercado Libre Perú", titulo, url_prod, not no_stock, precio)
    except Exception as e:
        print(f"Error ML: {e}")

def buscar_vtex(nombre, domain):
    print(f"\n--- [{nombre}] ---")
    query = urllib.parse.quote("tcg 30 aniversario ingles")
    api_url = f"{domain}/api/catalog_system/pub/products/search?ft={query}&_from=0&_to=15"
    try:
        res = requests.get(api_url, headers=HEADERS, timeout=10)
        if res.status_code in [200, 206]:
            for prod in res.json():
                nombre_p = prod.get("productName", "")
                if coincide_palabras_clave(nombre_p):
                    tiene_stock = any(
                        comm.get("AvailableQuantity", 0) > 0 and comm.get("IsAvailable", False)
                        for item in prod.get("items", [])
                        for seller in item.get("sellers", [])
                        for comm in [seller.get("commertialOffer", {})]
                    )
                    enviar_alerta_discord(nombre, nombre_p, prod.get("link", domain), tiene_stock)
    except Exception as e:
        print(f"Error VTEX {nombre}: {e}")

if __name__ == "__main__":
    hora = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"Iniciando escaneo único a las {hora}")
    
    buscar_mercadolibre()
    buscar_vtex("Plaza Vea", "https://www.plazavea.com.pe")
    buscar_vtex("Oechsle", "https://www.oechsle.pe")
    
    # Notificación de vida útil en Discord para confirmar el flujo
    requests.post(WEBHOOK_URL, json={
        "content": f"🤖 **Rastreador GitHub Actions:** Escaneo ejecutado con éxito a las `{hora}` (Lima). Sistema monitoreando activa y automáticamente."
    })
    
    print("Escaneo finalizado.")
