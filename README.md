# Monitor Pokémon TCG 30 Aniversario (Inglés)

Bot que revisa tiendas peruanas (Ilahui, Plaza Vea, Oechsle, Tai Loy, Phantom)
y avisa por Discord cuando hay stock de productos del TCG 30 Aniversario en inglés.

## Archivos

| Archivo | Función |
|---|---|
| `bot.py` | Script principal (rastreo, filtros, alertas y reportes) |
| `requirements.txt` | Dependencias de Python |
| `.github/workflows/monitor-stock.yml` | Corre cada 5 min y manda alerta si aparece stock nuevo |
| `.github/workflows/reporte-programado.yml` | Reporte consolidado cada 3 h (8 al día) |

## Configuración

1. En el repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Nombre: `DISCORD_WEBHOOK`
   - Valor: la URL del webhook de tu canal de Discord.
2. En **Actions**, verifica que los workflows estén habilitados.
3. Prueba manual: **Actions → (workflow) → Run workflow**.

## Modos (`MODO_EJECUCION`)

- `SOLO_ALERTAS`: solo avisa cuando un producto pasa de sin stock a con stock.
- `FORZAR_REPORTE`: envía siempre el reporte consolidado, sin alertas.
- `REPORTE`: reporte solo a las 8, 13 y 20 h (Perú), una vez por hora.
- vacío: alertas + reporte en esas horas.

## Ejecución local

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
python bot.py
```

## Notas

- GitHub Actions puede retrasar los `cron` varios minutos.
- Si no hay actividad en el repo por 60 días, GitHub pausa los workflows programados.
- `state.json` (stock previo) se guarda con `actions/cache`; no se sube al repo.
