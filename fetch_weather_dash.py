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
import math
import os
import re
import sys
import tarfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
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

# Todos los gráficos en español: nombres de meses y días en los ejes, y coma
# decimal. El idioma "es" se registra en la propia página (ver LOCALE_ES_JS).
CONFIG_PLOTLY = {"responsive": True, "locale": "es"}
LOCALE_ES_JS = """Plotly.register({
    moduleType: 'locale', name: 'es',
    dictionary: {'Autoscale': 'Autoescalar', 'Reset axes': 'Restablecer ejes', 'Zoom in': 'Acercar',
                 'Zoom out': 'Alejar', 'Pan': 'Desplazar', 'Zoom': 'Zoom',
                 'Download plot as a png': 'Descargar gráfico como PNG'},
    format: {
        days: ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado'],
        shortDays: ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'],
        months: ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
                 'septiembre', 'octubre', 'noviembre', 'diciembre'],
        shortMonths: ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sept', 'oct', 'nov', 'dic'],
        date: '%d/%m/%Y', decimal: ',', thousands: '.'
    }
});"""

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
    "ambar": "#FFB300",
    "normal": "#8A8A8A",  # líneas de referencia; legible sobre fondo claro y oscuro
    "fondo": "#FAFAFA",
    "texto": "#212121",
}


class SinDatosAEMET(Exception):
    """AEMET respondió correctamente pero no hay datos para la consulta
    (su campo 'estado' vale 404)."""


