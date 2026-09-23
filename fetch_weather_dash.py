"""
Genera un dashboard HTML estático con datos climatológicos históricos y
predicción de AEMET OpenData para las estaciones/municipios definidos en
config.py.

Uso:
    export AEMET_API_KEY="tu_clave"
    python fetch_weather_dash.py

El resultado se escribe en docs/index.html (esa carpeta es la que se publica
como GitHub Pages, ver README.md).
"""

import calendar
import html
import io
import json
import os
import re
import sys
import tarfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs_version

from config import STATIONS, DIAS_HISTORICO

BASE_URL = "https://opendata.aemet.es/opendata/api"
API_KEY = os.environ.get("AEMET_API_KEY")

if not API_KEY:
    sys.exit(
        "ERROR: define la variable de entorno AEMET_API_KEY con tu clave "
        "gratuita de AEMET OpenData (https://opendata.aemet.es/centrodedescargas/altaUsuario)."
    )

HEADERS = {"api_key": API_KEY, "Accept": "application/json"}

ZONA_HORARIA = ZoneInfo("Europe/Madrid")

# Se carga la misma versión de plotly.js que espera el paquete de Python
# instalado: si no coinciden, los gráficos pueden no dibujarse.
PLOTLY_JS_URL = f"https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"

# Paleta Material Design (tonos 500, salvo donde se indica)
MATERIAL = {
    "rojo": "#F44336",
    "azul": "#2196F3",
    "azul_claro": "#03A9F4",
    "indigo": "#3F51B5",
    "morado": "#673AB7",
    "teal": "#009688",
    "verde": "#4CAF50",
    "gris": "#607D8B",
    "fondo": "#FAFAFA",
    "texto": "#212121",
}


class SinDatosAEMET(Exception):
    """AEMET respondió correctamente pero no hay datos para la consulta
    (su campo 'estado' vale 404)."""


def aemet_get(endpoint, retries=5, binario=False):
    """Llama a un endpoint de AEMET.

    AEMET responde primero con un JSON pequeño que contiene la URL real de
    los datos (campo 'datos'), así que hace falta una segunda petición para
    obtener el contenido de verdad. Con `binario=True` se devuelven los bytes
    tal cual (p. ej. el .tar de los avisos) en vez de interpretarlos como JSON.

    AEMET limita las peticiones por minuto con la misma clave (HTTP 429).
    Si se supera el límite, se espera cada vez más tiempo antes de
    reintentar (10s, 20s, 40s, 60s...), porque el límite tarda hasta un
    minuto en liberarse.
    """
    espera = 10
    for intento in range(retries):
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            print(f"Límite de peticiones de AEMET alcanzado, esperando {espera}s (intento {intento + 1}/{retries})...")
            time.sleep(espera)
            espera = min(espera * 2, 60)
            continue
        resp.raise_for_status()
        meta = resp.json()
        if meta.get("estado") == 404:
            raise SinDatosAEMET(meta.get("descripcion", "sin datos"))
        if meta.get("estado") != 200:
            raise RuntimeError(f"AEMET devolvió un error: {meta}")
        data_resp = requests.get(meta["datos"], timeout=30)
        data_resp.raise_for_status()
        if binario:
            return data_resp.content
        data_resp.encoding = "latin-1"  # AEMET no siempre declara bien el charset
        return data_resp.json()
    raise RuntimeError(f"Demasiados reintentos (HTTP 429) para {endpoint}")


def resolver_idema(busqueda_nombre):
    """Busca el código IDEMA de una estación climatológica por su nombre."""
    estaciones = aemet_get("/valores/climatologicos/inventarioestaciones/todasestaciones")
    candidatas = [e for e in estaciones if busqueda_nombre.upper() in e["nombre"].upper()]
    if not candidatas:
        raise ValueError(f"No se encontró ninguna estación que contenga '{busqueda_nombre}'")
    if len(candidatas) > 1:
        nombres = ", ".join(f"{c['nombre']} ({c['indicativo']})" for c in candidatas)
        print(f"Aviso: varias estaciones coinciden con '{busqueda_nombre}': {nombres}. Se usa la primera.")
    return candidatas[0]["indicativo"], candidatas[0]["nombre"]


CACHE_DIR = "data"


def _ruta_cache(idema):
    return os.path.join(CACHE_DIR, f"historico_{idema}.json")


def _leer_cache(idema):
    """Lee los registros históricos guardados en ejecuciones anteriores.
    Si el archivo no existe o está corrupto, se ignora y se parte de cero
    (no es un error fatal, solo implica pedir más días a AEMET esta vez)."""
    ruta = _ruta_cache(idema)
    if not os.path.exists(ruta):
        return []
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Aviso: no se pudo leer la caché {ruta}, se ignora: {exc}")
        return []


