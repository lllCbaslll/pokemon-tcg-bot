import os
import re
import sys
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup

WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK",
    "https://discord.com/api/webhooks/1558357066597539867/Q2JWn4-2j_5oaJ-fGYZNetTXKCHrkr5G8XFzIlBMHnXRchcc1_tXDgjbcBEU0EMPlgam"
).strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache"
}

PRODUCTOS_AMAZON = [
    {"titulo": "Pokémon TCG: Ultra-Premium Collection — Night", "asin": "B0HAQTE9X", "url": "https://www.amazon.com.mx/dp/B0HAQTE9X"},
    {"titulo": "Pokémon TCG: Ultra-Premium Collection — Day", "asin": "B02C6WYHN", "url": "https://www.amazon.com.mx/dp/B02C6WYHN"},
    {"titulo": "Pokémon TCG: Ultra-Premium Collection (Aleatorio)", "asin": "B08VFJSTL", "url": "https://www.amazon.com.mx/dp/B08VFJSTL"},
    {"titulo": "Pokémon TCG: Ultra-Premium Collection — Night (Alt)", "asin": "B0IZB1IJ9", "url": "https://www.amazon.com.mx/dp/B0IZB1IJ9"},
    {"titulo": "Pokémon TCG: Ultra-Premium Collection Special", "asin": "B0H77XNKKK", "url": "https://www.amazon.com.mx/dp/B0H77XNKKK"}
]

KEYWORDS_BUSQUEDA = ["pokemon ultra premium collection", "pokemon tcg 30th ultra premium"]

# Exclusión amplia de productos no deseados
KEYWORDS_EXCLUIDAS = [
    "charizard", "moltres", "celebrations", 
    "fundas", "terapagos", "alfombra"
]

PRECIO_MIN = 2000.0
PRECIO_MAX = 4000.0

hallazgos = []

def limpiar_precio(texto_precio):
    if not texto_precio:
        return 0.0
    m = re.search(r'[\d,]+(?:\.\d+)?', texto_precio.replace('$', '').replace('MXN', ''))
    if m:
        try:
            return float(m.group(0).replace(',', ''))
        except ValueError:
            return 0.0
    return 0.0

def generar_link_compra_directa(asin):
    return f"https://www.amazon.com.mx/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"

def es_producto_excluido(titulo):
    if not titulo:
        return False
    titulo_lower = titulo.lower()
    return any(ex in titulo_lower for ex in KEYWORDS_EXCLUIDAS)

def registrar_hallazgo(asin, titulo, url, precio_num, tiene_stock, es_amazon):
    if es_producto_excluido(titulo):
        print(f"   [Excluido por Filtro] {titulo[:40]}")
        return

    for h in hallazgos:
        if h["asin"] == asin:
            return

    en_rango_precio = PRECIO_MIN <= precio_num <= PRECIO_MAX
    cumple_condiciones = tiene_stock and es_amazon and en_rango_precio
    precio_str = f"MXN ${precio_num:.2f}" if precio_num > 0 else "Consultar Web"

    hallazgos.append({
        "asin": asin,
        "titulo": titulo,
        "url": url,
        "precio": precio_str,
        "precio_num": precio_num,
        "stock": tiene_stock,
        "es_amazon": es_amazon,
        "cumple_autocompra": cumple_condiciones,
        "link_buy": generar_link_compra_directa(asin)
    })

def consultar_amazon_asin(prod, session):
    url = prod["url"]
    asin = prod["asin"]
    titulo_base = prod["titulo"]
    
    print(f"-> Escaneando ASIN {asin}...")
    
    try:
        res = session.get(url, headers=HEADERS, timeout=4)
        texto_html = res.text.lower()

        if res.status_code != 200 or "no pudimos encontrar" in texto_html or "lo sentimos" in texto_html:
            print(f"   [Página No Encontrada / 404] {asin}")
            registrar_hallazgo(asin, titulo_base, url, 0.0, False, False)
            return

        soup = BeautifulSoup(res.text, "html.parser")
        
        title_el = soup.select_one("#productTitle")
        titulo = title_el.get_text(strip=True) if title_el else titulo_base
        
        if es_producto_excluido(titulo):
            print(f"   [Excluido por Keyword Prohibida] {asin}")
            return

        # Verificación de Stock Estricta
        sin_stock_phrases = ["no disponible", "no tenemos información", "temporalmente agotado", "agotado", "out of stock"]
        out_of_stock_el = soup.select_one("#outOfStock, #availability, .a-color-price")
        availability_text = out_of_stock_el.get_text().lower() if out_of_stock_el else ""
        
        tiene_boton_compra = bool(soup.select_one("#add-to-cart-button, #buy-now-button, input[name='submit.add-to-cart']"))
        es_sin_stock = any(phrase in availability_text or phrase in texto_html for phrase in sin_stock_phrases)
        tiene_stock = tiene_boton_compra and not es_sin_stock

        # Precio
        price_el = soup.select_one(".a-price .a-offscreen, #priceblock_ourprice, #priceblock_dealprice, .a-price-whole")
        precio_raw = price_el.get_text(strip=True) if price_el else "$0"
        precio_num = limpiar_precio(precio_raw)
        
        seller_el = soup.select_one("#merchant-info, #tabular-buybox")
        seller_text = seller_el.get_text().lower() if seller_el else ""
        
        es_vendido_por_amazon = ("amazon" in seller_text or "vendido por amazon" in seller_text) and "ghost seller" not in seller_text

        registrar_hallazgo(asin, titulo, url, precio_num, tiene_stock, es_vendido_por_amazon)
        print(f"   [Éxito ASIN] {asin} | Stock Real: {tiene_stock} | Vendido por Amazon: {es_vendido_por_amazon} | Precio: MXN ${precio_num}")

    except Exception as e:
        print(f"   [Error ASIN] {asin}: {e}")

