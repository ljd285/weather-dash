# Dashboard climatológico con AEMET OpenData

Dashboard HTML que muestra, para una estación (por defecto Valencia):

- **Avisos**: avisos meteorológicos de AEMET (Meteoalerta) en vigor o próximos para la zona de la estación.
- **Ahora**: última observación de la estación (temperatura, viento, humedad, precipitación).
- **Predicción**: temperatura, probabilidad de precipitación, viento y humedad a 7 días.
- **Histórico**: temperatura (sobre la banda de valores normales), precipitación, viento y humedad de los últimos 90 días.
- **Comparación con lo normal**: el último mes completo frente a los valores normales de la estación (el periodo de referencia se lee de los metadatos de AEMET), clasificado como hace AEMET (frío/normal/cálido, seco/normal/húmedo…).
- **Días señalados** del último mes completo (días de calor, noches tropicales y tórridas, días de lluvia, rachas fuertes…) junto a lo normal.
- **Viento**: dirección y racha en la observación actual, y rosa de los vientos con la dirección de la racha máxima de cada día.
- **Año hidrológico**: lluvia acumulada desde el 1 de octubre frente a la acumulada normal a la misma fecha.
- **Lluvia intensa**: intensidad máxima diaria (mm/h) con los umbrales de AEMET de lluvia fuerte, muy fuerte y torrencial.
- **Presión y horas de sol**: evolución diaria frente a sus valores normales.
- **Confort**: sensación térmica y punto de rocío en la observación actual, sensación térmica e índice UV (categorías de la OMS) en la predicción.
- **Próximas 48 horas**: temperatura y sensación, precipitación, probabilidad de lluvia y de tormenta, y viento hora a hora, con la noche sombreada.
- **Récords de la estación** para el mes en curso (máxima, mínima y lluvia en un día), y avisos cuando un día reciente o previsto los supera o se queda cerca.
- **Mar**: temperatura del agua y oleaje (altura significativa, periodo y dirección) medidos por la **Boya de Valencia de Puertos del Estado**, con gráficos de las últimas 72 horas. Si la boya no da temperatura, se usa la del modelo de Copernicus (Open-Meteo). Se configura con los campos `boya` y `mar` de `config.py`.
- **Días provisionales**: los últimos días, que AEMET aún no ha validado, se calculan con las observaciones horarias y se muestran con línea de puntos.
- **Descarga de datos** en CSV (punto y coma y coma decimal, para Excel en español) y **enlace directo a cada estación** (`…/#valencia-aeropuerto`).

Todo se ejecuta en la nube (GitHub Actions), sin instalar nada en tu ordenador.
Se puede prototipar antes en Google Colab si quieres ver los datos sin montar aún la automatización.

## 0. Requisito: clave de AEMET OpenData

1. Ve a <https://opendata.aemet.es/centrodedescargas/altaUsuario> y pide una clave (API key) gratuita con tu email.
2. Te llegará un enlace por correo para activarla. El resultado es un token largo tipo JWT: guárdalo, lo necesitarás en el paso 3.

## 1. Probar los datos en Google Colab (opcional, recomendado la primera vez)

No hace falta instalar nada, solo un navegador:

1. Abre <https://colab.research.google.com> y crea un notebook nuevo.
2. En la primera celda:
   ```python
   !pip install requests pandas plotly -q
   ```
3. Sube (o pega el contenido de) `config.py` y `fetch_weather_dash.py` a los archivos del notebook (icono de carpeta a la izquierda → subir), o copia el código de `fetch_weather_dash.py` directamente en celdas.
4. En otra celda, define tu clave (mejor con un "Secret" de Colab — icono de llave a la izquierda — que como texto plano):
   ```python
   import os
   os.environ["AEMET_API_KEY"] = "tu_clave_aqui"
   ```
5. Ejecuta:
   ```python
   !python fetch_weather_dash.py
   ```