def _guardar_cache(idema, registros):
    """Guarda los registros históricos fusionados y ordenados por fecha."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    registros_ordenados = sorted(registros, key=lambda r: r.get("fecha", ""))
    with open(_ruta_cache(idema), "w", encoding="utf-8") as f:
        json.dump(registros_ordenados, f, ensure_ascii=False, indent=0)


def _fusionar_registros(existentes, nuevos):
    """Combina registros por fecha sin duplicados. Si una fecha aparece en
    ambos, gana el nuevo (por si AEMET corrigió un dato ya publicado)."""
    por_fecha = {r["fecha"]: r for r in existentes if "fecha" in r}
    for r in nuevos:
        if "fecha" in r:
            por_fecha[r["fecha"]] = r
    return list(por_fecha.values())


def obtener_historico(idema, dias):
    """Devuelve los valores climatológicos diarios de los últimos `dias`
    días, usando una caché local (data/historico_<idema>.json) para no
    volver a pedir a AEMET los días que ya se descargaron en una ejecución
    anterior: solo se piden los días nuevos desde la última fecha guardada.

    Los valores diarios de AEMET pasan por un proceso de validación antes de
    publicarse (AEMET indica un retardo oficial de unos 4 días), así que una
    petición cuyo rango llegue hasta "hoy" puede fallar. Por eso se aplica un
    margen de seguridad (MARGEN_DIAS) y el rango objetivo termina unos días
    antes de hoy.
    """
    MARGEN_DIAS = 5
    fecha_fin_objetivo = (datetime.utcnow() - timedelta(days=MARGEN_DIAS)).date()
    fecha_ini_objetivo = fecha_fin_objetivo - timedelta(days=dias)

    cache = _leer_cache(idema)
    fechas_cache = {r["fecha"][:10] for r in cache if "fecha" in r}

    # Se pide desde el primer día de la ventana que falte en la caché (no
    # solo desde el último guardado): así se rellenan los huecos que dejó
    # una descarga parcial y los días nuevos si se amplía DIAS_HISTORICO.
    fechas_objetivo = [fecha_ini_objetivo + timedelta(days=i) for i in range((fecha_fin_objetivo - fecha_ini_objetivo).days + 1)]
    faltan = [f for f in fechas_objetivo if f.isoformat() not in fechas_cache]

    nuevos = []
    if not faltan:
        print(f"Caché completa para la estación {idema} (hasta {fecha_fin_objetivo}); no se pide nada nuevo a AEMET.")
    else:
        fecha_ini_peticion = faltan[0]
        print(f"Faltan {len(faltan)} día(s) en la caché de la estación {idema}, el primero {fecha_ini_peticion}.")

        if fecha_ini_peticion <= fecha_fin_objetivo:
            cursor = datetime.combine(fecha_ini_peticion, datetime.min.time())
            fecha_fin_dt = datetime.combine(fecha_fin_objetivo, datetime.min.time())
            while cursor <= fecha_fin_dt:
                siguiente = min(cursor + timedelta(days=364), fecha_fin_dt)
                ini_str = cursor.strftime("%Y-%m-%dT00:00:00UTC")
                fin_str = siguiente.strftime("%Y-%m-%dT23:59:59UTC")
                endpoint = (
                    f"/valores/climatologicos/diarios/datos/"
                    f"fechaini/{ini_str}/fechafin/{fin_str}/estacion/{idema}"
                )
                try:
                    nuevos.extend(aemet_get(endpoint))
                except Exception as exc:
                    print(f"Aviso: no se pudo descargar el tramo {ini_str} - {fin_str}: {exc}")
                cursor = siguiente + timedelta(days=1)
            print(f"Descargados {len(nuevos)} registro(s) nuevo(s) de AEMET para la estación {idema} (desde {fecha_ini_peticion}).")

    cache_actualizada = _fusionar_registros(cache, nuevos)
    if nuevos:
        _guardar_cache(idema, cache_actualizada)

    # Solo se devuelve la ventana de días que se quiere mostrar en el dashboard.
    return [
        r for r in cache_actualizada
        if "fecha" in r and fecha_ini_objetivo.isoformat() <= r["fecha"][:10] <= fecha_fin_objetivo.isoformat()
    ]


def obtener_prediccion(municipio):
    """Descarga la predicción diaria (7 días) para un municipio."""
    data = aemet_get(f"/prediccion/especifica/municipio/diaria/{municipio}")
    return data[0]


def obtener_observacion_actual(idema_obs):
    """Descarga las lecturas recientes (última jornada) de una estación
    automática de observación y devuelve la más reciente (AEMET las da
    ordenadas de más antigua a más reciente, normalmente una por hora)."""
    lecturas = aemet_get(f"/observacion/convencional/datos/estacion/{idema_obs}")
    if not lecturas:
        return None
    return lecturas[-1]

def obtener_normales(idema):
    """Descarga los valores climatológicos normales (la media histórica de
    largo plazo, mes a mes) de una estación. Estos valores no cambian nunca,
    así que se guardan en una caché PERMANENTE (sin fecha de caducidad,
    a diferencia de la caché del histórico diario): una vez descargados
    para una estación, no se vuelven a pedir a AEMET."""
    ruta = os.path.join(CACHE_DIR, f"normales_{idema}.json")
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                registros_cache = json.load(f)
            print(f"Normales de {idema} leídos de caché: {len(registros_cache)} registro(s).")
            return registros_cache
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Aviso: no se pudo leer la caché de normales {ruta}, se vuelve a descargar: {exc}")

    registros = aemet_get(f"/valores/climatologicos/normales/estacion/{idema}")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False, indent=0)
    print(f"Normales descargados para {idema}: {len(registros)} registro(s).")
    return registros


# Última predicción y última observación descargadas con éxito. Si en una
# ejecución AEMET falla, se muestran estas (avisando de su antigüedad) en
# vez de publicar la página sin datos. No se suben al repositorio (ver
# .gitignore): el workflow las conserva entre ejecuciones con actions/cache.
ULTIMO_DIR = os.path.join(CACHE_DIR, "ultimo")


def _guardar_ultimo(clave, datos):
    try:
        os.makedirs(ULTIMO_DIR, exist_ok=True)
        with open(os.path.join(ULTIMO_DIR, f"{clave}.json"), "w", encoding="utf-8") as f:
            json.dump({"guardado": datetime.now(timezone.utc).isoformat(), "datos": datos}, f, ensure_ascii=False)
    except OSError as exc:
        print(f"Aviso: no se pudo guardar la copia de {clave}: {exc}")


def _leer_ultimo(clave):
    """Devuelve (datos, fecha_guardado) de la última copia buena, o (None, None)."""
    ruta = os.path.join(ULTIMO_DIR, f"{clave}.json")
    try:
        with open(ruta, encoding="utf-8") as f:
            contenido = json.load(f)
        return contenido["datos"], datetime.fromisoformat(contenido["guardado"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None, None


def _hora_local(momento, formato="%d/%m %H:%M"):
    return momento.astimezone(ZONA_HORARIA).strftime(formato)


# --- Avisos meteorológicos (Meteoalerta) --------------------------------

NS_CAP = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
NIVELES_AVISO = {"amarillo": 1, "naranja": 2, "rojo": 3}
# Por si algún aviso no trae el parámetro "AEMET-Meteoalerta nivel".
NIVEL_POR_SEVERIDAD = {"moderate": "amarillo", "severe": "naranja", "extreme": "rojo"}
DIAS_SEMANA = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _normalizar(texto):
    """Minúsculas y sin acentos, para comparar nombres de zona."""
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower().strip()


def _ficheros_cap(contenido):
    """AEMET entrega los avisos de un área como un .tar con un XML (formato
    CAP) por aviso; si solo hay uno puede llegar el XML suelto."""
    try:
        with tarfile.open(fileobj=io.BytesIO(contenido), mode="r:*") as tar:
            return [tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()]
    except tarfile.TarError:
        return [contenido]


def _avisos_de_cap(raiz):
    """Extrae los avisos (en español, de nivel amarillo o superior) de un
    documento CAP de AEMET."""
    def texto(nodo, etiqueta):
        return (nodo.findtext(f"cap:{etiqueta}", default="", namespaces=NS_CAP) or "").strip()

    if texto(raiz, "msgType").lower() == "cancel":
        return []

    avisos = []
    for info in raiz.findall("cap:info", NS_CAP):
        if not texto(info, "language").lower().startswith("es"):
            continue
        nivel = None
        for param in info.findall("cap:parameter", NS_CAP):
            if "nivel" in texto(param, "valueName").lower():
                nivel = _normalizar(texto(param, "value"))
        if nivel is None:
            nivel = NIVEL_POR_SEVERIDAD.get(texto(info, "severity").lower())
        if nivel not in NIVELES_AVISO:
            continue  # "verde" = sin aviso

        def fecha(etiqueta):
            valor = texto(info, etiqueta)
            try:
                return datetime.fromisoformat(valor) if valor else None
            except ValueError:
                return None

        avisos.append({
            "nivel": nivel,
            "evento": texto(info, "event"),
            "titular": texto(info, "headline"),
            "descripcion": texto(info, "description"),
            "inicio": fecha("onset") or fecha("effective"),
            "fin": fecha("expires"),
            "zonas": [
                (texto(area, "areaDesc"), [texto(g, "value") for g in area.findall("cap:geocode", NS_CAP)])
                for area in info.findall("cap:area", NS_CAP)
            ],
        })
    return avisos


def obtener_avisos(area):
    """Descarga los últimos avisos elaborados por AEMET para un área
    (comunidad autónoma, p. ej. "77" = Comunitat Valenciana)."""
    try:
        contenido = aemet_get(f"/avisos_cap/ultimoelaborado/area/{area}", binario=True)
    except SinDatosAEMET:
        return []
    avisos = []
    for xml in _ficheros_cap(contenido):
        try:
            avisos.extend(_avisos_de_cap(ET.fromstring(xml)))
        except ET.ParseError as exc:
            print(f"Aviso: se ignora un fichero de avisos que no es XML válido: {exc}")
    print(f"Avisos descargados para el área {area}: {len(avisos)} aviso(s) de nivel amarillo o superior en total.")
    return avisos


def avisos_para_zona(avisos, zona, ahora=None):
    """Filtra los avisos que afectan a `zona` (nombre de la zona de aviso,
    p. ej. "Litoral norte de Valencia", o su código numérico) y que aún no
    han caducado. Ordenados por inicio y, a igualdad, por gravedad."""
    ahora = ahora or datetime.now(timezone.utc)
    zona_norm = _normalizar(zona)
    vistos, resultado = set(), []
    for aviso in avisos:
        afecta = any(
            zona_norm in codigos or zona_norm == _normalizar(nombre)
            for nombre, codigos in aviso["zonas"]
        )
        if not afecta or (aviso["fin"] and aviso["fin"] <= ahora):
            continue
        clave = (aviso["nivel"], aviso["evento"], aviso["inicio"], aviso["fin"])
        if clave not in vistos:
            vistos.add(clave)
            resultado.append(aviso)
    lejano = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(resultado, key=lambda a: (a["inicio"] or ahora, -NIVELES_AVISO[a["nivel"]], a["fin"] or lejano))


def _momento_aviso(momento):
    local = momento.astimezone(ZONA_HORARIA)
    return f"{DIAS_SEMANA[local.weekday()]} {local:%d/%m %H:%M}"


def construir_banner_avisos(avisos, zona, error=False, ahora=None):
    """Banner con los avisos en vigor o próximos para la zona de la estación."""
    enlace = '<a href="https://www.aemet.es/es/eltiempo/prediccion/avisos" target="_blank" rel="noopener">Ver en aemet.es</a>'
    zona_html = html.escape(zona)
    if error:
        return f'<div class="avisos avisos-error">No se pudieron consultar los avisos de AEMET en esta actualización. {enlace}</div>'
    if not avisos:
        return f'<div class="avisos avisos-ninguno">✓ Sin avisos meteorológicos para {zona_html}.</div>'

    ahora = ahora or datetime.now(timezone.utc)
    items = []
    for aviso in avisos:
        inicio, fin = aviso["inicio"], aviso["fin"]
        if inicio and inicio > ahora:
            cuando = f"De {_momento_aviso(inicio)}"
        else:
            cuando = "En vigor"
        if fin:
            cuando += f" hasta {_momento_aviso(fin)}"
        titulo = aviso["evento"] or aviso["titular"] or f"Aviso {aviso['nivel']}"
        descripcion = f'<p>{html.escape(aviso["descripcion"])}</p>' if aviso["descripcion"] else ""
        items.append(
            f'<div class="aviso-meteo nivel-{aviso["nivel"]}">'
            f'<div class="aviso-cabecera"><strong>{html.escape(titulo)}</strong>'
            f'<span class="aviso-cuando">{cuando}</span></div>'
            f"{descripcion}</div>"
        )
    return (
        f'<div class="avisos"><h3 class="subtitulo">Avisos para {zona_html}</h3>'
        f'{"".join(items)}<p class="aviso">Fuente: AEMET Meteoalerta. {enlace}</p></div>'
    )


def normales_por_mes(registros):
    """Convierte los registros de valores normales en un diccionario
    {mes: registro} (mes de 1 a 12), descartando la fila anual (mes 13).

    El mes viene en el campo 'mes' (texto de dos dígitos, p. ej. '01' para
    enero); confirmado directamente contra una respuesta real de AEMET."""
    por_mes = {}
    for r in registros or []:
        try:
            mes = int(r.get("mes", ""))
        except (ValueError, TypeError):
            continue
        if 1 <= mes <= 12:
            por_mes[mes] = r
    return por_mes


def _num(valor):
    """Convierte un valor de AEMET (string, posiblemente con coma decimal)
    a float, o None si no es un número válido."""
    if valor is None or valor == "":
        return None
    try:
        return float(str(valor).replace(",", "."))
    except ValueError:
        return None


def _rango_eje(series, minimo, maximo, holgura):
    """Rango del eje Y: el habitual [minimo, maximo], ampliado (con
    `holgura` de margen) si algún dato se sale. Así los ejes de gráficos
    parecidos se pueden comparar a simple vista, pero un episodio extremo
    (p. ej. una DANA con más de 100 mm en un día) nunca queda cortado."""
    valores = pd.concat([pd.Series(s, dtype="float64") for s in series]).dropna()
    if valores.empty:
        return [minimo, maximo]
    bajo, alto = valores.min(), valores.max()
    return [minimo if bajo >= minimo else bajo - holgura, maximo if alto <= maximo else alto + holgura]


def normales_diarios(fechas, normales_registros, campos):
    """Valores normales día a día para las fechas dadas, interpolando
    linealmente los normales mensuales (cada uno situado a mitad de su mes).
    Devuelve un DataFrame con 'fecha' y una columna por campo, o vacío."""
    por_mes = normales_por_mes(normales_registros)
    if not por_mes or len(fechas) == 0:
        return pd.DataFrame()

    ini, fin = pd.Timestamp(fechas.min()).normalize(), pd.Timestamp(fechas.max()).normalize()
    anclas = pd.date_range(
        (ini - pd.DateOffset(months=1)).replace(day=1),
        (fin + pd.DateOffset(months=1)).replace(day=1),
        freq="MS",
    ) + pd.Timedelta(days=14)
    df_anclas = pd.DataFrame(
        [[_num(por_mes.get(a.month, {}).get(c)) for c in campos] for a in anclas],
        index=anclas, columns=campos, dtype="float64",
    )
    dias = pd.date_range(ini, fin, freq="D")
    df = df_anclas.reindex(df_anclas.index.union(dias)).interpolate(method="time").loc[dias]
    if df.isna().all().all():
        return pd.DataFrame()
    return df.rename_axis("fecha").reset_index()


def historico_a_dataframe(registros):
    """Convierte los registros diarios de AEMET en un DataFrame limpio.

    Campos usados (ver metadatos de AEMET): tmax/tmin (°C), prec (mm),
    velmedia/racha (m/s -> se convierten a km/h), hrmedia/hrmax/hrmin (%).
    """
    df = pd.DataFrame(registros)
    if df.empty:
        return df
    df.columns = [c.lower() for c in df.columns]
    df["fecha"] = pd.to_datetime(df["fecha"])

    columnas_numericas = ["tmax", "tmin", "tmed", "prec", "velmedia", "racha", "hrmedia", "hrmax", "hrmin"]
    for col in columnas_numericas:
        if col in df.columns:
            serie = df[col].astype(str).str.replace(",", ".", regex=False)
            if col == "prec":
                # "Ip" = precipitación inapreciable (< 0,1 mm)
                serie = serie.str.replace("Ip", "0", regex=False)
            df[col] = pd.to_numeric(serie, errors="coerce")

    # AEMET da la velocidad del viento en m/s; se pasa a km/h para que
    # coincida con las unidades de la predicción y con lo habitual en AEMET.
    for col in ["velmedia", "racha"]:
        if col in df.columns:
            df[col] = df[col] * 3.6

    return df.sort_values("fecha")


def _max_valor(lista, campo):
    """Extrae el valor numérico máximo de una lista de dicts de AEMET
    (p. ej. 'viento' o 'rachaMax' dentro de la predicción diaria)."""
    valores = []
    for item in lista or []:
        v = item.get(campo)
        if v is None or v == "":
            continue
        try:
            valores.append(float(str(v).replace(",", ".")))
        except (TypeError, ValueError):
            continue
    return max(valores) if valores else None


def prediccion_a_dataframe(prediccion):
    """Convierte la predicción diaria (7 días) de AEMET en un DataFrame.

    Por cada día se extrae: temperatura máx/mín, probabilidad de
    precipitación, viento (velocidad y racha máximas del día) y humedad
    relativa máx/mín.
    """
    filas = []
    for dia in prediccion.get("prediccion", {}).get("dia", []):
        fecha = dia["fecha"][:10]
        tmax = dia.get("temperatura", {}).get("maxima")
        tmin = dia.get("temperatura", {}).get("minima")

        probs = dia.get("probPrecipitacion", []) or []
        valores_precip = [p.get("value") for p in probs if p.get("value") is not None]
        prob_precip = max(valores_precip) if valores_precip else None

        humedad = dia.get("humedadRelativa", {}) or {}

        filas.append({
            "fecha": fecha,
            "tmax": tmax,
            "tmin": tmin,
            "prob_precip": prob_precip,
            "hum_max": humedad.get("maxima"),
            "hum_min": humedad.get("minima"),
            "viento_max": _max_valor(dia.get("viento"), "velocidad"),
            "racha_max": _max_valor(dia.get("rachaMax"), "value"),
        })

    df = pd.DataFrame(filas)
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"])
        for col in ["tmax", "tmin", "prob_precip", "hum_max", "hum_min"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _texto_barras(serie, sufijo):
    """Etiquetas de texto para barras, en el mismo orden que la serie."""
    return [f"{v:.0f}{sufijo}" if pd.notna(v) else "" for v in serie]


def _rangeselector():
    """Botones de rango rápido (7d/30d/90d/Todo) para el eje de fechas de
    los gráficos históricos. No pide nada nuevo a AEMET: solo recorta la
    vista sobre los datos que ya están cargados en el gráfico."""
    return dict(
        buttons=[
            dict(count=7, label="7d", step="day", stepmode="backward"),
            dict(count=30, label="30d", step="day", stepmode="backward"),
            dict(count=90, label="90d", step="day", stepmode="backward"),
            dict(step="all", label="Todo"),
        ],
        bgcolor="#EEEEEE",
        activecolor=MATERIAL["indigo"],
        font=dict(color=MATERIAL["texto"], size=11),
    )


def construir_graficos_historico(df, normales_registros=None):
    """Devuelve una lista de fragmentos HTML, uno por variable (temperatura,
    precipitación, viento, humedad), cada uno pensado para ocupar el ancho
    completo de la página (en vez de un único gráfico con 4 paneles). Cada
    uno incluye botones de rango rápido (7d/30d/90d/Todo). La temperatura
    lleva además la banda de valores normales de la estación, si los hay."""
    if df.empty:
        return ['<p class="aviso">No se pudieron cargar datos históricos en esta ejecución.</p>']

    graficos = []
    config = {"responsive": True}
    layout_comun = dict(template="plotly_white", height=360, margin=dict(t=70, b=40, l=50, r=20))

    # Temperatura, sobre la banda normal (media de las máximas y de las
    # mínimas de 1991–2020 para cada época del año). Un día por encima o por
    # debajo de la banda se ve de un vistazo como cálido o frío.
    fig = go.Figure()
    series_rango = [df["tmax"], df["tmin"]]
    df_normal = normales_diarios(df["fecha"], normales_registros, ["tm_max_md", "tm_min_md"])
    if not df_normal.empty:
        fig.add_trace(go.Scatter(
            x=df_normal["fecha"], y=df_normal["tm_max_md"], name="Máx. normal",
            line=dict(color=MATERIAL["rojo"], width=1, dash="dash"), opacity=0.6,
            hovertemplate="%{y:.1f} °C",
        ))
        fig.add_trace(go.Scatter(
            x=df_normal["fecha"], y=df_normal["tm_min_md"], name="Mín. normal",
            line=dict(color=MATERIAL["azul"], width=1, dash="dash"), opacity=0.6,
            fill="tonexty", fillcolor="rgba(96,125,139,0.15)",
            hovertemplate="%{y:.1f} °C",
        ))
        series_rango += [df_normal["tm_max_md"], df_normal["tm_min_md"]]
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmax"], name="Máxima", line=dict(color=MATERIAL["rojo"], width=2)))
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmin"], name="Mínima", line=dict(color=MATERIAL["azul"], width=2)))
    fig.update_xaxes(rangeselector=_rangeselector())
    fig.update_yaxes(range=_rango_eje(series_rango, 5, 45, 2), title="°C")
    fig.update_layout(hovermode="x unified")
    fig.update_layout(title="Temperatura", legend=dict(orientation="h", y=-0.25, traceorder="normal"), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Precipitación
    if "prec" in df.columns:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["fecha"], y=df["prec"], name="Precipitación", marker_color=MATERIAL["azul_claro"]))
        fig.update_xaxes(rangeselector=_rangeselector())
        fig.update_yaxes(range=_rango_eje([df["prec"]], 0, 50, 5), title="mm")
        fig.update_layout(title="Precipitación", showlegend=False, **layout_comun)
        graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Viento
    fig = go.Figure()
    if "velmedia" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["velmedia"], name="Vel. media", line=dict(color=MATERIAL["indigo"], width=2)))
    if "racha" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["racha"], name="Racha máx.", line=dict(color=MATERIAL["morado"], width=1.5, dash="dot")))
    fig.update_xaxes(rangeselector=_rangeselector())
    fig.update_yaxes(range=_rango_eje([df.get("velmedia"), df.get("racha")], 0, 80, 5), title="km/h")
    fig.update_layout(title="Viento", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Humedad
    fig = go.Figure()
    if "hrmax" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hrmax"], name="Humedad máx.", line=dict(color=MATERIAL["teal"], width=2)))
    if "hrmin" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hrmin"], name="Humedad mín.", line=dict(color=MATERIAL["verde"], width=2)))
    fig.update_xaxes(rangeselector=_rangeselector())
    fig.update_yaxes(range=[0, 100], title="%")
    fig.update_layout(title="Humedad relativa", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    return graficos


def construir_graficos_prediccion(df):
    """Devuelve una lista de fragmentos HTML, uno por variable (temperatura,
    probabilidad de precipitación, viento, humedad), cada uno a ancho
    completo — en vez de una única cuadrícula 2x2, que en pantallas
    estrechas (móvil) quedaría ilegible porque Plotly la calcula como una
    sola imagen de tamaño fijo. Temperatura y humedad usan barras
    superpuestas (mínima delante/encima de máxima, valor dentro/fuera de
    la barra); cada gráfico lleva una línea vertical separando los días."""
    if df.empty:
        return ['<p class="aviso">No se pudo cargar la predicción en esta ejecución.</p>']

    graficos = []
    config = {"responsive": True}
    layout_comun = dict(template="plotly_white", height=320, margin=dict(t=50, b=40, l=50, r=20))

    def lineas_de_dia(fig):
        for fecha in df["fecha"]:
            fig.add_vline(x=fecha, line_width=1, line_dash="dot", line_color=MATERIAL["gris"], opacity=0.3)

    # Temperatura: la máxima se dibuja primero (detrás, texto fuera para que
    # no quede tapado) y la mínima después (delante/encima, texto dentro).
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["fecha"], y=df["tmax"], name="Máxima prevista", marker_color=MATERIAL["rojo"],
        text=_texto_barras(df["tmax"], "°"), textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=df["fecha"], y=df["tmin"], name="Mínima prevista", marker_color=MATERIAL["azul"],
        text=_texto_barras(df["tmin"], "°"), textposition="inside",
    ))
    lineas_de_dia(fig)
    fig.update_yaxes(range=_rango_eje([df["tmax"], df["tmin"]], 5, 45, 3), title="°C")
    fig.update_layout(title="Temperatura prevista", barmode="overlay", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Probabilidad de precipitación
    if "prob_precip" in df.columns:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["fecha"], y=df["prob_precip"], name="Prob. precipitación", marker_color=MATERIAL["azul_claro"]))
        lineas_de_dia(fig)
        fig.update_yaxes(range=[0, 100], title="%")
        fig.update_layout(title="Prob. precipitación prevista", showlegend=False, **layout_comun)
        graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Viento
    fig = go.Figure()
    if "viento_max" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["viento_max"], name="Viento previsto", mode="lines+markers", line=dict(color=MATERIAL["indigo"], width=2)))
    if "racha_max" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["racha_max"], name="Racha prevista", mode="lines+markers", line=dict(color=MATERIAL["morado"], width=1.5, dash="dot")))
    lineas_de_dia(fig)
    fig.update_yaxes(range=_rango_eje([df.get("viento_max"), df.get("racha_max")], 0, 50, 5), title="km/h")
    fig.update_layout(title="Viento previsto", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Humedad: igual que temperatura, mínima delante/encima de máxima.
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["fecha"], y=df["hum_max"], name="Humedad máx. prevista", marker_color=MATERIAL["teal"],
        text=_texto_barras(df["hum_max"], "%"), textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=df["fecha"], y=df["hum_min"], name="Humedad mín. prevista", marker_color=MATERIAL["verde"],
        text=_texto_barras(df["hum_min"], "%"), textposition="inside",
    ))
    lineas_de_dia(fig)
    fig.update_yaxes(range=[0, 100], title="%")
    fig.update_layout(title="Humedad prevista", barmode="overlay", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    return graficos


def _slug(texto):
    """Convierte un nombre de estación en un identificador simple (sin
    acentos ni espacios) para usarlo en atributos HTML y en JavaScript."""
    texto = texto.lower()
    for original, sin_acento in {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n", "ü": "u"}.items():
        texto = texto.replace(original, sin_acento)
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto or "estacion"


def construir_tarjetas_kpi(lectura):
    """Tarjetas destacadas con las condiciones observadas más recientes:
    temperatura, viento, humedad y precipitación de la última hora."""
    if not lectura:
        return ""

    hora = lectura.get("fint")
    if hora:
        try:
            hora = datetime.strptime(hora, "%Y-%m-%dT%H:%M:%S").strftime("%H:%M UTC")
        except ValueError:
            pass

    def fmt(valor, unidad, decimales=1):
        return f"{valor:.{decimales}f} {unidad}" if valor is not None else "sin datos"

    vv = lectura.get("vv")
    viento_kmh = vv * 3.6 if vv is not None else None

    kpis = [
        ("Temperatura ahora", MATERIAL["rojo"], fmt(lectura.get("ta"), "°C")),
        ("Viento ahora", MATERIAL["indigo"], fmt(viento_kmh, "km/h")),
        ("Humedad ahora", MATERIAL["teal"], fmt(lectura.get("hr"), "%", 0)),
        ("Precipitación (última hora)", MATERIAL["azul_claro"], fmt(lectura.get("prec"), "mm")),
    ]

    tarjetas = "".join(
        f'<div class="tarjeta-kpi" style="border-top-color:{color};">'
        f"<h3>{etiqueta}</h3>"
        f'<p class="valor-kpi">{valor}</p>'
        f"</div>"
        for etiqueta, color, valor in kpis
    )
    nota_hora = f'<p class="aviso">Última observación: {hora}</p>' if hora else ""
    return f'<div class="tarjetas-kpi">{tarjetas}</div>{nota_hora}'


#: (etiqueta, color, columna para el valor máximo, columna para el valor
#: mínimo, unidad). Cuando max y min vienen de la misma columna (p. ej.
#: precipitación) se repite la columna en ambos huecos.
VARIABLES_EXTREMOS_HISTORICO = [
    ("Temperatura", MATERIAL["rojo"], "tmax", "tmin", "°C"),
    ("Viento", MATERIAL["indigo"], "racha", "velmedia", "km/h"),
    ("Humedad", MATERIAL["teal"], "hrmax", "hrmin", "%"),
    ("Precipitación", MATERIAL["azul_claro"], "prec", "prec", "mm"),
]

VARIABLES_EXTREMOS_PREDICCION = [
    ("Temperatura prevista", MATERIAL["rojo"], "tmax", "tmin", "°C"),
    ("Viento previsto", MATERIAL["indigo"], "racha_max", "viento_max", "km/h"),
    ("Prob. precipitación prevista", MATERIAL["azul_claro"], "prob_precip", "prob_precip", "%"),
    ("Humedad prevista", MATERIAL["teal"], "hum_max", "hum_min", "%"),
]


def _extremo(df, col, modo):
    if col not in df.columns or df[col].dropna().empty:
        return None
    idx = df[col].idxmax() if modo == "max" else df[col].idxmin()
    valor = df.loc[idx, col]
    fecha = df.loc[idx, "fecha"].strftime("%d/%m/%Y")
    return valor, fecha

NOMBRES_MES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def _mes_completo_mas_reciente(df_hist):
    """Devuelve (año, mes, datos_de_ese_mes) del mes calendario más reciente
    que el histórico cubre ENTERO (todos sus días), o None si no hay ninguno.

    No basta con tomar el mes anterior al actual: AEMET publica los datos
    diarios con unos días de retraso, así que a principios de mes el mes
    anterior aún está incompleto y, por ejemplo, su precipitación total
    saldría por debajo de la real. En ese caso se usa el mes previo."""
    if df_hist.empty:
        return None
    fechas = set(df_hist["fecha"].dt.normalize())
    primera = min(fechas)
    cursor = datetime.utcnow().date().replace(day=1) - timedelta(days=1)
    while pd.Timestamp(cursor.replace(day=1)) >= primera.replace(day=1):
        anio, mes = cursor.year, cursor.month
        dias_mes = calendar.monthrange(anio, mes)[1]
        dias = pd.date_range(f"{anio}-{mes:02d}-01", periods=dias_mes, freq="D")
        if all(d in fechas for d in dias):
            df_mes = df_hist[(df_hist["fecha"].dt.year == anio) & (df_hist["fecha"].dt.month == mes)]
            return anio, mes, df_mes
        print(f"Diagnóstico anomalía: {NOMBRES_MES[mes]} de {anio} está incompleto en el histórico, se prueba el mes anterior.")
        cursor = cursor.replace(day=1) - timedelta(days=1)
    return None


# Clasificación de un valor mensual frente a la serie de referencia, con
# los mismos términos que AEMET en sus avances climatológicos: los quintiles
# (q1..q4) reparten los años de referencia en cinco grupos del 20 %, y
# "extremadamente" es un valor fuera de todo lo registrado en ese periodo.
CLASES_TEMPERATURA = [
    ("Extremadamente frío", "#0D47A1"), ("Muy frío", "#1565C0"), ("Frío", "#1976D2"),
    ("Normal", "#546E7A"),
    ("Cálido", "#C43E00"), ("Muy cálido", "#B71C1C"), ("Extremadamente cálido", "#7F0000"),
]
CLASES_PRECIPITACION = [
    ("Extremadamente seco", "#3E2723"), ("Muy seco", "#5D4037"), ("Seco", "#795548"),
    ("Normal", "#546E7A"),
    ("Húmedo", "#0277BD"), ("Muy húmedo", "#01579B"), ("Extremadamente húmedo", "#002F6C"),
]
CLASES_GENERICAS = [
    ("Extremadamente bajo", "#263238"), ("Muy bajo", "#37474F"), ("Bajo", "#455A64"),
    ("Normal", "#546E7A"),
    ("Alto", "#3949AB"), ("Muy alto", "#283593"), ("Extremadamente alto", "#1A237E"),
]


def clasificar(valor, normal, prefijo, clases):
    """Sitúa `valor` entre el mínimo, los quintiles y el máximo de la serie
    de referencia (campos <prefijo>_min, _q1.._q4, _max de los normales).
    Devuelve (texto, color) o None si faltan umbrales."""
    umbrales = [_num(normal.get(f"{prefijo}_{s}")) for s in ("min", "q1", "q2", "q3", "q4", "max")]
    if valor is None or pd.isna(valor) or any(u is None for u in umbrales):
        return None
    minimo, q1, q2, q3, q4, maximo = umbrales
    if valor < minimo:
        return clases[0]
    if valor < q1:
        return clases[1]
    if valor < q2:
        return clases[2]
    if valor <= q3:
        return clases[3]
    if valor <= q4:
        return clases[4]
    if valor <= maximo:
        return clases[5]
    return clases[6]


def construir_tarjetas_anomalia(df_hist, normales_registros):
    """Compara el último mes calendario completo disponible en el histórico
    frente a los valores climatológicos normales de ese mismo mes, y lo
    clasifica (frío/normal/cálido, seco/normal/húmedo...) según los
    quintiles de la serie de referencia."""
    if df_hist.empty or not normales_registros:
        print(f"Diagnóstico anomalía: df_hist vacío={df_hist.empty}, normales vacíos={not normales_registros}")
        return ""

    resultado = _mes_completo_mas_reciente(df_hist)
    if not resultado:
        print("Diagnóstico anomalía: no se encontró ningún mes calendario completo en el histórico.")
        return ""
    anio, mes, df_mes = resultado

    meses_disponibles = normales_por_mes(normales_registros)
    normal = meses_disponibles.get(mes)
    if not normal:
        print(
            f"Diagnóstico anomalía: se buscaba el mes {mes} ({NOMBRES_MES[mes]}) pero no está entre los "
            f"meses reconocidos en los normales: {sorted(meses_disponibles.keys())}."
        )
        return ""

    filas = []

    def comparar(etiqueta, color, valor_real, prefijo, estadistico, unidad, clases, decimales=1, porcentaje=False):
        """`estadistico` es 'md' (media) o 'mn' (mediana) de los normales."""
        valor_normal = _num(normal.get(f"{prefijo}_{estadistico}"))
        if valor_real is None or valor_normal is None or pd.isna(valor_real):
            return
        diferencia = valor_real - valor_normal
        signo = "+" if diferencia >= 0 else ""
        referencia = "mediana" if estadistico == "mn" else "normal"
        texto_diferencia = f"{signo}{diferencia:.{decimales}f} {unidad} vs. {referencia}"
        if porcentaje and valor_normal > 0:
            texto_diferencia += f" ({100 * valor_real / valor_normal:.0f} % de la {referencia})"
        filas.append((
            etiqueta, color,
            f"{valor_real:.{decimales}f} {unidad}",
            texto_diferencia,
            clasificar(valor_real, normal, prefijo, clases),
        ))

    if "tmed" in df_mes.columns:
        comparar("Temperatura media", MATERIAL["rojo"], df_mes["tmed"].mean(), "tm_mes", "md", "°C", CLASES_TEMPERATURA)
    if "prec" in df_mes.columns:
        if df_mes["prec"].notna().all():
            # La lluvia mensual es muy asimétrica (unos pocos meses
            # torrenciales inflan la media), así que se compara con la mediana.
            comparar("Precipitación total", MATERIAL["azul_claro"], df_mes["prec"].sum(), "p_mes", "mn", "mm",
                     CLASES_PRECIPITACION, 0, porcentaje=True)
        else:
            print(f"Diagnóstico anomalía: faltan {df_mes['prec'].isna().sum()} día(s) de precipitación en {NOMBRES_MES[mes]}; no se compara el total.")
    if "hrmedia" in df_mes.columns:
        comparar("Humedad media", MATERIAL["teal"], df_mes["hrmedia"].mean(), "hr", "md", "%", CLASES_GENERICAS, 0)
    if "velmedia" in df_mes.columns:
        # w_med_md (el normal) ya viene en km/h de AEMET; velmedia ya se convirtió
        # de m/s a km/h en historico_a_dataframe, así que las unidades coinciden.
        comparar("Viento medio", MATERIAL["indigo"], df_mes["velmedia"].mean(), "w_med", "md", "km/h", CLASES_GENERICAS)

    if not filas:
        return ""

    def etiqueta_clase(clase):
        if not clase:
            return ""
        texto, color = clase
        return f'<p><span class="clase" style="background:{color};">{texto}</span></p>'

    tarjetas = "".join(
        f'<div class="tarjeta" style="border-top-color:{color};">'
        f"<h3>{etiqueta}</h3>"
        f"<p>{valor}</p>"
        f"<p class='fecha'>{diferencia}</p>"
        f"{etiqueta_clase(clase)}"
        f"</div>"
        for etiqueta, color, valor, diferencia, clase in filas
    )
    titulo = f'<h3 class="subtitulo">{NOMBRES_MES[mes].capitalize()} de {anio} frente a lo normal</h3>'
    explicacion = (
        '<p class="aviso">Último mes completo comparado con los valores normales de AEMET para '
        'esta estación (periodo de referencia 1991–2020). La etiqueta sitúa el mes entre los años '
        'de referencia: «Normal» es el 20 % central; «Cálido/Frío» (o «Húmedo/Seco», «Alto/Bajo»), '
        'el 20 % siguiente por cada lado; «Muy…», el 20 % más extremo; y «Extremadamente…», fuera '
        'de todo lo registrado en el periodo.</p>'
    )
    return f'{titulo}{explicacion}<div class="tarjetas">{tarjetas}</div>'

def construir_recuadro_extremos(df, variables):
    """Tarjetas con el valor más alto y más bajo de cada variable de
    `variables` (ver VARIABLES_EXTREMOS_HISTORICO / _PREDICCION más arriba)."""
    if df.empty:
        return ""

    tarjetas = []
    for etiqueta, color, col_max, col_min, unidad in variables:
        alto = _extremo(df, col_max, "max")
        bajo = _extremo(df, col_min, "min")
        alto_html = f"{alto[0]:.1f} {unidad} <span class='fecha'>({alto[1]})</span>" if alto else "sin datos"
        bajo_html = f"{bajo[0]:.1f} {unidad} <span class='fecha'>({bajo[1]})</span>" if bajo else "sin datos"
        tarjetas.append(
            f'<div class="tarjeta" style="border-top-color:{color};">'
            f"<h3>{etiqueta}</h3>"
            f"<p>▲ Máx: {alto_html}</p>"
            f"<p>▼ Mín: {bajo_html}</p>"
            f"</div>"
        )
    return f'<div class="tarjetas">{"".join(tarjetas)}</div>'


def main():
    os.makedirs("docs", exist_ok=True)
    bloques_html = []
    estaciones_menu = []
    generado = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    avisos_por_area = {}  # se descargan una sola vez por área, aunque la compartan varias estaciones

    for estacion in STATIONS:
        idema = estacion.get("idema")
        nombre = estacion["nombre"]
        df_hist, df_pred = pd.DataFrame(), pd.DataFrame()
        lectura_actual = None
        notas_antiguedad = []

        # Avisos meteorológicos de la zona de la estación.
        banner_avisos = ""
        zona_avisos, area_avisos = estacion.get("zona_avisos"), estacion.get("area_avisos")
        if zona_avisos and area_avisos:
            if area_avisos not in avisos_por_area:
                try:
                    print(f"Procesando avisos del área {area_avisos}...")
                    avisos_por_area[area_avisos] = obtener_avisos(area_avisos)
                except Exception as exc:
                    print(f"Aviso: no se pudieron obtener los avisos del área {area_avisos}: {exc}")
                    avisos_por_area[area_avisos] = None
            avisos_area = avisos_por_area[area_avisos]
            if avisos_area is None:
                banner_avisos = construir_banner_avisos([], zona_avisos, error=True)
            else:
                banner_avisos = construir_banner_avisos(avisos_para_zona(avisos_area, zona_avisos), zona_avisos)

        # La observación actual, el histórico y la predicción se piden por
        # separado: si una de las tres falla (p. ej. por el límite de
        # peticiones de AEMET), las demás se siguen mostrando en vez de
        # perder todo el bloque de la estación. Si fallan la observación o
        # la predicción, se usa la última copia buena guardada.
        idema_obs = estacion.get("idema_tiempo_real") or idema
        try:
            print(f"Procesando observación actual de {nombre} (idema={idema_obs})...")
            lectura_actual = obtener_observacion_actual(idema_obs)
            if lectura_actual:
                _guardar_ultimo(f"observacion_{idema_obs}", lectura_actual)
        except Exception as exc:
            print(f"Aviso: no se pudo obtener la observación actual de {nombre}: {exc}")
            lectura_actual, guardado = _leer_ultimo(f"observacion_{idema_obs}")
            if lectura_actual:
                notas_antiguedad.append(f"la observación actual (se muestra la descargada el {_hora_local(guardado)})")

        time.sleep(2)

        try:
            if not idema:
                idema, nombre_real = resolver_idema(estacion["busqueda_nombre"])
                nombre = estacion.get("nombre") or nombre_real
            print(f"Procesando histórico de {nombre} (idema={idema})...")
            historico = obtener_historico(idema, DIAS_HISTORICO)
            df_hist = historico_a_dataframe(historico)
        except Exception as exc:
            print(f"Aviso: no se pudo obtener el histórico de {nombre}: {exc}")

        normales = []
        try:
            if idema:
                normales = obtener_normales(idema)
        except Exception as exc:
            print(f"Aviso: no se pudieron obtener los valores normales de {nombre}: {exc}")

        time.sleep(2)  # pequeña pausa para no encadenar peticiones demasiado rápido

        municipio = estacion["municipio"]
        prediccion = None
        try:
            print(f"Procesando predicción de {nombre}...")
            prediccion = obtener_prediccion(municipio)
            if prediccion_a_dataframe(prediccion).empty:
                dias_recibidos = prediccion.get("prediccion", {}).get("dia", [])
                print(
                    f"Aviso: la predicción de {nombre} se descargó sin errores pero "
                    f"salió vacía. Claves de nivel superior recibidas: {list(prediccion.keys())}. "
                    f"Días dentro de 'prediccion': {len(dias_recibidos)}."
                )
                prediccion = None
            else:
                _guardar_ultimo(f"prediccion_{municipio}", prediccion)
        except Exception as exc:
            print(f"Aviso: no se pudo obtener la predicción de {nombre}: {exc}")
        if prediccion is None:
            prediccion, guardado = _leer_ultimo(f"prediccion_{municipio}")
            if prediccion:
                notas_antiguedad.append(f"la predicción (se muestra la descargada el {_hora_local(guardado)})")
        if prediccion:
            df_pred = prediccion_a_dataframe(prediccion)
            # Con una predicción antigua, los primeros días ya han pasado.
            hoy = pd.Timestamp(datetime.now(ZONA_HORARIA).date())
            if not df_pred.empty:
                df_pred = df_pred[df_pred["fecha"] >= hoy].reset_index(drop=True)

        slug = _slug(nombre)
        estaciones_menu.append((slug, nombre, estacion.get("lat"), estacion.get("lon")))
        seccion = [f'<section class="estacion" data-estacion="{slug}"><h2>{html.escape(nombre)}</h2>']
        seccion.append(banner_avisos)
        if notas_antiguedad:
            seccion.append(
                '<p class="aviso aviso-antiguo">⚠ AEMET no respondió en esta actualización para '
                + " ni para ".join(notas_antiguedad) + ".</p>"
            )
        seccion.append(construir_tarjetas_kpi(lectura_actual))

        # Extremos previstos arriba (donde antes estaban los históricos);
        # pronóstico primero (con fondo propio), histórico después con sus
        # propios extremos históricos.
        seccion.append(construir_recuadro_extremos(df_pred, VARIABLES_EXTREMOS_PREDICCION))

        seccion.append('<div class="bloque-pronostico">')
        seccion.append('<h3 class="subtitulo">Pronóstico (7 días)</h3>')
        seccion.append('<div class="graficos-apilados">')
        seccion.extend(construir_graficos_prediccion(df_pred))
        seccion.append('</div>')
        seccion.append('</div>')

        seccion.append('<h3 class="subtitulo">Histórico</h3>')
        seccion.append(construir_recuadro_extremos(df_hist, VARIABLES_EXTREMOS_HISTORICO))
        seccion.append(construir_tarjetas_anomalia(df_hist, normales))
        seccion.append('<div class="graficos-apilados">')
        seccion.extend(construir_graficos_historico(df_hist, normales))
        seccion.append('</div>')

        seccion.append("</section>")
        bloques_html.append("".join(seccion))

    selector_html = ""
    if len(estaciones_menu) > 1:
        opciones = "".join(f'<option value="{slug}">{html.escape(nombre)}</option>' for slug, nombre, _, _ in estaciones_menu)
        selector_html = f'<select id="selector-estacion" class="selector-estacion" aria-label="Elegir estación">{opciones}</select>'

    estaciones_con_coordenadas = [
        (slug, nombre, lat, lon) for slug, nombre, lat, lon in estaciones_menu if lat is not None and lon is not None
    ]
    mapa_html = ""
    if estaciones_con_coordenadas:
        mapa_html = '<div id="mapa-estaciones" class="mapa-estaciones"></div>'
    datos_mapa_js = json.dumps([
        {"slug": slug, "nombre": nombre, "lat": lat, "lon": lon}
        for slug, nombre, lat, lon in estaciones_con_coordenadas
    ], ensure_ascii=False)

    pagina = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<script>
(function() {{
    try {{
        var guardado = localStorage.getItem('tema-aemet');
        var prefiereOscuro = window.matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.setAttribute('data-theme', guardado || (prefiereOscuro ? 'dark' : 'light'));
    }} catch (e) {{}}
}})();
</script>
<title>Dashboard AEMET</title>
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icon-192.png">
<meta name="theme-color" content="#3F51B5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="AEMET Dashboard">
<script src="{PLOTLY_JS_URL}"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
:root {{
    --md-rojo: {MATERIAL["rojo"]};
    --md-azul: {MATERIAL["azul"]};
    --md-azul-claro: {MATERIAL["azul_claro"]};
    --md-indigo: {MATERIAL["indigo"]};
    --md-morado: {MATERIAL["morado"]};
    --md-teal: {MATERIAL["teal"]};
    --md-verde: {MATERIAL["verde"]};
    --md-gris: {MATERIAL["gris"]};
    --fondo: {MATERIAL["fondo"]};
    --superficie: #FFFFFF;
    --texto: {MATERIAL["texto"]};
    --texto-secundario: {MATERIAL["gris"]};
    --sombra: rgba(0,0,0,0.12);
    --sombra-suave: rgba(0,0,0,0.08);
    --borde: rgba(0,0,0,0.08);
    --fondo-pronostico: #E8EAF6;
}}
:root[data-theme="dark"] {{
    --fondo: #121212;
    --superficie: #1E1E1E;
    --texto: #ECECEC;
    --texto-secundario: #A0A0A0;
    --sombra: rgba(0,0,0,0.6);
    --sombra-suave: rgba(0,0,0,0.4);
    --borde: rgba(255,255,255,0.1);
    --fondo-pronostico: #232B45;
}}
body {{
    font-family: "Roboto", -apple-system, Arial, sans-serif;
    margin: 0; padding: 2rem;
    background: var(--fondo);
    color: var(--texto);
}}
.contenedor {{ max-width: 1100px; margin: 0 auto; }}
.cabecera {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }}
h1 {{ font-size: 1.6rem; font-weight: 500; margin: 0; }}
.controles-cabecera {{ display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }}
.boton-tema {{
    background: var(--superficie); color: var(--texto); border: 1px solid var(--borde);
    border-radius: 50%; width: 44px; height: 44px; font-size: 1.3rem; line-height: 1;
    cursor: pointer; box-shadow: 0 1px 3px var(--sombra); flex-shrink: 0;
}}
.selector-estacion {{
    background: var(--superficie); color: var(--texto); border: 1px solid var(--borde);
    border-radius: 6px; padding: 0.55rem 0.75rem; font-size: 0.95rem; cursor: pointer;
    box-shadow: 0 1px 3px var(--sombra);
}}
h2 {{ font-size: 1.3rem; font-weight: 500; color: var(--md-indigo); border-bottom: 2px solid var(--md-indigo); padding-bottom: 0.3rem; }}
.subtitulo {{ font-size: 1.05rem; font-weight: 500; color: var(--texto-secundario); margin-top: 1.5rem; }}
.estacion {{ background: var(--superficie); border-radius: 8px; padding: 1.5rem; margin-bottom: 2rem; box-shadow: 0 1px 4px var(--sombra), 0 1px 2px var(--sombra-suave); }}
.tarjetas {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0 1.5rem; }}
.tarjeta {{ flex: 1 1 200px; background: var(--superficie); border-radius: 6px; border-top: 4px solid; padding: 0.8rem 1rem; box-shadow: 0 1px 3px var(--sombra); }}
.tarjeta h3 {{ margin: 0 0 0.4rem; font-size: 0.95rem; font-weight: 500; }}
.tarjeta p {{ margin: 0.2rem 0; font-size: 0.9rem; }}
.tarjeta .fecha {{ color: var(--texto-secundario); font-size: 0.8rem; }}
.aviso {{ color: var(--texto-secundario); font-style: italic; }}
.tarjetas-kpi {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 0.5rem 0 0.25rem; }}
.tarjeta-kpi {{ flex: 1 1 150px; background: var(--superficie); border-radius: 6px; border-top: 4px solid; padding: 1rem; box-shadow: 0 1px 3px var(--sombra); text-align: center; }}
.tarjeta-kpi h3 {{ margin: 0 0 0.5rem; font-size: 0.8rem; font-weight: 500; color: var(--texto-secundario); }}
.valor-kpi {{ margin: 0; font-size: 1.7rem; font-weight: 700; }}
.bloque-pronostico {{ background: var(--fondo-pronostico); border-radius: 8px; padding: 1rem 1rem 0.5rem; margin-bottom: 1.5rem; }}
.bloque-pronostico .subtitulo {{ margin-top: 0; }}
.mapa-estaciones {{ height: 320px; border-radius: 8px; margin: 1rem 0 1.5rem; box-shadow: 0 1px 4px var(--sombra), 0 1px 2px var(--sombra-suave); }}
.avisos {{ margin: 0.5rem 0 1rem; }}
.avisos .subtitulo {{ margin-top: 0; }}
.avisos-ninguno, .avisos-error {{ padding: 0.6rem 0.9rem; border-radius: 6px; font-size: 0.9rem; }}
.avisos-ninguno {{ background: rgba(76,175,80,0.14); color: var(--texto); }}
.avisos-error {{ background: rgba(96,125,139,0.14); color: var(--texto); }}
.avisos a {{ color: inherit; }}
.aviso-meteo {{ border-radius: 6px; padding: 0.7rem 0.9rem; margin-bottom: 0.5rem; border-left: 6px solid rgba(0,0,0,0.35); }}
.aviso-meteo p {{ margin: 0.35rem 0 0; font-size: 0.9rem; }}
.aviso-cabecera {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0.25rem 1rem; }}
.aviso-cuando {{ font-size: 0.85rem; }}
.nivel-amarillo {{ background: #FFEB3B; color: #212121; }}
.nivel-naranja {{ background: #FF9800; color: #212121; }}
.nivel-rojo {{ background: #C62828; color: #FFFFFF; }}
.aviso-antiguo {{ color: #E65100; font-style: normal; }}
:root[data-theme="dark"] .aviso-antiguo {{ color: #FFB74D; }}
.clase {{ display: inline-block; margin-top: 0.3rem; padding: 0.15rem 0.55rem; border-radius: 999px; color: #FFFFFF; font-size: 0.8rem; font-weight: 500; }}
.graficos-apilados {{ display: flex; flex-direction: column; gap: 0.5rem; }}
.graficos-apilados > div, .graficos-apilados .plotly-graph-div {{ width: 100% !important; }}
footer {{ margin-top: 2rem; color: var(--texto-secundario); font-size: 0.85rem; text-align: center; }}
@media (max-width: 640px) {{
    body {{ padding: 1rem; }}
    h1 {{ font-size: 1.3rem; }}
    .estacion {{ padding: 1rem; }}
    .bloque-pronostico {{ padding: 0.75rem 0.75rem 0.25rem; }}
    .tarjeta, .tarjeta-kpi {{ flex-basis: 100%; }}
    .mapa-estaciones {{ height: 240px; }}
}}
</style>
</head>
<body>
<div class="contenedor">
<div class="cabecera">
<h1>Dashboard climatológico — AEMET OpenData</h1>
<div class="controles-cabecera">
{selector_html}
<button id="toggle-tema" class="boton-tema" aria-label="Cambiar de tema">🌙</button>
</div>
</div>
<p>Datos históricos ({DIAS_HISTORICO} días) y predicción a 7 días.</p>
{mapa_html}
{''.join(bloques_html)}
<footer>Generado automáticamente el {generado}. Fuente: AEMET OpenData.</footer>
</div>
<script>
(function() {{
    var raiz = document.documentElement;
    var boton = document.getElementById('toggle-tema');

    function coloresGrafico(tema) {{
        var esOscuro = tema === 'dark';
        return {{
            fondo: esOscuro ? '#1E1E1E' : '#FFFFFF',
            texto: esOscuro ? '#ECECEC' : '#212121',
            rejilla: esOscuro ? '#333333' : '#E5E5E5',
        }};
    }}

    function actualizarGraficos(tema) {{
        var c = coloresGrafico(tema);
        document.querySelectorAll('.plotly-graph-div').forEach(function(div) {{
            if (!div.layout) return;
            var actualizacion = {{ paper_bgcolor: c.fondo, plot_bgcolor: c.fondo, 'font.color': c.texto }};
            Object.keys(div.layout).forEach(function(clave) {{
                if (/^(xaxis|yaxis)\\d*$/.test(clave)) {{
                    actualizacion[clave + '.gridcolor'] = c.rejilla;
                    actualizacion[clave + '.linecolor'] = c.rejilla;
                    actualizacion[clave + '.zerolinecolor'] = c.rejilla;
                    if (div.layout[clave] && div.layout[clave].rangeselector) {{
                        actualizacion[clave + '.rangeselector.bgcolor'] = c.rejilla;
                        actualizacion[clave + '.rangeselector.font.color'] = c.texto;
                    }}
                }}
            }});
            if (div.layout.annotations && div.layout.annotations.length) {{
                actualizacion.annotations = div.layout.annotations.map(function(a) {{
                    var copia = Object.assign({{}}, a);
                    copia.font = Object.assign({{}}, a.font, {{ color: c.texto }});
                    return copia;
                }});
            }}
            Plotly.relayout(div, actualizacion);
        }});
    }}

    function actualizarBoton(tema) {{
        boton.textContent = tema === 'dark' ? '☀️' : '🌙';
        boton.setAttribute('aria-label', tema === 'dark' ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro');
    }}

    var temaActual = raiz.getAttribute('data-theme') || 'light';
    actualizarBoton(temaActual);
    actualizarGraficos(temaActual);

    boton.addEventListener('click', function() {{
        var nuevo = raiz.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        raiz.setAttribute('data-theme', nuevo);
        try {{ localStorage.setItem('tema-aemet', nuevo); }} catch (e) {{}}
        actualizarBoton(nuevo);
        actualizarGraficos(nuevo);
    }});
}})();

(function() {{
    var selector = document.getElementById('selector-estacion');
    if (!selector) return;  // solo hay una estación, no hace falta selector

    var secciones = document.querySelectorAll('.estacion');

    function mostrarEstacion(slug) {{
        secciones.forEach(function(sec) {{
            var visible = sec.getAttribute('data-estacion') === slug;
            sec.style.display = visible ? '' : 'none';
            if (visible) {{
                sec.querySelectorAll('.plotly-graph-div').forEach(function(div) {{
                    if (div.layout && window.Plotly) {{
                        Plotly.Plots.resize(div);
                    }}
                }});
            }}
        }});
    }}

    var slugs = Array.prototype.map.call(secciones, function(sec) {{ return sec.getAttribute('data-estacion'); }});
    var guardada = null;
    try {{ guardada = localStorage.getItem('estacion-aemet'); }} catch (e) {{}}
    var inicial = (guardada && slugs.indexOf(guardada) !== -1) ? guardada : slugs[0];

    selector.value = inicial;
    mostrarEstacion(inicial);

    selector.addEventListener('change', function() {{
        try {{ localStorage.setItem('estacion-aemet', selector.value); }} catch (e) {{}}
        mostrarEstacion(selector.value);
    }});
}})();

(function() {{
    var contenedor = document.getElementById('mapa-estaciones');
    if (!contenedor || !window.L) return;

    var estaciones = {datos_mapa_js};
    if (!estaciones.length) return;

    var mapa = L.map('mapa-estaciones');
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        maxZoom: 19,
    }}).addTo(mapa);

    var grupo = L.featureGroup();
    estaciones.forEach(function(est) {{
        var marcador = L.marker([est.lat, est.lon]).bindPopup(est.nombre);
        marcador.on('click', function() {{
            var selector = document.getElementById('selector-estacion');
            if (!selector) return;
            var tieneOpcion = Array.prototype.some.call(selector.options, function(o) {{ return o.value === est.slug; }});
            if (tieneOpcion) {{
                selector.value = est.slug;
                selector.dispatchEvent(new Event('change'));
            }}
        }});
        marcador.addTo(grupo);
    }});
    grupo.addTo(mapa);

    if (estaciones.length === 1) {{
        mapa.setView([estaciones[0].lat, estaciones[0].lon], 13);
    }} else {{
        mapa.fitBounds(grupo.getBounds(), {{ padding: [30, 30] }});
    }}
}})();

if ("serviceWorker" in navigator) {{
    window.addEventListener("load", function() {{
        navigator.serviceWorker.register("sw.js").catch(function(error) {{
            console.log("No se pudo registrar el service worker:", error);
        }});
    }});
}}
</script>
</body>
</html>"""

    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(pagina)
    print("Dashboard generado en docs/index.html")


if __name__ == "__main__":
    main()
