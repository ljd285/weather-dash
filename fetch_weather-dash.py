"""
Genera un dashboard HTML estático con datos climatológicos históricos y
predicción de AEMET OpenData para las estaciones/municipios definidos en
config.py.

Uso:
    export AEMET_API_KEY="tu_clave"
    python fetch_aemet_dashboard.py

El resultado se escribe en docs/index.html (esa carpeta es la que se publica
como GitHub Pages, ver README.md).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import plotly.graph_objects as go
#from plotly.subplots import make_subplots

from config import STATIONS, DIAS_HISTORICO

BASE_URL = "https://opendata.aemet.es/opendata/api"
API_KEY = os.environ.get("AEMET_API_KEY")

if not API_KEY:
    sys.exit(
        "ERROR: define la variable de entorno AEMET_API_KEY con tu clave "
        "gratuita de AEMET OpenData (https://opendata.aemet.es/centrodedescargas/altaUsuario)."
    )

HEADERS = {"api_key": API_KEY, "Accept": "application/json"}

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


def aemet_get(endpoint, retries=5):
    """Llama a un endpoint de AEMET.

    AEMET responde primero con un JSON pequeño que contiene la URL real de
    los datos (campo 'datos'), así que hace falta una segunda petición para
    obtener el contenido de verdad.

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
        if meta.get("estado") != 200:
            raise RuntimeError(f"AEMET devolvió un error: {meta}")
        data_resp = requests.get(meta["datos"], timeout=30)
        data_resp.raise_for_status()
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
    fechas_cache = [r["fecha"][:10] for r in cache if "fecha" in r]
    ultima_fecha_cache = max(fechas_cache) if fechas_cache else None

    nuevos = []
    if ultima_fecha_cache and ultima_fecha_cache >= fecha_fin_objetivo.isoformat():
        print(f"Caché al día para la estación {idema} (hasta {ultima_fecha_cache}); no se pide nada nuevo a AEMET.")
    else:
        if ultima_fecha_cache:
            fecha_ini_peticion = max(
                fecha_ini_objetivo,
                datetime.strptime(ultima_fecha_cache, "%Y-%m-%d").date() + timedelta(days=1),
            )
        else:
            fecha_ini_peticion = fecha_ini_objetivo

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


def construir_graficos_historico(df):
    """Devuelve una lista de fragmentos HTML, uno por variable (temperatura,
    precipitación, viento, humedad), cada uno pensado para ocupar el ancho
    completo de la página (en vez de un único gráfico con 4 paneles). Cada
    uno incluye botones de rango rápido (7d/30d/90d/Todo)."""
    if df.empty:
        return ['<p class="aviso">No se pudieron cargar datos históricos en esta ejecución.</p>']

    graficos = []
    config = {"responsive": True}
    layout_comun = dict(template="plotly_white", height=360, margin=dict(t=70, b=40, l=50, r=20))

    # Temperatura
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmax"], name="Máxima", line=dict(color=MATERIAL["rojo"], width=2)))
    fig.add_trace(go.Scatter(x=df["fecha"], y=df["tmin"], name="Mínima", line=dict(color=MATERIAL["azul"], width=2)))
    fig.update_xaxes(rangeselector=_rangeselector())
    fig.update_yaxes(range=[5, 45], title="°C")
    fig.update_layout(title="Temperatura", legend=dict(orientation="h", y=-0.25), **layout_comun)
    graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Precipitación
    if "prec" in df.columns:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["fecha"], y=df["prec"], name="Precipitación", marker_color=MATERIAL["azul_claro"]))
        fig.update_xaxes(rangeselector=_rangeselector())
        fig.update_yaxes(range=[0, 50], title="mm")
        fig.update_layout(title="Precipitación", showlegend=False, **layout_comun)
        graficos.append(fig.to_html(full_html=False, include_plotlyjs=False, config=config))

    # Viento
    fig = go.Figure()
    if "velmedia" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["velmedia"], name="Vel. media", line=dict(color=MATERIAL["indigo"], width=2)))
    if "racha" in df.columns:
        fig.add_trace(go.Scatter(x=df["fecha"], y=df["racha"], name="Racha máx.", line=dict(color=MATERIAL["morado"], width=1.5, dash="dot")))
    fig.update_xaxes(rangeselector=_rangeselector())
    fig.update_yaxes(range=[0, 80], title="km/h")
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
    fig.update_yaxes(range=[5, 45], title="°C")
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
    fig.update_yaxes(range=[0, 50], title="km/h")
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
    """Devuelve (año, mes, datos_de_ese_mes) del último mes calendario
    completo cubierto por el histórico, o None si no hay ninguno todavía."""
    hoy = datetime.utcnow().date()
    ultimo_dia_mes_anterior = hoy.replace(day=1) - timedelta(days=1)
    anio, mes = ultimo_dia_mes_anterior.year, ultimo_dia_mes_anterior.month
    df_mes = df_hist[(df_hist["fecha"].dt.year == anio) & (df_hist["fecha"].dt.month == mes)]
    if df_mes.empty:
        return None
    return anio, mes, df_mes


