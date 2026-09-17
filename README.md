# Shopstar · Bombas Millas Benefit

Proyecto reproducible para extraer el catálogo disponible en **https://www.shopstar.pe/bombas-millas**, guardar los datos en JSON y ordenar los productos por la valorización obtenida al usar Millas Benefit.

## Métrica principal

El dashboard usa:

```text
S/ por milla = precio vigente en soles / millas Benefit requeridas
S/ por 1,000 millas = (precio vigente en soles / millas Benefit requeridas) × 1,000
```

**Un valor mayor es mejor**: significa que cada milla está cubriendo más valor en soles.

Ejemplo: un producto de S/ 400 que cuesta 5,000 millas entrega `S/ 80 por 1,000 millas`; otro de S/ 400 que cuesta 8,000 millas entrega `S/ 50 por 1,000 millas`. El primero valoriza mejor las millas.

> Shopstar puede usar un factor de conversión dinámico y campañas especiales. Por eso el scraper toma el precio y las millas mostradas en el momento de cada ejecución en vez de asumir una equivalencia fija.

## Qué guarda

`data/latest.json` contiene la última extracción completa. Para cada producto se intenta conservar:

- ID de producto, SKU y EAN.
- Nombre, marca, categorías y tags de campaña.
- Seller y seller ID.
- URL e imagen.
- Precio vigente y precio de lista en soles.
- Millas Benefit actuales y, cuando aparece, valor de millas de referencia/anterior.
- Stock/disponibilidad.
- Ratios calculados: S/ por milla, céntimos por milla, S/ por 1,000 millas y millas por S/1.
- Texto observado en la tarjeta/ficha.
- Objeto de producto original recuperado de las respuestas de catálogo de VTEX (`source_product`) para no perder campos útiles.

`data/history.json` mantiene snapshots compactos de las últimas 90 ejecuciones para poder analizar cambios de precio/millas en el tiempo.

## Cómo funciona el scraper

El scraper usa Playwright porque la landing de Shopstar es dinámica. Combina tres estrategias:

1. Recorre scroll infinito y botones tipo **Mostrar más / Ver más / Cargar más** hasta que el catálogo deja de crecer.
2. Captura los productos que aparecen en el DOM y extrae valores visibles de soles y Millas Benefit.
3. Intercepta respuestas JSON de búsqueda/catálogo de VTEX para enriquecer cada producto con precio, SKU, seller, stock, imágenes, categorías y el payload original. Si una tarjeta no expone las millas, visita la ficha del producto como respaldo.

## Ejecución local

Requiere Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python -m unittest discover -s tests -v
python scraper/shopstar_bombas_millas.py
```

Para abrir el dashboard:

```bash
python -m http.server 8000
```

Luego abre `http://localhost:8000`.

## GitHub Actions

`.github/workflows/scrape.yml` ejecuta el scraper:

- al modificar el scraper/workflow;
- manualmente con **Run workflow**;
- todos los días a las 08:15 hora de Lima (13:15 UTC).

Cuando la extracción termina, el workflow actualiza `data/latest.json` y `data/history.json` y hace commit automático al repositorio.

## Dashboard

`index.html` es estático, sin framework ni backend. Permite:

- ver top productos por valorización de millas;
- buscar por producto, marca o seller;
- filtrar disponibilidad;
- ordenar cualquier columna;
- comparar precio, millas, S/ por 1,000 millas, céntimos por milla y millas por S/1.

También puede publicarse con GitHub Pages sirviendo la raíz de la rama `main`.

## Consideraciones

- Los datos reflejan lo que Shopstar publica al momento del scraping y pueden cambiar sin previo aviso.
- El ratio no incorpora costo de envío.
- Si Shopstar cambia el HTML o sus endpoints, el payload crudo y la estrategia dual DOM + red facilitan ajustar el scraper sin rediseñar el proyecto.