def aemet_get(endpoint, retries=5, binario=False, metadatos=False):
    """Llama a un endpoint de AEMET.

    AEMET responde primero con un JSON pequeño que contiene la URL real de
    los datos (campo 'datos'), así que hace falta una segunda petición para
    obtener el contenido de verdad. Con `binario=True` se devuelven los bytes
    tal cual (p. ej. el .tar de los avisos) en vez de interpretarlos como JSON.
    Con `metadatos=True` se devuelve la descripción de los datos (campo
    'metadatos') en vez de los datos: unidades, periodo de referencia, etc.

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
        data_resp = requests.get(meta["metadatos" if metadatos else "datos"], timeout=30)
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


# AEMET publica los valores diarios con unos 4 días de retraso (validación);
# el histórico se pide hasta MARGEN_DIAS días antes de hoy.
MARGEN_DIAS = 5


def fecha_fin_historico():
    return (datetime.utcnow() - timedelta(days=MARGEN_DIAS)).date()


def inicio_anio_hidrologico(fecha):
    """El año hidrológico en España va del 1 de octubre al 30 de septiembre."""
    return date(fecha.year if fecha.month >= 10 else fecha.year - 1, 10, 1)


def obtener_historico(idema, dias, desde=None):
    """Devuelve los valores climatológicos diarios de los últimos `dias`
    días, usando una caché local (data/historico_<idema>.json) para no
    volver a pedir a AEMET los días que ya se descargaron en una ejecución
    anterior: solo se piden los días que falten.

    Los valores diarios de AEMET pasan por un proceso de validación antes de
    publicarse (AEMET indica un retardo oficial de unos 4 días), así que una
    petición cuyo rango llegue hasta "hoy" puede fallar. Por eso se aplica un
    margen de seguridad (MARGEN_DIAS) y el rango objetivo termina unos días
    antes de hoy.

    Con `desde` (una fecha) la ventana empieza como muy tarde ese día, aunque
    sea más de `dias` atrás (p. ej. el inicio del año hidrológico).
    """
    fecha_fin_objetivo = fecha_fin_historico()
    fecha_ini_objetivo = fecha_fin_objetivo - timedelta(days=dias)
    if desde:
        fecha_ini_objetivo = min(fecha_ini_objetivo, desde)

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
                # Tramos de ~6 meses: AEMET rechaza rangos demasiado largos.
                siguiente = min(cursor + timedelta(days=180), fecha_fin_dt)
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


def obtener_prediccion_horaria(municipio):
    """Descarga la predicción horaria (hoy, mañana y pasado) de un municipio."""
    data = aemet_get(f"/prediccion/especifica/municipio/horaria/{municipio}")
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


def periodo_normales(idema):
    """Periodo de referencia de los valores normales (p. ej. "1981–2010"),
    leído de los metadatos que AEMET publica junto a los datos, o None si no
    se puede saber. No se da por hecho: la web de AEMET usa ya 1991–2020,
    pero la descripción de este servicio de OpenData hablaba de 1981–2010.
    Los metadatos se guardan en caché permanente, como los normales."""
    ruta = os.path.join(CACHE_DIR, f"normales_{idema}_metadatos.json")
    metadatos = None
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                metadatos = json.load(f)
        except (json.JSONDecodeError, OSError):
            metadatos = None
    if metadatos is None:
        try:
            metadatos = aemet_get(f"/valores/climatologicos/normales/estacion/{idema}", metadatos=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(metadatos, f, ensure_ascii=False, indent=0)
        except Exception as exc:
            print(f"Aviso: no se pudieron obtener los metadatos de los normales de {idema}: {exc}")
            return None
    texto = json.dumps(metadatos, ensure_ascii=False)
    encontrado = re.search(r"\b(19\d\d|20\d\d)\s*(?:-|–|/|a|al|y)\s*(19\d\d|20\d\d)\b", texto)
    if not encontrado or int(encontrado.group(2)) - int(encontrado.group(1)) < 10:
        print(f"Diagnóstico normales: no se encontró el periodo en los metadatos de {idema}: {texto[:500]}")
        return None
    periodo = f"{encontrado.group(1)}–{encontrado.group(2)}"
    print(f"Periodo de los normales de {idema}: {periodo}.")
    return periodo


# --- Récords de la estación (valores extremos) ---------------------------

DIAS_REFRESCO_EXTREMOS = 30  # los récords pueden batirse: se renuevan cada mes

#: Por parámetro de AEMET (T temperatura, P precipitación): los récords
#: que se muestran, como (clave interna, campo del valor, campo del día,
#: campo del año, unidad). Los nombres de campo son los que usa AEMET en esta
#: respuesta; si alguno no llega, ese récord se omite y el log lo indica.
#: El viento no se incluye: AEMET no deja claro en qué unidad da su récord
#: y un récord con la unidad equivocada sería peor que no mostrarlo.
CAMPOS_EXTREMOS = {
    "T": [
        ("tmax", "temMax", "diaMax", "anioMax", "°C"),
        ("tmin", "temMin", "diaMin", "anioMin", "°C"),
    ],
    "P": [
        ("prec", "precMaxDia", "diaMaxDia", "anioMaxDia", "mm"),
    ],
}


def obtener_extremos(idema):
    """Descarga los valores extremos (récords) de temperatura, precipitación
    y viento de la estación y los devuelve como
    {clave: [ {valor, dia, anio} por mes 1..12 ]}. Caché en
    data/extremos_<idema>.json, renovada cada DIAS_REFRESCO_EXTREMOS días."""
    ruta = os.path.join(CACHE_DIR, f"extremos_{idema}.json")
    crudos = {}
    try:
        with open(ruta, encoding="utf-8") as f:
            guardado = json.load(f)
        edad = datetime.now(timezone.utc) - datetime.fromisoformat(guardado["guardado"])
        if edad < timedelta(days=DIAS_REFRESCO_EXTREMOS):
            crudos = guardado["datos"]
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        pass

    if not crudos:
        for parametro in CAMPOS_EXTREMOS:
            try:
                datos = aemet_get(f"/valores/climatologicos/valoresextremos/parametro/{parametro}/estacion/{idema}")
                crudos[parametro] = datos[0] if isinstance(datos, list) else datos
            except Exception as exc:
                print(f"Aviso: no se pudieron obtener los extremos '{parametro}' de {idema}: {exc}")
            time.sleep(2)
        if crudos:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump({"guardado": datetime.now(timezone.utc).isoformat(), "datos": crudos}, f, ensure_ascii=False, indent=0)
    return extremos_por_mes(crudos)


def extremos_por_mes(crudos):
    """Interpreta la respuesta de valores extremos de AEMET: listas de 13
    valores (12 meses + el anual) por campo. AEMET da las cifras en décimas
    (de °C o de mm). Se comprueba con la temperatura, que en décimas siempre
    tiene algún valor por encima de 60; la precipitación sigue el mismo
    criterio (o, sin temperatura, se toma en décimas si pasa de 1000)."""
    en_decimas = None
    resultado = {}
    for parametro, campos in CAMPOS_EXTREMOS.items():
        datos = crudos.get(parametro) or {}
        if not datos:
            continue
        for clave, c_valor, c_dia, c_anio, unidad in campos:
            valores, dias, anios = datos.get(c_valor), datos.get(c_dia), datos.get(c_anio)
            if not all(isinstance(x, list) and len(x) >= 12 for x in (valores, dias, anios)):
                print(f"Diagnóstico extremos: falta '{c_valor}'/'{c_dia}'/'{c_anio}' en '{parametro}'. Campos recibidos: {sorted(datos.keys())}")
                continue
            numeros = [_num(v) for v in valores[:12]]
            validos = [abs(v) for v in numeros if v is not None]
            if not validos:
                continue
            if parametro == "T" and en_decimas is None:
                en_decimas = max(validos) > 60
            decimas = en_decimas if en_decimas is not None else max(validos) > 1000
            escala = 10 if decimas else 1
            resultado[clave] = [
                {"valor": v / escala if v is not None else None, "dia": _num(d), "anio": _num(a), "unidad": unidad}
                for v, d, a in zip(numeros, dias[:12], anios[:12])
            ]
    return resultado


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


# --- Temperatura del mar --------------------------------------------------

URL_MAR = "https://marine-api.open-meteo.com/v1/marine"


def obtener_temperatura_mar(lat, lon):
    """Temperatura superficial del mar (°C) en un punto frente a la costa,
    de los análisis del servicio marino de Copernicus que distribuye
    Open-Meteo (gratuito, sin clave). Es un valor de modelo, no de una boya.
    Devuelve {"ahora": °C, "hace_semana": °C o None, "hora": datetime}."""
    resp = requests.get(URL_MAR, params={
        "latitude": lat, "longitude": lon, "hourly": "sea_surface_temperature",
        "past_days": 7, "forecast_days": 1, "timezone": "Europe/Madrid",
    }, timeout=30)
    resp.raise_for_status()
    horario = resp.json().get("hourly", {})
    serie = pd.Series(
        pd.to_numeric(pd.Series(horario.get("sea_surface_temperature", [])), errors="coerce").values,
        index=pd.to_datetime(horario.get("time", [])),
    ).dropna()
    if serie.empty:
        raise ValueError(f"respuesta sin temperatura del mar: {list(horario.keys())}")
    ahora = pd.Timestamp(datetime.now(ZONA_HORARIA).replace(tzinfo=None))
    pasado = serie[serie.index <= ahora]
    actual = pasado if not pasado.empty else serie
    hora = actual.index[-1]
    semana = serie[serie.index <= hora - pd.Timedelta(days=7)]
    return {
        "ahora": float(actual.iloc[-1]),
        "hace_semana": float(semana.iloc[-1]) if not semana.empty else None,
        "hora": hora.isoformat(),
    }


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


def _es(valor, decimales=1):
    """Número con coma decimal, como se escribe en español (26,3)."""
    return f"{valor:.{decimales}f}".replace(".", ",")


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

    columnas_numericas = [
        "tmax", "tmin", "tmed", "prec", "velmedia", "racha", "hrmedia", "hrmax", "hrmin",
        "pintmax", "presmax", "presmin", "sol",
    ]
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


def _dato_del_dia(lista, campo):
    """De una lista de AEMET por periodos, el elemento del día completo
    ("00-24") o, si no lo hay, el primero con `campo` relleno."""
    candidatos = [item for item in lista or [] if item.get(campo) not in (None, "")]
    for item in candidatos:
        if item.get("periodo") in ("00-24", None, ""):
            return item
    return candidatos[0] if candidatos else None


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
        sensacion = dia.get("sensTermica", {}) or {}
        cielo = _dato_del_dia(dia.get("estadoCielo"), "value")
        viento = _dato_del_dia(dia.get("viento"), "direccion")

        filas.append({
            "fecha": fecha,
            "tmax": tmax,
            "tmin": tmin,
            "prob_precip": prob_precip,
            "hum_max": humedad.get("maxima"),
            "hum_min": humedad.get("minima"),
            "viento_max": _max_valor(dia.get("viento"), "velocidad"),
            "racha_max": _max_valor(dia.get("rachaMax"), "value"),
            "sens_max": sensacion.get("maxima"),
            "sens_min": sensacion.get("minima"),
            "uv_max": dia.get("uvMax"),  # AEMET solo lo da para los primeros días
            "cielo": (cielo or {}).get("value"),
            "cielo_texto": (cielo or {}).get("descripcion"),
            "viento_dir": (viento or {}).get("direccion"),
        })

    df = pd.DataFrame(filas)
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"])
        for col in ["tmax", "tmin", "prob_precip", "hum_max", "hum_min", "sens_max", "sens_min", "uv_max"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _texto_barras(serie, sufijo):
    """Etiquetas de texto para barras, en el mismo orden que la serie."""
    return [f"{v:.0f}{sufijo}" if pd.notna(v) else "" for v in serie]


# Fechas en orden español ("6 sept") en ejes y ventanas emergentes.
FORMATO_FECHAS = dict(tickformat="%-d %b", hoverformat="%-d %b %Y")


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
    config = CONFIG_PLOTLY
    layout_comun = dict(template="plotly_white", height=360, margin=dict(t=70, b=40, l=50, r=20))

    # Temperatura, sobre la banda normal (media de las máximas y de las
    # mínimas del periodo de referencia para cada época del año). Un día por encima o por
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
    fig.update_xaxes(rangeselector=_rangeselector(), **FORMATO_FECHAS)
    fig.update_yaxes(range=_rango_eje(series_rango, 5, 45, 2), title="°C")
    fig.update_layout(hovermode="x unified")
    fig.update_layout(title="Temperatura", legend=dict(orientation="h", y=-0.25, traceorder="normal"), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Precipitación
    if "prec" in df.columns:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["fecha"], y=df["prec"], name="Precipitación", marker_color=MATERIAL["azul_claro"]))
        fig.update_xaxes(rangeselector=_rangeselector(), **FORMATO_FECHAS)
        fig.update_yaxes(range=_rango_eje([df["prec"]], 0, 50, 5), title="mm")
        fig.update_layout(title="Precipitación", showlegend=False, **layout_comun)
        graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Intensidad máxima de la lluvia: dice si fue torrencial mejor que el total.
    if "pintmax" in df.columns and (df["pintmax"] > 0).any():
        graficos.append(_grafico_intensidad_lluvia(df, config, layout_comun))

    # Viento
    fig = go.Figure()
    if "velmedia" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["velmedia"], name="Vel. media", line=dict(color=MATERIAL["indigo"], width=2)))
    if "racha" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["racha"], name="Racha máx.", line=dict(color=MATERIAL["morado"], width=1.5, dash="dot")))
    fig.update_xaxes(rangeselector=_rangeselector(), **FORMATO_FECHAS)
    fig.update_yaxes(range=_rango_eje([df.get("velmedia"), df.get("racha")], 0, 80, 5), title="km/h")
    fig.update_layout(title="Viento", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Humedad
    fig = go.Figure()
    if "hrmax" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hrmax"], name="Humedad máx.", line=dict(color=MATERIAL["teal"], width=2)))
    if "hrmin" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["hrmin"], name="Humedad mín.", line=dict(color=MATERIAL["verde"], width=2)))
    fig.update_xaxes(rangeselector=_rangeselector(), **FORMATO_FECHAS)
    fig.update_yaxes(range=[0, 100], title="%")
    fig.update_layout(title="Humedad relativa", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Presión: banda entre la mínima y la máxima de cada día, sobre la presión
    # media normal de la estación. Las bajadas marcadas anuncian borrascas y
    # DANAs. AEMET la da a la altura de la estación (no reducida al nivel del
    # mar), igual que el valor normal, así que son comparables entre sí.
    if {"presmax", "presmin"} <= set(df.columns) and df["presmin"].notna().any():
        fig = go.Figure()
        series = [df["presmax"], df["presmin"]]
        fig.add_trace(go.Scatter(
            x=df["fecha"], y=df["presmax"], name="Máxima", line=dict(color=MATERIAL["gris"], width=1.5),
            hovertemplate="%{y:.1f} hPa",
        ))
        fig.add_trace(go.Scatter(
            x=df["fecha"], y=df["presmin"], name="Mínima", line=dict(color=MATERIAL["indigo"], width=1.5),
            fill="tonexty", fillcolor="rgba(63,81,181,0.12)", hovertemplate="%{y:.1f} hPa",
        ))
        df_normal = normales_diarios(df["fecha"], normales_registros, ["q_med_md"])
        if not df_normal.empty:
            fig.add_trace(go.Scatter(
                x=df_normal["fecha"], y=df_normal["q_med_md"], name="Media normal",
                line=dict(color=MATERIAL["normal"], width=1.5, dash="dash"),
                hovertemplate="%{y:.1f} hPa",
            ))
            series.append(df_normal["q_med_md"])
        fig.update_xaxes(rangeselector=_rangeselector(), **FORMATO_FECHAS)
        fig.update_yaxes(range=_rango_eje(series, 1000, 1025, 2), title="hPa")
        fig.update_layout(
            title="Presión atmosférica (a la altura de la estación)", hovermode="x unified",
            legend=dict(orientation="h", y=-0.25, traceorder="normal"), **layout_comun,
        )
        graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Horas de sol, frente a la media normal de horas de sol diarias.
    if "sol" in df.columns and df["sol"].notna().any():
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=df["fecha"], y=df["sol"], name="Horas de sol", marker_color=MATERIAL["ambar"],
            hovertemplate="%{y:.1f} h<extra></extra>",
        ))
        df_normal = normales_diarios(df["fecha"], normales_registros, ["inso_md"])
        if not df_normal.empty:
            fig.add_trace(go.Scatter(
                x=df_normal["fecha"], y=df_normal["inso_md"], name="Media normal",
                line=dict(color=MATERIAL["normal"], width=1.5, dash="dash"),
                hovertemplate="%{y:.1f} h<extra>Media normal</extra>",
            ))
        fig.update_xaxes(rangeselector=_rangeselector(), **FORMATO_FECHAS)
        fig.update_yaxes(range=[0, 15], title="h")
        fig.update_layout(title="Horas de sol", legend=dict(orientation="h", y=-0.25), **layout_comun)
        graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))
        faltan = int(df["sol"].isna().sum())
        if faltan:
            graficos.append(f'<p class="aviso">La estación no tiene dato de horas de sol en {faltan} de los {len(df)} días.</p>')

    return graficos


#: Clasificación de AEMET de la intensidad de la lluvia (mm/h):
#: (límite superior incluido, nombre).
CLASES_INTENSIDAD_LLUVIA = [(2, "débil"), (15, "moderada"), (30, "fuerte"), (60, "muy fuerte"), (None, "torrencial")]


def clase_intensidad_lluvia(mm_h):
    for limite, nombre in CLASES_INTENSIDAD_LLUVIA:
        if limite is None or mm_h <= limite:
            return nombre


def _grafico_intensidad_lluvia(df, config, layout_comun):
    """Intensidad máxima de la lluvia de cada día ('pintMax' de AEMET: la
    lluvia de los 10 minutos más intensos, expresada en mm/h). Las líneas
    marcan los umbrales de AEMET de lluvia fuerte, muy fuerte y torrencial."""
    datos = df[df["pintmax"] > 0]
    hora = datos["horapintmax"] if "horapintmax" in datos.columns else pd.Series("", index=datos.index)
    textos = [
        f"{clase_intensidad_lluvia(v).capitalize()}" + (f" · a las {h}" if isinstance(h, str) and ":" in h else "")
        for v, h in zip(datos["pintmax"], hora)
    ]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=datos["fecha"], y=datos["pintmax"], name="Intensidad máxima", marker_color=MATERIAL["azul"],
        customdata=textos, hovertemplate="%{y:.1f} mm/h<br>%{customdata}<extra></extra>",
    ))
    for umbral, nombre in [(15, "fuerte"), (30, "muy fuerte"), (60, "torrencial")]:
        fig.add_hline(
            y=umbral, line_width=1, line_dash="dot", line_color=MATERIAL["gris"],
            annotation_text=nombre, annotation_position="top left",
            annotation_font=dict(size=10, color=MATERIAL["gris"]),
        )
    fig.update_xaxes(rangeselector=_rangeselector(), **FORMATO_FECHAS, range=[df["fecha"].min(), df["fecha"].max() + pd.Timedelta(days=1)])
    fig.update_yaxes(range=_rango_eje([datos["pintmax"]], 0, 70, 10), title="mm/h")
    fig.update_layout(title="Intensidad máxima de la lluvia", showlegend=False, **layout_comun)
    return (
        fig.to_html(full_html=False, include_plotlyjs=False, config=config)
        + '<p class="aviso">Lluvia de los 10 minutos más intensos de cada día, en mm/h. Según AEMET, '
        'la lluvia es fuerte por encima de 15 mm/h, muy fuerte por encima de 30 y torrencial por encima de 60.</p>'
    )


HORAS_PREDICCION_HORARIA = 48


def _por_hora(lista):
    """{hora: valor} de una lista de AEMET con periodos de una hora ("08")."""
    resultado = {}
    for item in lista or []:
        periodo = str(item.get("periodo", ""))
        if len(periodo) == 2 and periodo.isdigit() and "value" in item:
            resultado[int(periodo)] = item["value"]
    return resultado


def _por_tramo(lista, hora):
    """Valor del tramo de varias horas ("0814" = de 8 a 14) que contiene `hora`."""
    for item in lista or []:
        periodo = str(item.get("periodo", ""))
        if len(periodo) == 4 and periodo.isdigit():
            inicio, fin = int(periodo[:2]), int(periodo[2:])
            dentro = inicio <= hora < fin if inicio < fin else hora >= inicio
            if dentro:
                return item.get("value")
    return None


def prediccion_horaria_a_dataframe(prediccion, ahora=None):
    """Convierte la predicción horaria de AEMET en (DataFrame, noches).

    El DataFrame tiene una fila por hora (hora oficial española) desde la
    hora actual hasta HORAS_PREDICCION_HORARIA horas después: temperatura,
    sensación térmica, precipitación (mm), probabilidad de precipitación y
    de tormenta (%, por tramos de 6 horas), humedad, viento, racha y
    dirección. `noches` es una lista de (inicio, fin) entre la puesta y la
    salida del sol, para sombrear la noche en los gráficos."""
    filas, soles = [], []
    for dia in prediccion.get("prediccion", {}).get("dia", []):
        fecha = pd.Timestamp(dia["fecha"][:10])
        temperatura = _por_hora(dia.get("temperatura"))
        sensacion = _por_hora(dia.get("sensTermica"))
        precipitacion = _por_hora(dia.get("precipitacion"))
        humedad = _por_hora(dia.get("humedadRelativa"))
        velocidad, direccion, racha = {}, {}, {}
        for item in dia.get("vientoAndRachaMax") or []:
            periodo = str(item.get("periodo", ""))
            if not (len(periodo) == 2 and periodo.isdigit()):
                continue
            if "velocidad" in item:
                velocidad[int(periodo)] = (item.get("velocidad") or [None])[0]
                direccion[int(periodo)] = (item.get("direccion") or [None])[0]
            elif "value" in item:
                racha[int(periodo)] = item["value"]
        for hora in sorted(temperatura):
            filas.append({
                "fecha": fecha + pd.Timedelta(hours=hora),
                "temp": temperatura.get(hora),
                "sens": sensacion.get(hora),
                "prec": precipitacion.get(hora),
                "prob_precip": _por_tramo(dia.get("probPrecipitacion"), hora),
                "prob_tormenta": _por_tramo(dia.get("probTormenta"), hora),
                "hum": humedad.get(hora),
                "viento": velocidad.get(hora),
                "racha": racha.get(hora),
                "dir": direccion.get(hora),
            })
        orto, ocaso = dia.get("orto"), dia.get("ocaso")
        if orto and ocaso and ":" in orto and ":" in ocaso:
            soles.append((fecha + pd.Timedelta(orto + ":00"), fecha + pd.Timedelta(ocaso + ":00")))

    df = pd.DataFrame(filas)
    if df.empty:
        return df, []
    for col in ["temp", "sens", "prec", "prob_precip", "prob_tormenta", "hum", "viento", "racha"]:
        # "Ip" = precipitación inapreciable.
        df[col] = pd.to_numeric(df[col].astype(str).str.replace("Ip", "0", regex=False).str.replace(",", "."), errors="coerce")
    ahora = ahora or pd.Timestamp(datetime.now(ZONA_HORARIA).replace(tzinfo=None)).floor("h")
    df = df[(df["fecha"] >= ahora) & (df["fecha"] <= ahora + pd.Timedelta(hours=HORAS_PREDICCION_HORARIA))]
    df = df.drop_duplicates("fecha").reset_index(drop=True)

    noches = []
    for (_, ocaso), (orto_siguiente, _) in zip(soles, soles[1:] + [(None, None)]):
        fin = orto_siguiente if orto_siguiente is not None else ocaso + pd.Timedelta(hours=11)
        noches.append((ocaso, fin))
    if soles:
        noches.insert(0, (soles[0][0].normalize(), soles[0][0]))  # madrugada del primer día
    return df, noches


def construir_graficos_horarios(df, noches):
    """Gráficos de las próximas 48 horas: temperatura y sensación,
    precipitación, probabilidad de lluvia y de tormenta, y viento. La noche
    va sombreada."""
    if df.empty:
        return []
    config = CONFIG_PLOTLY
    layout_comun = dict(template="plotly_white", height=300, margin=dict(t=50, b=40, l=50, r=20), hovermode="x unified")
    inicio, fin = df["fecha"].min(), df["fecha"].max()

    def preparar(fig, titulo, eje, rango):
        for ini_noche, fin_noche in noches:
            if fin_noche > inicio and ini_noche < fin:
                fig.add_vrect(x0=max(ini_noche, inicio), x1=min(fin_noche, fin), fillcolor=MATERIAL["gris"],
                              opacity=0.12, line_width=0, layer="below")
        fig.update_xaxes(range=[inicio, fin], dtick=12 * 3600 * 1000, tick0=inicio.normalize(),
                         tickformat="%H h<br>%a %-d", hoverformat="%a %-d, %H h", tickangle=0)
        fig.update_yaxes(range=rango, title=eje)
        fig.update_layout(title=titulo, legend=dict(orientation="h", y=-0.3), **layout_comun)
        return fig.to_html(full_html=False, include_plotlyjs=False, config=config)

    graficos = []

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["temp"], name="Temperatura", line=dict(color=MATERIAL["rojo"], width=2),
                             hovertemplate="%{y:.0f} °C"))
    series = [df["temp"]]
    if ((df["sens"] - df["temp"]).abs() >= 1).any():
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["sens"], name="Sensación térmica",
                                 line=dict(color="#B71C1C", width=1.5, dash="dot"), hovertemplate="%{y:.0f} °C"))
        series.append(df["sens"])
    graficos.append(preparar(fig, "Temperatura por horas", "°C", _rango_eje(series, 10, 35, 2)))

    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["fecha"], y=df["prec"], name="Precipitación", marker_color=MATERIAL["azul"],
                         hovertemplate="%{y:.1f} mm<extra></extra>"))
    graficos.append(preparar(fig, "Precipitación por horas", "mm", _rango_eje([df["prec"]], 0, 5, 1)))

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["prob_precip"], name="Prob. de precipitación",
                             line=dict(color=MATERIAL["azul_claro"], width=2, shape="hv"), hovertemplate="%{y:.0f} %"))
    if df["prob_tormenta"].fillna(0).gt(0).any():
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["prob_tormenta"], name="Prob. de tormenta",
                                 line=dict(color=MATERIAL["morado"], width=2, shape="hv"), hovertemplate="%{y:.0f} %"))
    graficos.append(preparar(fig, "Probabilidad de lluvia y de tormenta", "%", [0, 100]))

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["viento"], name="Viento", line=dict(color=MATERIAL["indigo"], width=2),
                             customdata=df["dir"].fillna("—"), hovertemplate="%{y:.0f} km/h del %{customdata}"))
    if df["racha"].notna().any():
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["racha"], name="Racha", line=dict(color=MATERIAL["morado"], width=1.5, dash="dot"),
                                 hovertemplate="%{y:.0f} km/h"))
    graficos.append(preparar(fig, "Viento por horas", "km/h", _rango_eje([df["viento"], df["racha"]], 0, 40, 5)))
    return graficos


#: Iconos por código de estado del cielo de AEMET (sin la "n" de noche).
#: Códigos: 11 despejado, 12 poco nuboso, 13 intervalos nubosos, 14 nuboso,
#: 15 muy nuboso, 16 cubierto, 17 nubes altas; 2x lluvia, 3x nieve, 4x lluvia
#: escasa, 5x tormenta, 6x tormenta con lluvia escasa, 7x nieve escasa,
#: 81 niebla, 82 bruma, 83 calima.
ICONOS_CIELO = {"11": "☀️", "12": "🌤️", "13": "⛅", "14": "🌥️", "15": "☁️", "16": "☁️", "17": "🌤️"}
ICONOS_GRUPO_CIELO = {"2": "🌧️", "3": "🌨️", "4": "🌦️", "5": "⛈️", "6": "⛈️", "7": "🌨️", "8": "🌫️"}


def icono_cielo(codigo):
    codigo = str(codigo or "").rstrip("n")
    if codigo in ICONOS_CIELO:
        return ICONOS_CIELO[codigo]
    return ICONOS_GRUPO_CIELO.get(codigo[:1], "🌡️")


def construir_tarjetas_pronostico(df):
    """Una tarjeta por día de la predicción: estado del cielo, máxima y
    mínima, probabilidad de lluvia y viento. En pantallas estrechas cada
    tarjeta se convierte en una fila."""
    if df.empty:
        return ""
    hoy = pd.Timestamp(datetime.now(ZONA_HORARIA).date())
    tarjetas = []
    for fila in df.itertuples():
        dias = (fila.fecha - hoy).days
        nombre = "Hoy" if dias == 0 else "Mañana" if dias == 1 else f"{DIAS_SEMANA[fila.fecha.weekday()].capitalize()} {fila.fecha.day}"
        texto_cielo = html.escape(str(getattr(fila, "cielo_texto", "") or ""))
        icono = icono_cielo(getattr(fila, "cielo", None))
        temps = " / ".join(
            f'<span class="{clase}">{v:.0f}°</span>'
            for v, clase in ((fila.tmax, "t-max"), (fila.tmin, "t-min")) if pd.notna(v)
        )
        lluvia = f"💧 {fila.prob_precip:.0f} %" if pd.notna(fila.prob_precip) else ""
        viento = ""
        if pd.notna(getattr(fila, "viento_max", None)):
            direccion = getattr(fila, "viento_dir", None)
            viento = f"💨 {fila.viento_max:.0f} km/h" + (f" {html.escape(str(direccion))}" if direccion and direccion != "C" else "")
        tarjetas.append(
            f'<div class="dia-pronostico">'
            f'<p class="dia-nombre">{nombre}</p>'
            f'<p class="dia-icono" role="img" aria-label="{texto_cielo or "estado del cielo"}">{icono}</p>'
            f'<p class="dia-cielo">{texto_cielo}</p>'
            f'<p class="dia-temps">{temps}</p>'
            f'<p class="dia-detalle">{lluvia}</p>'
            f'<p class="dia-detalle">{viento}</p>'
            f"</div>"
        )
    return f'<div class="dias-pronostico">{"".join(tarjetas)}</div>'


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
    config = CONFIG_PLOTLY
    layout_comun = dict(template="plotly_white", height=320, margin=dict(t=50, b=40, l=50, r=20))

    def lineas_de_dia(fig):
        fig.update_xaxes(tickformat="%a %-d", hoverformat="%A %-d de %B")
        for fecha in df["fecha"]:
            fig.add_vline(x=fecha, line_width=1, line_dash="dot", line_color=MATERIAL["gris"], opacity=0.3)

    # Temperatura: la máxima se dibuja primero (detrás, texto fuera para que
    # no quede tapado) y la mínima después (delante/encima, texto dentro).
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["fecha"], y=df["tmax"], name="Máxima prevista", marker_color=MATERIAL["rojo"],
        # Texto dentro, arriba: encima de la barra van las marcas de sensación.
        text=_texto_barras(df["tmax"], "°"), textposition="inside", insidetextanchor="end",
    ))
    fig.add_trace(go.Bar(
        x=df["fecha"], y=df["tmin"], name="Mínima prevista", marker_color=MATERIAL["azul"],
        text=_texto_barras(df["tmin"], "°"), textposition="inside",
    ))
    # Sensación térmica prevista por AEMET, como marcas sobre las barras; solo
    # si se aparta de la temperatura en algún día (con calor húmedo o frío
    # con viento), para no recargar el gráfico cuando coinciden.
    series_rango = [df["tmax"], df["tmin"]]
    for col, col_t, nombre, color in [
        ("sens_max", "tmax", "Sensación máx.", "#B71C1C"),
        ("sens_min", "tmin", "Sensación mín.", "#0D47A1"),
    ]:
        if col not in df.columns:
            continue
        distinta = (df[col] - df[col_t]).abs() >= 1
        if distinta.any():
            fig.add_trace(go.Scatter(
                x=df["fecha"], y=df[col].where(distinta), name=nombre, mode="markers",
                marker=dict(symbol="diamond", size=10, color=color, line=dict(color="#FFFFFF", width=1.5)),
                hovertemplate="%{y:.0f} °C",
            ))
            series_rango.append(df[col])
    lineas_de_dia(fig)
    fig.update_yaxes(range=_rango_eje(series_rango, 5, 45, 3), title="°C")
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

    # Índice UV máximo, con los colores y categorías estándar de la OMS.
    if "uv_max" in df.columns and df["uv_max"].notna().any():
        datos = df[df["uv_max"].notna()]
        categorias = [categoria_uv(v) for v in datos["uv_max"]]
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=datos["fecha"], y=datos["uv_max"], name="Índice UV",
            marker=dict(color=[c[1] for c in categorias]),
            text=[f"{v:.0f}" for v in datos["uv_max"]], textposition="outside",
            customdata=[c[0] for c in categorias],
            hovertemplate="UV %{y:.0f} · %{customdata}<extra></extra>",
        ))
        lineas_de_dia(fig)
        fig.update_xaxes(range=[df["fecha"].min() - pd.Timedelta(hours=12), df["fecha"].max() + pd.Timedelta(hours=12)])
        fig.update_yaxes(range=[0, max(12, datos["uv_max"].max() + 2)], title="UV")
        fig.update_layout(title="Índice UV máximo previsto", showlegend=False, **layout_comun)
        graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))
        leyenda = "".join(
            f'<span class="uv-cat"><span class="uv-muestra" style="background:{color};"></span>'
            f'{nombre} ({"≥ " + str(limite_anterior + 1) if limite is None else f"{limite_anterior + 1}–{limite}" if limite_anterior + 1 < limite else limite})</span>'
            for (limite, nombre, color), limite_anterior in zip(CATEGORIAS_UV, [-1] + [c[0] for c in CATEGORIAS_UV[:-1]])
        )
        graficos.append(f'<p class="aviso leyenda-uv">Categorías de la OMS: {leyenda}. AEMET solo da el índice UV de los primeros días.</p>')

    return graficos


#: Categorías del índice UV de la OMS: (límite superior incluido, nombre, color).
CATEGORIAS_UV = [
    (2, "bajo", "#289500"), (5, "moderado", "#F7E400"), (7, "alto", "#F85900"),
    (10, "muy alto", "#D8001D"), (None, "extremo", "#6B49C8"),
]


def categoria_uv(valor):
    for limite, nombre, color in CATEGORIAS_UV:
        if limite is None or valor <= limite:
            return nombre, color


def _slug(texto):
    """Convierte un nombre de estación en un identificador simple (sin
    acentos ni espacios) para usarlo en atributos HTML y en JavaScript."""
    texto = texto.lower()
    for original, sin_acento in {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n", "ü": "u"}.items():
        texto = texto.replace(original, sin_acento)
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto or "estacion"


def punto_de_rocio(t, hr):
    """Punto de rocío (°C) con la fórmula de Magnus, a partir de la
    temperatura (°C) y la humedad relativa (%)."""
    if t is None or hr is None or hr <= 0:
        return None
    a, b = 17.62, 243.12
    g = math.log(hr / 100) + a * t / (b + t)
    return b * g / (a - g)


def sensacion_termica(t, hr, viento_kmh):
    """Temperatura aparente (°C): índice de calor (fórmula de Rothfusz, NOAA)
    con calor y humedad, sensación por viento (fórmula de Environment
    Canada/NOAA) con frío y viento, y la propia temperatura en otro caso."""
    if t is None:
        return None
    if hr is not None and t >= 27 and hr >= 40:
        f = t * 9 / 5 + 32
        hi = (-42.379 + 2.04901523 * f + 10.14333127 * hr - 0.22475541 * f * hr
              - 0.00683783 * f * f - 0.05481717 * hr * hr + 0.00122874 * f * f * hr
              + 0.00085282 * f * hr * hr - 0.00000199 * f * f * hr * hr)
        return (hi - 32) * 5 / 9
    if viento_kmh is not None and t <= 10 and viento_kmh > 4.8:
        v = viento_kmh ** 0.16
        return 13.12 + 0.6215 * t - 11.37 * v + 0.3965 * t * v
    return t


#: Sensación de humedad según el punto de rocío (°C): (límite superior, texto).
CONFORT_ROCIO = [(10, "aire seco"), (16, "agradable"), (18, "algo húmedo"), (21, "bochornoso"), (24, "muy bochornoso"), (None, "opresivo")]


def confort_rocio(td):
    for limite, texto in CONFORT_ROCIO:
        if limite is None or td < limite:
            return texto


SECTORES_VIENTO = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]


def _sector_viento(grados):
    """Sector de 8 rumbos (N, NE, E...) para una dirección en grados, o None."""
    try:
        grados = float(grados)
    except (TypeError, ValueError):
        return None
    if not 0 <= grados <= 360:
        return None
    return SECTORES_VIENTO[int(((grados % 360) + 22.5) // 45) % 8]


HORAS_OBSERVACION_ANTIGUA = 3


def construir_tarjetas_kpi(lectura, mar=None):
    """Tarjetas destacadas con las condiciones observadas más recientes:
    temperatura, viento, humedad y precipitación de la última hora, y la
    temperatura del mar si se ha podido obtener (`mar`, ver
    obtener_temperatura_mar)."""
    if not lectura and not mar:
        return ""
    lectura = lectura or {}

    # AEMET da la hora de la observación en UTC; se muestra en hora local.
    hora, aviso_antiguedad = None, ""
    if lectura.get("fint"):
        try:
            momento = datetime.strptime(lectura["fint"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            hora = _hora_local(momento, "%d/%m a las %H:%M")
            horas = (datetime.now(timezone.utc) - momento).total_seconds() / 3600
            if horas > HORAS_OBSERVACION_ANTIGUA:
                aviso_antiguedad = (
                    f'<p class="aviso aviso-antiguo">⚠ La última observación de la estación es de hace '
                    f"{horas:.0f} horas: los valores de «ahora» pueden no reflejar el tiempo actual.</p>"
                )
        except ValueError:
            hora = lectura["fint"]

    def fmt(valor, unidad, decimales=1):
        return f"{_es(valor, decimales)} {unidad}" if valor is not None else "sin datos"

    vv = lectura.get("vv")
    viento_kmh = vv * 3.6 if vv is not None else None

    # Dirección (de dónde sopla) y racha máxima de la última hora.
    detalles_viento = []
    sector = _sector_viento(lectura.get("dv"))
    if sector and viento_kmh:
        detalles_viento.append(f"del {sector}")
    vmax = lectura.get("vmax")
    if vmax is not None:
        detalles_viento.append(f"racha {vmax * 3.6:.0f} km/h")

    # Sensación térmica (solo si se aparta de la temperatura) y punto de
    # rocío, que dice mejor que la humedad relativa si hay bochorno.
    ta, hr = lectura.get("ta"), lectura.get("hr")
    detalle_temperatura = ""
    sensacion = sensacion_termica(ta, hr, viento_kmh)
    if sensacion is not None and abs(sensacion - ta) >= 1:
        detalle_temperatura = f"sensación {sensacion:.0f} °C"
    rocio = lectura.get("tpr")
    if rocio is None:
        rocio = punto_de_rocio(ta, hr)
    detalle_humedad = f"rocío {rocio:.0f} °C · {confort_rocio(rocio)}" if rocio is not None else ""

    kpis = [
        ("Temperatura ahora", MATERIAL["rojo"], fmt(lectura.get("ta"), "°C"), detalle_temperatura),
        ("Viento ahora", MATERIAL["indigo"], fmt(viento_kmh, "km/h"), " · ".join(detalles_viento)),
        ("Humedad ahora", MATERIAL["teal"], fmt(lectura.get("hr"), "%", 0), detalle_humedad),
        ("Precipitación (última hora)", MATERIAL["azul_claro"], fmt(lectura.get("prec"), "mm"), ""),
    ]
    if mar:
        detalle_mar = ""
        if mar.get("hace_semana") is not None:
            detalle_mar = f"hace una semana {_es(mar['hace_semana'])} °C"
        kpis.append(("Temperatura del mar", MATERIAL["azul"], fmt(mar["ahora"], "°C"), detalle_mar))

    tarjetas = "".join(
        f'<div class="tarjeta-kpi" style="border-top-color:{color};">'
        f"<h3>{etiqueta}</h3>"
        f'<p class="valor-kpi">{valor}</p>'
        + (f'<p class="detalle-kpi">{detalle}</p>' if detalle else "")
        + "</div>"
        for etiqueta, color, valor, detalle in kpis
    )
    nota_hora = f"Última observación: {hora} (hora local)." if hora else ""
    if mar:
        nota_hora += " Mar: análisis del modelo de Copernicus (vía Open-Meteo) frente a la costa, no una medición."
    nota_hora = f'<p class="aviso">{nota_hora.strip()}</p>' if nota_hora else ""
    return f'{aviso_antiguedad}<div class="tarjetas-kpi">{tarjetas}</div>{nota_hora}'


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


def _texto_periodo(periodo):
    return f"periodo {periodo}" if periodo else "periodo de referencia de AEMET"


def construir_tarjetas_anomalia(mes_completo, normales_registros, periodo=None):
    """Compara el último mes calendario completo disponible en el histórico
    (`mes_completo`, resultado de _mes_completo_mas_reciente) frente a los
    valores climatológicos normales de ese mismo mes, y lo clasifica
    (frío/normal/cálido, seco/normal/húmedo...) según los quintiles de la
    serie de referencia."""
    if not mes_completo or not normales_registros:
        print(f"Diagnóstico anomalía: mes completo={bool(mes_completo)}, normales vacíos={not normales_registros}")
        return ""
    anio, mes, df_mes = mes_completo

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
        texto_diferencia = f"{signo}{_es(diferencia, decimales)} {unidad} vs. {referencia}"
        if porcentaje and valor_normal > 0:
            texto_diferencia += f" ({100 * valor_real / valor_normal:.0f} % de la {referencia})"
        # Años con dato en la serie de referencia: no todas las variables
        # tienen los 30 (p. ej. la humedad suele tener menos).
        anios = _num(normal.get(f"{prefijo}_n"))
        filas.append((
            etiqueta, color,
            f"{_es(valor_real, decimales)} {unidad}",
            texto_diferencia,
            clasificar(valor_real, normal, prefijo, clases),
            f"{anios:.0f} años de referencia" if anios else "",
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
    if "sol" in df_mes.columns and df_mes["sol"].notna().all():
        # Solo con el mes entero: muchas estaciones no miden el sol todos los días.
        comparar("Horas de sol (media diaria)", MATERIAL["ambar"], df_mes["sol"].mean(), "inso", "md", "h", CLASES_GENERICAS)

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
        + (f"<p class='fecha'>{anios}</p>" if anios else "")
        + "</div>"
        for etiqueta, color, valor, diferencia, clase, anios in filas
    )
    titulo = f'<h3 class="subtitulo">{NOMBRES_MES[mes].capitalize()} de {anio} frente a lo normal</h3>'
    explicacion = (
        '<p class="aviso">Último mes completo comparado con los valores normales de AEMET para '
        f'esta estación ({_texto_periodo(periodo)}). La etiqueta sitúa el mes entre los años '
        'de referencia: «Normal» es el 20 % central; «Cálido/Frío» (o «Húmedo/Seco», «Alto/Bajo»), '
        'el 20 % siguiente por cada lado; «Muy…», el 20 % más extremo; y «Extremadamente…», fuera '
        'de todo lo registrado en el periodo.</p>'
    )
    return f'{titulo}{explicacion}<div class="tarjetas">{tarjetas}</div>'


#: (etiqueta, color, columna, condición, campo de días normales o None,
#: mostrar siempre). Los campos normales de AEMET: nt_30 = días con máxima
#: ≥ 30 °C, nt_00 = días con mínima ≤ 0 °C, np_010/np_100/np_300 = días con
#: precipitación ≥ 1/10/30 mm, nw_55 = días con racha ≥ 55 km/h. Las noches
#: tropicales y tórridas no tienen valor normal en AEMET.
DIAS_SENALADOS = [
    ("Días de calor (máx. ≥ 30 °C)", MATERIAL["rojo"], "tmax", lambda s: s >= 30, "nt_30", True),
    ("Días muy calurosos (máx. ≥ 35 °C)", MATERIAL["rojo"], "tmax", lambda s: s >= 35, None, False),
    ("Noches tropicales (mín. ≥ 20 °C)", MATERIAL["morado"], "tmin", lambda s: s >= 20, None, True),
    ("Noches tórridas (mín. ≥ 25 °C)", MATERIAL["morado"], "tmin", lambda s: s >= 25, None, False),
    ("Días de helada (mín. ≤ 0 °C)", MATERIAL["azul"], "tmin", lambda s: s <= 0, "nt_00", False),
    ("Días de lluvia (≥ 1 mm)", MATERIAL["azul_claro"], "prec", lambda s: s >= 1, "np_010", True),
    ("Días de lluvia fuerte (≥ 10 mm)", MATERIAL["azul_claro"], "prec", lambda s: s >= 10, "np_100", False),
    ("Días de lluvia muy fuerte (≥ 30 mm)", MATERIAL["azul_claro"], "prec", lambda s: s >= 30, "np_300", False),
    ("Días de rachas fuertes (≥ 55 km/h)", MATERIAL["indigo"], "racha", lambda s: s >= 55, "nw_55", False),
]


def construir_dias_senalados(mes_completo, normales_registros):
    """Recuento de días señalados (calor, noches tropicales, lluvia...) del
    último mes completo, junto al número medio de esos días en un mes igual
    del periodo normal. Los umbrales poco habituales solo se muestran si ese
    mes hubo alguno o si lo normal es tenerlos."""
    if not mes_completo:
        return ""
    anio, mes, df_mes = mes_completo
    normal = normales_por_mes(normales_registros).get(mes, {})

    tarjetas = []
    for etiqueta, color, col, condicion, campo_normal, siempre in DIAS_SENALADOS:
        if col not in df_mes.columns or df_mes[col].isna().all():
            continue
        dias = int(condicion(df_mes[col].dropna()).sum())
        valor_normal = _num(normal.get(f"{campo_normal}_md")) if campo_normal else None
        if not siempre and dias == 0 and not (valor_normal and valor_normal >= 0.1):
            continue
        faltan = int(df_mes[col].isna().sum())
        detalle = f"normal: {_es(valor_normal)}" if valor_normal is not None else "sin valor normal en AEMET"
        if faltan:
            detalle += f" · {faltan} día(s) sin dato"
        tarjetas.append(
            f'<div class="tarjeta tarjeta-dias" style="border-top-color:{color};">'
            f"<h3>{etiqueta}</h3>"
            f'<p class="valor-dias">{dias}</p>'
            f"<p class='fecha'>{detalle}</p>"
            f"</div>"
        )
    if not tarjetas:
        return ""
    return (
        f'<h3 class="subtitulo">Días señalados en {NOMBRES_MES[mes]} de {anio}</h3>'
        f'<div class="tarjetas">{"".join(tarjetas)}</div>'
    )


def precipitacion_normal_acumulada(fechas, normales_registros, inicio):
    """Precipitación normal acumulada desde `inicio` hasta cada fecha: suma
    de las medias mensuales (p_mes_md) de los meses ya completos más la
    parte proporcional del mes en curso. Se usan medias, no medianas,
    porque las medias sí se pueden sumar."""
    por_mes = normales_por_mes(normales_registros)
    medias = {m: _num(r.get("p_mes_md")) for m, r in por_mes.items()}
    if len(medias) < 12 or any(v is None for v in medias.values()):
        return None
    resultado = []
    for f in fechas:
        total, cursor = 0.0, date(inicio.year, inicio.month, 1)
        while (cursor.year, cursor.month) < (f.year, f.month):
            total += medias[cursor.month]
            cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
        total += medias[f.month] * f.day / calendar.monthrange(f.year, f.month)[1]
        resultado.append(total)
    return resultado


def construir_anio_hidrologico(df_completo, normales_registros):
    """Precipitación acumulada desde el 1 de octubre (inicio del año
    hidrológico) comparada con la acumulada normal a la misma fecha: el
    indicador habitual de sequía en España. Devuelve una lista de fragmentos
    HTML (resumen + gráfico) o una lista vacía."""
    if df_completo.empty or "prec" not in df_completo.columns:
        return []
    ultima = df_completo["fecha"].max().date()
    inicio = inicio_anio_hidrologico(ultima)
    dias = pd.date_range(inicio, ultima, freq="D")
    prec = df_completo.set_index(df_completo["fecha"].dt.normalize())["prec"]
    prec = prec[~prec.index.duplicated()].reindex(dias)
    faltan = int(prec.isna().sum())
    acumulado = prec.fillna(0).cumsum()
    normal = precipitacion_normal_acumulada([d.date() for d in dias], normales_registros, inicio)

    nombre_anio = f"{inicio.year}-{str(inicio.year + 1)[2:]}"
    total = acumulado.iloc[-1]
    resumen = f"<p><strong>{total:.0f} mm</strong> desde el 1 de octubre de {inicio.year} (hasta el {ultima:%d/%m/%Y})"
    if normal:
        normal_hoy = normal[-1]
        if normal_hoy > 0:
            resumen += f": un <strong>{100 * total / normal_hoy:.0f} %</strong> de lo normal a esta fecha ({normal_hoy:.0f} mm)"
        normal_anual = precipitacion_normal_acumulada([date(inicio.year + 1, 9, 30)], normales_registros, inicio)[0]
        resumen += f". Lo normal en un año hidrológico completo son {normal_anual:.0f} mm."
    else:
        resumen += "."
    resumen += "</p>"
    if faltan:
        resumen += f'<p class="aviso">Faltan {faltan} día(s) sin dato de precipitación: el acumulado real puede ser algo mayor.</p>'

    fig = go.Figure()
    if normal:
        fig.add_trace(go.Scatter(
            x=dias, y=normal, name="Normal acumulada",
            line=dict(color=MATERIAL["gris"], width=2, dash="dash"),
            hovertemplate="%{y:.0f} mm",
        ))
    fig.add_trace(go.Scatter(
        x=dias, y=acumulado, name=f"Acumulada {nombre_anio}",
        line=dict(color=MATERIAL["azul"], width=2, shape="hv"),
        hovertemplate="%{y:.0f} mm",
    ))
    series = [acumulado] + ([pd.Series(normal)] if normal else [])
    fig.update_yaxes(range=_rango_eje(series, 0, 100, 20), title="mm")
    fig.update_layout(
        title="Precipitación acumulada del año hidrológico", hovermode="x unified",
        xaxis=dict(tickformat="%b %Y", hoverformat="%-d %b %Y"),
        legend=dict(orientation="h", y=-0.25), template="plotly_white", height=360,
        margin=dict(t=50, b=40, l=50, r=20),
    )
    return [
        f'<h3 class="subtitulo">Año hidrológico {nombre_anio}</h3>{resumen}',
        fig.to_html(full_html=False, include_plotlyjs=False, config=CONFIG_PLOTLY),
    ]


#: Clases de velocidad de la racha (km/h). Se colorean con una rampa
#: secuencial de un solo tono (índigo): en modo claro de claro (flojo) a
#: oscuro (fuerte), y en modo oscuro al revés, para que el tramo más fuerte
#: no se pierda contra el fondo. El JavaScript de la página cambia de rampa
#: al cambiar de tema.
CLASES_RACHA = [
    (0, 20, "< 20 km/h"),
    (20, 40, "20–40 km/h"),
    (40, 60, "40–60 km/h"),
    (60, None, "≥ 60 km/h"),
]
RAMPA_RACHA = {
    "light": ["#C5CAE9", "#7986CB", "#3F51B5", "#1A237E"],
    "dark": ["#3F51B5", "#7986CB", "#B0B8E6", "#EEF0FA"],
}


def construir_rosa_vientos(df):
    """Rosa de los vientos con la dirección de la racha máxima de cada día
    (el campo 'dir' de AEMET, en decenas de grado; 99 = dirección variable),
    apilada por intensidad. Al pasar el ratón se ve también la máxima media
    de los días de cada sector: en Valencia, las rachas de poniente (O–NO)
    suelen coincidir con los días más calurosos y las de levante (E–SE)
    con la brisa marina."""
    if df.empty or "dir" not in df.columns or "racha" not in df.columns:
        return ""
    grados = pd.to_numeric(df["dir"], errors="coerce") * 10
    datos = pd.DataFrame({"grados": grados, "racha": df["racha"], "tmax": df.get("tmax")})
    datos = datos[(datos["grados"] >= 0) & (datos["grados"] <= 360) & datos["racha"].notna()]
    if datos.empty:
        return ""
    datos["sector"] = datos["grados"].map(_sector_viento)
    tmax_media = datos.groupby("sector")["tmax"].mean()

    fig = go.Figure()
    for (minimo, maximo, etiqueta), color in zip(CLASES_RACHA, RAMPA_RACHA["light"]):
        en_clase = datos[(datos["racha"] >= minimo) & ((datos["racha"] < maximo) if maximo else True)]
        conteo = en_clase["sector"].value_counts().reindex(SECTORES_VIENTO, fill_value=0)
        fig.add_trace(go.Barpolar(
            r=conteo.values, theta=SECTORES_VIENTO, name=etiqueta,
            marker=dict(color=color, line=dict(color="rgba(255,255,255,0.9)", width=1)),
            customdata=[f"{_es(tmax_media[s])} °C" if s in tmax_media and pd.notna(tmax_media[s]) else "—" for s in SECTORES_VIENTO],
            hovertemplate="%{theta}: %{r} día(s) con racha " + etiqueta + "<br>Máx. media de los días de este sector: %{customdata}<extra></extra>",
        ))
    fig.update_layout(
        title="Dirección de la racha máxima diaria",
        template="plotly_white", height=420, margin=dict(t=60, b=40, l=40, r=40),
        legend=dict(orientation="h", y=-0.12),
        polar=dict(
            angularaxis=dict(direction="clockwise", rotation=90),
            # El eje de días va entre N y NE para no tapar el sector norte.
            radialaxis=dict(ticksuffix=" d", angle=67.5, tickangle=67.5, tickfont=dict(size=10)),
        ),
    )
    variable = int((pd.to_numeric(df["dir"], errors="coerce") == 99).sum())
    nota = f" {variable} día(s) con dirección variable no aparecen en la rosa." if variable else ""
    return (
        fig.to_html(full_html=False, include_plotlyjs=False, config=CONFIG_PLOTLY)
        + f'<p class="aviso">Cada día cuenta una vez, en el sector desde el que sopló su racha más fuerte.{nota}</p>'
    )

#: (clave, columna del histórico/predicción, etiqueta, nombre corto,
#: ¿récord por abajo?, margen para "cerca del récord").
RECORDS_MOSTRADOS = [
    ("tmax", "tmax", "Temperatura más alta", "máxima", False, 1.0),
    ("tmin", "tmin", "Temperatura más baja", "mínima", True, 1.0),
    ("prec", "prec", "Más lluvia en un día", "lluvia", False, None),  # "cerca" = 80 % del récord
]


def _fecha_record(record, mes):
    dia, anio = record.get("dia"), record.get("anio")
    if anio is None:
        return ""
    return f"{dia:.0f}/{mes:02d}/{anio:.0f}" if dia else f"{anio:.0f}"


def _comparar_con_record(valor, record, por_abajo, margen):
    """'supera', 'cerca' o None, comparando un valor con un récord."""
    if valor is None or pd.isna(valor) or record is None:
        return None
    if (valor < record) if por_abajo else (valor > record):
        return "supera"
    if margen is None:
        cerca = not por_abajo and record > 0 and valor >= 0.8 * record
    else:
        cerca = (valor <= record + margen) if por_abajo else (valor >= record - margen)
    return "cerca" if cerca else None


def notas_records(df, extremos, prevision=False):
    """Frases sobre días (del histórico o de la predicción) que superan o
    se quedan cerca de un récord mensual de la estación."""
    if df.empty or not extremos:
        return []
    notas = []
    for clave, col, _, nombre, por_abajo, margen in RECORDS_MOSTRADOS:
        if clave not in extremos or col not in df.columns:
            continue
        for fecha, valor in zip(df["fecha"], df[col]):
            record = extremos[clave][fecha.month - 1]
            resultado = _comparar_con_record(valor, record["valor"], por_abajo, margen)
            if not resultado:
                continue
            fecha_record = _fecha_record(record, fecha.month)
            if not prevision and fecha_record == f"{fecha.day}/{fecha.month:02d}/{fecha.year}":
                texto = "estableció el récord de"
            elif resultado == "supera":
                texto = "superaría el récord de" if prevision else "superó el récord de"
            else:
                texto = "se quedaría cerca del récord de" if prevision else "se quedó cerca del récord de"
            unidad = record["unidad"]
            notas.append((
                fecha, resultado,
                f"{DIAS_SEMANA[fecha.weekday()].capitalize()} {fecha:%d/%m}: {nombre} de {_es(valor)} {unidad}"
                f"{' prevista' if prevision and clave != 'prec' else ''}, {texto} {NOMBRES_MES[fecha.month]} "
                f"({_es(record['valor'])} {unidad}{', ' + fecha_record if fecha_record else ''})",
            ))
    notas.sort(key=lambda n: (n[1] != "supera", -n[0].value))
    return [texto for _, _, texto in notas[:6]]


def construir_records(extremos, df_hist, mes):
    """Récords de la estación para el mes en curso y, si los hay, los días
    recientes que los superaron o se quedaron cerca."""
    if not extremos:
        return ""
    tarjetas = []
    colores = {"tmax": MATERIAL["rojo"], "tmin": MATERIAL["azul"], "prec": MATERIAL["azul_claro"]}
    for clave, _, etiqueta, _, _, _ in RECORDS_MOSTRADOS:
        if clave not in extremos:
            continue
        record = extremos[clave][mes - 1]
        if record["valor"] is None:
            continue
        tarjetas.append(
            f'<div class="tarjeta" style="border-top-color:{colores[clave]};">'
            f"<h3>{etiqueta}</h3>"
            f'<p class="valor-dias">{_es(record["valor"])} {record["unidad"]}</p>'
            f"<p class='fecha'>{_fecha_record(record, mes)}</p>"
            f"</div>"
        )
    if not tarjetas:
        return ""
    notas = notas_records(df_hist, extremos)
    lista = "".join(f"<li>{n}</li>" for n in notas)
    return (
        f'<h3 class="subtitulo">Récords de {NOMBRES_MES[mes]} en esta estación</h3>'
        f'<div class="tarjetas">{"".join(tarjetas)}</div>'
        + (f'<ul class="notas-records">{lista}</ul>' if lista else "")
    )


def construir_recuadro_extremos(df, variables):
    """Tarjetas con el valor más alto y más bajo de cada variable de
    `variables` (ver VARIABLES_EXTREMOS_HISTORICO / _PREDICCION más arriba)."""
    if df.empty:
        return ""

    tarjetas = []
    for etiqueta, color, col_max, col_min, unidad in variables:
        alto = _extremo(df, col_max, "max")
        bajo = _extremo(df, col_min, "min")
        alto_html = f"{_es(alto[0])} {unidad} <span class='fecha'>({alto[1]})</span>" if alto else "sin datos"
        bajo_html = f"{_es(bajo[0])} {unidad} <span class='fecha'>({bajo[1]})</span>" if bajo else "sin datos"
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
    temperaturas_ahora = {}  # para mostrarlas en los marcadores del mapa
    generado = _hora_local(datetime.now(timezone.utc), "%d/%m/%Y a las %H:%M (hora peninsular)")

    avisos_por_area = {}  # se descargan una sola vez por área, aunque la compartan varias estaciones
    mar_por_punto = {}  # ídem para la temperatura del mar

    for estacion in STATIONS:
        idema = estacion.get("idema")
        nombre = estacion["nombre"]
        # df_hist_completo llega hasta el inicio del año hidrológico (para
        # la lluvia acumulada); df_hist es solo la ventana de DIAS_HISTORICO
        # días que se muestra en los gráficos y en los extremos.
        df_hist_completo, df_hist, df_pred = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
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
            historico = obtener_historico(
                idema, DIAS_HISTORICO, desde=inicio_anio_hidrologico(fecha_fin_historico())
            )
            df_hist_completo = historico_a_dataframe(historico)
            if not df_hist_completo.empty:
                inicio_ventana = pd.Timestamp(fecha_fin_historico() - timedelta(days=DIAS_HISTORICO))
                df_hist = df_hist_completo[df_hist_completo["fecha"] >= inicio_ventana]
        except Exception as exc:
            print(f"Aviso: no se pudo obtener el histórico de {nombre}: {exc}")

        normales, periodo = [], None
        try:
            if idema:
                normales = obtener_normales(idema)
                periodo = periodo_normales(idema)
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

        time.sleep(2)

        # Predicción por horas (próximas 48 h), con la misma reserva.
        df_horas, noches = pd.DataFrame(), []
        pred_horaria = None
        try:
            print(f"Procesando predicción horaria de {nombre}...")
            pred_horaria = obtener_prediccion_horaria(municipio)
            if prediccion_horaria_a_dataframe(pred_horaria)[0].empty:
                print(f"Aviso: la predicción horaria de {nombre} salió vacía. Claves recibidas: {list(pred_horaria.keys())}.")
                pred_horaria = None
            else:
                _guardar_ultimo(f"prediccion_horaria_{municipio}", pred_horaria)
        except Exception as exc:
            print(f"Aviso: no se pudo obtener la predicción horaria de {nombre}: {exc}")
        if pred_horaria is None:
            pred_horaria, guardado = _leer_ultimo(f"prediccion_horaria_{municipio}")
            if pred_horaria:
                notas_antiguedad.append(f"la predicción por horas (se muestra la descargada el {_hora_local(guardado)})")
        if pred_horaria:
            df_horas, noches = prediccion_horaria_a_dataframe(pred_horaria)

        # Récords de la estación (caché mensual).
        extremos = {}
        try:
            if idema:
                extremos = obtener_extremos(idema)
        except Exception as exc:
            print(f"Aviso: no se pudieron obtener los récords de {nombre}: {exc}")

        # Temperatura del mar en el punto de costa configurado.
        mar = None
        punto_mar = estacion.get("mar")
        if punto_mar:
            clave_mar = (punto_mar["lat"], punto_mar["lon"])
            if clave_mar not in mar_por_punto:
                try:
                    print(f"Procesando temperatura del mar en {clave_mar}...")
                    mar_por_punto[clave_mar] = obtener_temperatura_mar(*clave_mar)
                    _guardar_ultimo(f"mar_{clave_mar[0]}_{clave_mar[1]}", mar_por_punto[clave_mar])
                except Exception as exc:
                    print(f"Aviso: no se pudo obtener la temperatura del mar: {exc}")
                    mar_por_punto[clave_mar] = _leer_ultimo(f"mar_{clave_mar[0]}_{clave_mar[1]}")[0]
            mar = mar_por_punto[clave_mar]

        slug = _slug(nombre)
        estaciones_menu.append((slug, nombre, estacion.get("lat"), estacion.get("lon")))
        temperaturas_ahora[slug] = (lectura_actual or {}).get("ta")
        seccion = [f'<section class="estacion" data-estacion="{slug}"><h2>{html.escape(nombre)}</h2>']
        seccion.append(banner_avisos)
        if notas_antiguedad:
            seccion.append(
                '<p class="aviso aviso-antiguo">⚠ AEMET no respondió en esta actualización para '
                + " ni para ".join(notas_antiguedad) + ".</p>"
            )
        seccion.append(construir_tarjetas_kpi(lectura_actual, mar))

        # Extremos previstos arriba (donde antes estaban los históricos);
        # pronóstico primero (con fondo propio), histórico después con sus
        # propios extremos históricos.
        seccion.append(construir_recuadro_extremos(df_pred, VARIABLES_EXTREMOS_PREDICCION))

        seccion.append('<div class="bloque-pronostico">')
        notas_prevision = notas_records(df_pred, extremos, prevision=True)
        if notas_prevision:
            seccion.append(
                '<ul class="notas-records">' + "".join(f"<li>{n}</li>" for n in notas_prevision) + "</ul>"
            )
        graficos_horas = construir_graficos_horarios(df_horas, noches)
        if graficos_horas:
            seccion.append('<details class="bloque-plegable" open><summary class="subtitulo">Próximas 48 horas</summary>')
            seccion.append('<div class="graficos-apilados">')
            seccion.extend(graficos_horas)
            seccion.append('</div>')
            seccion.append('<p class="aviso">La franja sombreada es la noche.</p></details>')
        seccion.append('<h3 class="subtitulo">Pronóstico (7 días)</h3>')
        seccion.append(construir_tarjetas_pronostico(df_pred))
        seccion.append('<details class="bloque-plegable"><summary class="subtitulo">Gráficos de los 7 días</summary>')
        seccion.append('<div class="graficos-apilados">')
        seccion.extend(construir_graficos_prediccion(df_pred))
        seccion.append('</div></details>')
        seccion.append('</div>')

        seccion.append('<h3 class="subtitulo">Histórico</h3>')
        seccion.append(construir_recuadro_extremos(df_hist, VARIABLES_EXTREMOS_HISTORICO))
        mes_completo = _mes_completo_mas_reciente(df_hist_completo)
        seccion.append(construir_tarjetas_anomalia(mes_completo, normales, periodo))
        seccion.append(construir_dias_senalados(mes_completo, normales))
        seccion.append(construir_records(extremos, df_hist, datetime.now(ZONA_HORARIA).month))
        seccion.append('<div class="graficos-apilados">')
        seccion.extend(construir_graficos_historico(df_hist, normales))
        seccion.append(construir_rosa_vientos(df_hist))
        seccion.append('</div>')

        anio_hidrologico = construir_anio_hidrologico(df_hist_completo, normales)
        if anio_hidrologico:
            seccion.append('<div class="graficos-apilados">')
            seccion.extend(anio_hidrologico)
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
        {"slug": slug, "nombre": nombre, "lat": lat, "lon": lon,
         "temp": f"{_es(temperaturas_ahora[slug])} °C" if temperaturas_ahora.get(slug) is not None else None}
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="apple-touch-icon" href="icon-192.png">
<meta name="theme-color" content="#3F51B5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="AEMET Dashboard">
<script src="{PLOTLY_JS_URL}"></script>
<script>if (window.Plotly) {{ {LOCALE_ES_JS} }}</script>
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
.tarjeta-kpi {{ box-sizing: border-box; flex: 1 1 140px; background: var(--superficie); border-radius: 6px; border-top: 4px solid; padding: 1rem; box-shadow: 0 1px 3px var(--sombra); text-align: center; }}
.tarjeta-kpi h3 {{ margin: 0 0 0.5rem; font-size: 0.8rem; font-weight: 500; color: var(--texto-secundario); }}
.valor-kpi {{ margin: 0; font-size: 1.7rem; font-weight: 700; }}
.detalle-kpi {{ margin: 0.3rem 0 0; font-size: 0.85rem; color: var(--texto-secundario); }}
.tarjeta-dias {{ flex-basis: 170px; }}
.notas-records {{ margin: 0.25rem 0 1rem; padding-left: 1.25rem; font-size: 0.9rem; }}
.notas-records li {{ margin: 0.2rem 0; }}
.bloque-plegable {{ margin-bottom: 1rem; }}
.bloque-plegable > summary {{ cursor: pointer; margin: 0 0 0.5rem; }}
.dias-pronostico {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(105px, 1fr)); gap: 0.5rem; margin: 0.5rem 0 1rem; }}
.dia-pronostico {{ background: var(--superficie); border-radius: 8px; padding: 0.6rem 0.4rem; text-align: center; box-shadow: 0 1px 3px var(--sombra); }}
.dia-pronostico p {{ margin: 0.15rem 0; }}
.dia-nombre {{ font-weight: 500; }}
.dia-icono {{ font-size: 2rem; line-height: 1.2; }}
.dia-cielo {{ font-size: 0.75rem; color: var(--texto-secundario); min-height: 2em; }}
.dia-temps {{ font-size: 1.05rem; }}
.t-max {{ font-weight: 700; }}
.t-min {{ color: var(--texto-secundario); }}
.dia-detalle {{ font-size: 0.8rem; color: var(--texto-secundario); }}
.leyenda-uv {{ display: flex; flex-wrap: wrap; gap: 0.3rem 0.9rem; align-items: center; }}
.uv-cat {{ display: inline-flex; align-items: center; gap: 0.3rem; font-style: normal; }}
.uv-muestra {{ width: 0.8rem; height: 0.8rem; border-radius: 2px; display: inline-block; }}
.valor-dias {{ font-size: 1.6rem !important; font-weight: 700; margin: 0.1rem 0 !important; }}
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
    .dias-pronostico {{ grid-template-columns: 1fr; gap: 0.35rem; }}
    .dia-pronostico {{ display: grid; grid-template-columns: 4.5rem 2.2rem 1fr auto; grid-template-areas: "nombre icono cielo temps" "nombre icono detalle detalle2"; align-items: center; text-align: left; column-gap: 0.5rem; padding: 0.4rem 0.7rem; }}
    .dia-nombre {{ grid-area: nombre; }}
    .dia-icono {{ grid-area: icono; font-size: 1.6rem; }}
    .dia-cielo {{ grid-area: cielo; min-height: 0; }}
    .dia-temps {{ grid-area: temps; text-align: right; }}
    .dia-detalle:nth-of-type(5) {{ grid-area: detalle; }}
    .dia-detalle:nth-of-type(6) {{ grid-area: detalle2; text-align: right; }}
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

    var rampaRacha = {json.dumps(RAMPA_RACHA)};

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
            var actualizacion = {{ paper_bgcolor: c.fondo, plot_bgcolor: c.fondo, 'font.color': c.texto, separators: ',.' }};
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
            if (div.layout.polar) {{
                var rampa = rampaRacha[tema] || rampaRacha.light;
                var indices = [];
                (div.data || []).forEach(function(traza, i) {{ if (traza.type === 'barpolar') indices.push(i); }});
                if (indices.length) {{
                    Plotly.restyle(div, {{
                        'marker.color': indices.map(function(_, j) {{ return rampa[j % rampa.length]; }}),
                        'marker.line.color': c.fondo,
                    }}, indices);
                }}
                actualizacion['polar.bgcolor'] = c.fondo;
                ['radialaxis', 'angularaxis'].forEach(function(eje) {{
                    actualizacion['polar.' + eje + '.gridcolor'] = c.rejilla;
                    actualizacion['polar.' + eje + '.linecolor'] = c.rejilla;
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
        document.dispatchEvent(new CustomEvent('cambio-tema', {{ detail: nuevo }}));
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
    // Teselas claras u oscuras (CARTO) según el tema de la página.
    var atribucion = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';
    var capas = {{
        light: L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{ attribution: atribucion, maxZoom: 19 }}),
        dark: L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{ attribution: atribucion, maxZoom: 19 }}),
    }};
    var capaActual = null;
    function ponerCapa(tema) {{
        var nueva = capas[tema === 'dark' ? 'dark' : 'light'];
        if (nueva === capaActual) return;
        if (capaActual) mapa.removeLayer(capaActual);
        capaActual = nueva.addTo(mapa);
    }}
    ponerCapa(document.documentElement.getAttribute('data-theme'));
    document.addEventListener('cambio-tema', function(e) {{ ponerCapa(e.detail); }});

    var grupo = L.featureGroup();
    estaciones.forEach(function(est) {{
        var marcador = L.marker([est.lat, est.lon]).bindPopup(est.nombre);
        if (est.temp) {{
            marcador.bindTooltip(est.nombre + ': ' + est.temp, {{ permanent: true, direction: 'top', offset: [-15, -12] }});
        }}
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

// Al desplegar un bloque plegable, los gráficos de dentro recalculan su tamaño.
document.querySelectorAll('details.bloque-plegable').forEach(function(bloque) {{
    bloque.addEventListener('toggle', function() {{
        if (!bloque.open || !window.Plotly) return;
        bloque.querySelectorAll('.plotly-graph-div').forEach(function(div) {{ Plotly.Plots.resize(div); }});
    }});
}});

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