def construir_tarjetas_anomalia(df_hist, normales_registros):
    """Compara el último mes calendario completo disponible en el histórico
    frente a los valores climatológicos normales (media histórica de largo
    plazo) de ese mismo mes."""
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
            f"meses reconocidos en los normales: {sorted(meses_disponibles.keys())}. "
            f"Fechas 'fecha' originales recibidas: {[r.get('fecha') for r in normales_registros]}"
        )
        return ""

    filas = []

    def comparar(etiqueta, color, valor_real, valor_normal, unidad, decimales=1):
        if valor_real is None or valor_normal is None or pd.isna(valor_real):
            return
        diferencia = valor_real - valor_normal
        signo = "+" if diferencia >= 0 else ""
        filas.append((
            etiqueta, color,
            f"{valor_real:.{decimales}f} {unidad}",
            f"{signo}{diferencia:.{decimales}f} {unidad} vs. normal",
        ))

    if "tmed" in df_mes.columns:
        comparar("Temperatura media", MATERIAL["rojo"], df_mes["tmed"].mean(), _num(normal.get("tm_mes_md")), "°C")
    if "prec" in df_mes.columns:
        comparar("Precipitación total", MATERIAL["azul_claro"], df_mes["prec"].sum(), _num(normal.get("p_mes_md")), "mm", 0)
    if "hrmedia" in df_mes.columns:
        comparar("Humedad media", MATERIAL["teal"], df_mes["hrmedia"].mean(), _num(normal.get("hr_md")), "%", 0)
    if "velmedia" in df_mes.columns:
        # w_med_md (el normal) ya viene en km/h de AEMET; velmedia ya se convirtió
        # de m/s a km/h en historico_a_dataframe, así que las unidades coinciden.
        comparar("Viento medio", MATERIAL["indigo"], df_mes["velmedia"].mean(), _num(normal.get("w_med_md")), "km/h")

    if not filas:
        return ""

    tarjetas = "".join(
        f'<div class="tarjeta" style="border-top-color:{color};">'
        f"<h3>{etiqueta}</h3>"
        f"<p>{valor}</p>"
        f"<p class='fecha'>{diferencia}</p>"
        f"</div>"
        for etiqueta, color, valor, diferencia in filas
    )
    titulo = f'<h3 class="subtitulo">Comparado con la media histórica de {NOMBRES_MES[mes]} ({anio})</h3>'
    explicacion = (
        '<p class="aviso">Compara el último mes ya completo con la media histórica de AEMET '
        'para ese mismo mes en esta estación (no el mes en curso, para no comparar un mes a '
        'medias). ▲ Máx y ▼ Mín, arriba, no aparecen aquí: cada tarjeta muestra el valor '
        'medio del mes y su diferencia con lo habitual — positivo es por encima de lo '
        'normal, negativo por debajo.</p>'
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

    for estacion in STATIONS:
        idema = estacion.get("idema")
        nombre = estacion["nombre"]
        df_hist, df_pred = pd.DataFrame(), pd.DataFrame()
        lectura_actual = None

        # La observación actual, el histórico y la predicción se piden por
        # separado: si una de las tres falla (p. ej. por el límite de
        # peticiones de AEMET), las demás se siguen mostrando en vez de
        # perder todo el bloque de la estación.
        try:
            idema_obs = estacion.get("idema_tiempo_real") or idema
            print(f"Procesando observación actual de {nombre} (idema={idema_obs})...")
            lectura_actual = obtener_observacion_actual(idema_obs)
        except Exception as exc:
            print(f"Aviso: no se pudo obtener la observación actual de {nombre}: {exc}")

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

        try:
            print(f"Procesando predicción de {nombre}...")
            prediccion = obtener_prediccion(estacion["municipio"])
            df_pred = prediccion_a_dataframe(prediccion)
            if df_pred.empty:
                dias_recibidos = prediccion.get("prediccion", {}).get("dia", [])
                print(
                    f"Aviso: la predicción de {nombre} se descargó sin errores pero "
                    f"salió vacía. Claves de nivel superior recibidas: {list(prediccion.keys())}. "
                    f"Días dentro de 'prediccion': {len(dias_recibidos)}."
                )
        except Exception as exc:
            print(f"Aviso: no se pudo obtener la predicción de {nombre}: {exc}")

        slug = _slug(nombre)
        estaciones_menu.append((slug, nombre, estacion.get("lat"), estacion.get("lon")))
        seccion = [f'<section class="estacion" data-estacion="{slug}"><h2>{nombre}</h2>']
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
        seccion.extend(construir_graficos_historico(df_hist))
        seccion.append('</div>')

        seccion.append("</section>")
        bloques_html.append("".join(seccion))

    selector_html = ""
    if len(estaciones_menu) > 1:
        opciones = "".join(f'<option value="{slug}">{nombre}</option>' for slug, nombre, _, _ in estaciones_menu)
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

    html = f"""<!DOCTYPE html>
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
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
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
        f.write(html)
    print("Dashboard generado en docs/index.html")


if __name__ == "__main__":
    main()