6. Para verlo dentro del propio notebook:
   ```python
   from IPython.display import IFrame
   IFrame("docs/index.html", width=900, height=700)
   ```

Esto te sirve para comprobar que la clave funciona y que los datos de Valencia se descargan bien, antes de automatizar nada.

## 2. Subir el proyecto a GitHub

1. Crea un repositorio nuevo en GitHub (público o privado, ambos funcionan con GitHub Pages, aunque con un repo privado necesitas GitHub Pro/Team/Enterprise para publicar Pages).
2. Sube todos los archivos de esta carpeta (`config.py`, `fetch_weather_dash.py`, `requirements.txt`, `README.md` y la carpeta `.github/workflows/`) al repositorio. Puedes hacerlo desde la web de GitHub arrastrando los archivos ("Add file → Upload files"), sin usar la terminal.

## 3. Guardar la clave de AEMET como secreto

1. En el repositorio: **Settings → Secrets and variables → Actions → New repository secret**.
2. Nombre: `AEMET_API_KEY`. Valor: tu clave de AEMET. Guardar.

(El workflow la usa como variable de entorno; nunca queda escrita en el código ni es visible en los logs.)

## 4. Activar GitHub Pages

1. **Settings → Pages**.
2. En "Build and deployment" → Source: **Deploy from a branch**.
3. Branch: `gh-pages` (la crea automáticamente la primera vez que se ejecute el workflow) → carpeta `/ (root)`.
4. Guarda. Tu dashboard quedará accesible en `https://<tu-usuario>.github.io/<tu-repo>/`.

## 5. Ejecutar el workflow

- Se ejecuta solo, cada hora en el minuto 50 (lo puedes cambiar editando el `cron` en `.github/workflows/update_dashboard.yml`).
- Para probarlo ya, sin esperar: pestaña **Actions** del repo → "Actualizar dashboard AEMET" → **Run workflow**.
- Si falla, la pestaña Actions muestra el log paso a paso (útil para ver si el error viene de la clave, de un límite de peticiones, etc.).

## 6. Extender a más estaciones en el futuro

Edita `config.py` y añade otra entrada a la lista `STATIONS`, por ejemplo:

```python
{
    "nombre": "Madrid",
    "idema": None,
    "idema_tiempo_real": None,
    "busqueda_nombre": "MADRID, RETIRO",
    "municipio": "28079",
    "lat": 40.4117,
    "lon": -3.6805,
    "area_avisos": "72",
    "zona_avisos": "Metropolitana y Henares",
},
```