def buscar_por_palabras_clave(kw, session):
    print(f"\n-> Buscando por Palabra Clave: '{kw}'...")
    url_search = f"https://www.amazon.com.mx/s?k={urllib.parse.quote(kw)}"
    try:
        res = session.get(url_search, headers=HEADERS, timeout=4)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            items = soup.select("div[data-asin]")
            for item in items:
                asin = item.get("data-asin", "").strip()
                if not asin:
                    continue
                
                title_el = item.select_one("h2 a span, .a-text-normal")
                titulo = title_el.get_text(strip=True) if title_el else ""
                
                if es_producto_excluido(titulo):
                    continue

                if "ultra" in titulo.lower() and "premium" in titulo.lower():
                    price_el = item.select_one(".a-price .a-offscreen, .a-price-whole")
                    precio_raw = price_el.get_text(strip=True) if price_el else "$0"
                    precio_num = limpiar_precio(precio_raw)
                    url_prod = f"https://www.amazon.com.mx/dp/{asin}"
                    
                    registrar_hallazgo(asin, titulo, url_prod, precio_num, True, True)
                    print(f"   [Hallazgo Keyword] ASIN: {asin} | {titulo[:40]}...")
    except Exception as e:
        print(f"   [Error Keyword] '{kw}': {e}")

def enviar_alerta_compra_inmediata(item):
    payload = {
        "content": f"🚨 **¡STOCK DETECTADO (VENDIDO POR AMAZON)!** @everyone",
        "embeds": [{
            "title": f"🟢 {item['titulo']}",
            "url": item['url'],
            "color": 3066993,
            "fields": [
                {"name": "Estado", "value": "🟢 CON STOCK DISPONIBLE", "inline": True},
                {"name": "Precio", "value": str(item['precio']), "inline": True},
                {"name": "Vendedor", "value": "Amazon (Vendido y Enviado)", "inline": True},
                {"name": "ASIN", "value": item['asin'], "inline": True},
                {"name": "⚡ ENLACE DE COMPRA DIRECTA", "value": f"[🛒 COMPRAR AHORA EN 1-CLICK]({item['link_buy']})", "inline": False}
            ],
            "footer": {"text": "Monitoreo Amazon.mx 24/7"}
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=5)
        print(f"Alerta enviada a Discord -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando alerta: {e}")

def enviar_reporte_diario(hora_label):
    if not hallazgos:
        desc = "No se encontraron productos coincidentes en esta ronda."
    else:
        lineas = []
        for item in hallazgos:
            st = "🟢 CON STOCK" if item['stock'] else "🔴 SIN STOCK"
            vendedor = "Amazon" if item['es_amazon'] else "Tercero"
            lineas.append(f"• [{item['asin']}] [{item['titulo']}]({item['url']}) - **{st}** ({item['precio']} | {vendedor})")
        desc = "\n".join(lineas)

    payload = {
        "content": f"📊 **REPORTE DE ESTADO AMAZON.MX ({hora_label})**",
        "embeds": [{
            "title": "Consolidado de Productos Monitoreados",
            "description": desc,
            "color": 3447003,
            "footer": {"text": f"Generado el {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"}
        }]
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=5)
        print(f"Reporte ({hora_label}) enviado a Discord -> HTTP {res.status_code}")
    except Exception as e:
        print(f"Error enviando reporte: {e}")

if __name__ == "__main__":
    hora_utc = datetime.now()
    print(f"Iniciando escaneo Amazon.mx: {hora_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    session = requests.Session()

    for prod in PRODUCTOS_AMAZON:
        consultar_amazon_asin(prod, session)

    for kw in KEYWORDS_BUSQUEDA:
        buscar_por_palabras_clave(kw, session)

    alerta_enviada = False
    for item in hallazgos:
        if item["cumple_autocompra"]:
            alerta_enviada = True
            enviar_alerta_compra_inmediata(item)

    if not alerta_enviada:
        print("Sin stock vendido por Amazon dentro del rango $2,000-$4,000 MXN.")

    mapa_cortes_utc = {
        5: "12:00 AM PET",
        9: "04:00 AM PET",
        13: "08:00 AM PET",
        17: "12:00 PM PET",
        21: "04:00 PM PET",
        1: "08:00 PM PET"
    }

    es_ejecucion_manual = "--manual" in sys.argv or len(sys.argv) > 1

    if (hora_utc.hour in mapa_cortes_utc and hora_utc.minute < 10) or es_ejecucion_manual:
        label = mapa_cortes_utc.get(hora_utc.hour, f"{hora_utc.strftime('%H:%M')} UTC (Ejecución Manual)")
        enviar_reporte_diario(label)

    print("Escaneo finalizado.")