- `busqueda_nombre` es un texto que debe aparecer en el nombre oficial de la estación climatológica (el script lo busca solo, no hace falta el código IDEMA a mano).
- `area_avisos` y `zona_avisos` indican de qué comunidad autónoma y de qué zona de aviso se muestran los avisos (la lista de códigos está en `config.py`; el nombre de la zona se ve en el mapa de <https://www.aemet.es/es/eltiempo/prediccion/avisos>). Si se dejan en `None`, no se muestran avisos.
- `municipio` es el código INE de 5 dígitos que usa AEMET para la predicción por municipios: se ve en la URL de `aemet.es/es/eltiempo/prediccion/municipios/...-id<codigo>` buscando la localidad en <https://www.aemet.es/es/eltiempo/prediccion/municipios>.

El script genera un bloque de gráficos por cada estación de la lista, todos en el mismo `docs/index.html`.

## 7. Avisos por email

El workflow **Notificar avisos AEMET** (`.github/workflows/avisos.yml`) revisa cada 15 minutos los avisos de AEMET de las zonas de `config.py`. No hace falta configurar nada más que la clave de AEMET que ya usa el dashboard:

- Cada episodio de aviso (una zona y un fenómeno) abre un **issue** con la etiqueta `aviso-aemet` que menciona al usuario de `AVISOS_NOTIFICAR_A`, y **GitHub le envía un correo**. Los tramos del mismo episodio (p. ej. amarillo y luego naranja) van en el mismo issue.
- Si el aviso sube o baja de nivel o cambian sus horas, se añade un comentario (otro correo). Si AEMET lo retira antes de tiempo, se comenta y se cierra. Cuando termina a su hora, se cierra sin comentario (GitHub puede enviar igualmente un breve correo de "Closed").
- El nivel mínimo se elige con `AVISOS_NIVEL_MINIMO` en `config.py` (`"amarillo"`, `"naranja"` o `"rojo"`).
- Para que lleguen los correos, en <https://github.com/settings/notifications> debe estar activado el correo para "Participating, @mentions and custom".
- Para probarlo sin tocar GitHub: `AVISOS_SIMULACRO=1 python avisos_notificar.py` (con `AEMET_API_KEY` y `GITHUB_REPOSITORY` definidos).

Es un complemento, no un sistema de emergencias: puede retrasarse o fallar (GitHub, AEMET, el correo). Ante avisos rojos, sigue los canales oficiales (AEMET, 112, ES-Alert).

## Notas técnicas

- AEMET limita cada consulta de histórico a un máximo de ~1 año; el script trocea automáticamente el rango si algún día pides más de 90 días.
- AEMET tiene un límite de peticiones por minuto; si ves errores HTTP 429 el script reintenta automáticamente con una pequeña espera.
- Los datos climatológicos diarios (histórico) y la predicción por municipios son dos APIs distintas de AEMET; por eso la estación (`idema`) y el municipio (`municipio`) se configuran por separado, aunque sea la misma ciudad.
- Para la lluvia del año hidrológico, el histórico se descarga desde el 1 de octubre (aunque los gráficos solo muestran los últimos `DIAS_HISTORICO` días). La primera ejecución tras este cambio descarga esos meses de una vez; las siguientes solo piden los días nuevos.
- El histórico diario se guarda en `data/historico_<idema>.json` (y los normales en `data/normales_<idema>.json`), que el workflow sube al repositorio. En cada ejecución solo se piden a AEMET los días que falten en la ventana, incluidos los huecos que hubieran quedado de descargas anteriores.
- Los récords se guardan en `data/extremos_<idema>.json` y se renuevan cada 30 días. El periodo de referencia de los normales se lee una vez de los metadatos de AEMET y se guarda en `data/normales_<idema>_metadatos.json`.
- La última predicción y la última observación descargadas con éxito se guardan en `data/ultimo/` (fuera del repositorio; el workflow las conserva con `actions/cache`). Si AEMET falla en una ejecución, la página muestra esas copias con un aviso de su antigüedad en vez de quedarse vacía.
- Los datos de la boya salen del servicio que usa la web de Portus (`poem.puertos.es/portus/StationData`), que no está documentado como API pública: si deja de responder, el dashboard sigue funcionando con la última copia buena y, después, con el modelo. Solo se usan los valores que Puertos del Estado marca como buenos (calidad 1). Fuente: Puertos del Estado.
- Las versiones de `requirements.txt` están fijadas y la página carga la versión de plotly.js que corresponde al paquete de Python instalado. Al actualizar Plotly, comprueba que los gráficos se siguen viendo bien.

## Estructura del código

- `fetch_weather_dash.py`: descarga los datos de AEMET, los procesa y genera `docs/index.html`.
- `templates/`: la página (`pagina.html`), sus estilos (`estilos.css`) y su JavaScript (`app.js`). Los huecos que rellena el script se escriben `${nombre}`; un `$` literal se escribe `$$`.
- `config.py`: estaciones y opciones.
- `tests/`: pruebas automáticas con datos de ejemplo (no llaman a AEMET). Se ejecutan con `pip install -r requirements.txt -r requirements-dev.txt` y `pytest`; el workflow `pruebas.yml` las pasa, junto con `ruff`, en cada cambio.
