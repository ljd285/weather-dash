"""
Climatología diaria de cada estación a partir de sus datos diarios de
1991-2020 (el periodo de las normales oficiales de AEMET).

AEMET solo publica normales mensuales. Para comparar cada día con lo normal
para su fecha, se descarga una sola vez la serie diaria 1991-2020 de la
estación (máxima, mínima y precipitación) y se guarda en
data/serie_1991_2020_<idema>.csv. De ahí sale, para cada día del año, la
distribución de valores de los días de alrededor (±VENTANA_DIAS) en los 30
años: su media (la normal del día) y el percentil de un valor dado.

La descarga (unas 60 peticiones por estación) la hace el workflow
"Descargar climatología diaria" (.github/workflows/climatologia.yml):
    python climatologia.py
Si se corta (límite de peticiones, AEMET caído...), al volver a lanzarla
sigue por donde iba: los tramos ya descargados se apuntan en
data/serie_1991_2020_<idema>_tramos.json.
"""
import csv
import json
import os
import sys
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

CACHE_DIR = "data"
SERIE_INICIO = date(1991, 1, 1)
SERIE_FIN = date(2020, 12, 31)
DIAS_TRAMO = 180  # AEMET rechaza rangos de más de ~6 meses
VENTANA_DIAS = 7  # días a cada lado de la fecha que cuentan como "su época"
MUESTRA_MINIMA = 150  # valores necesarios para dar normal y percentil (~10 años)
VARIABLES = ["tmax", "tmin", "prec"]


def _ruta_serie(idema):
    return os.path.join(CACHE_DIR, f"serie_1991_2020_{idema}.csv")


def _ruta_tramos(idema):
    return os.path.join(CACHE_DIR, f"serie_1991_2020_{idema}_tramos.json")


def tramos():
    """Inicio y fin de cada tramo de la serie, siempre los mismos."""
    inicio = SERIE_INICIO
    while inicio <= SERIE_FIN:
        fin = min(inicio + timedelta(days=DIAS_TRAMO), SERIE_FIN)
        yield inicio, fin
        inicio = fin + timedelta(days=1)


def _numero(valor, es_prec=False):
    """Valor de AEMET ("12,4", "Ip", "Acum", "") como float o None."""
    if valor is None:
        return None
    texto = str(valor).strip().replace(",", ".")
    if es_prec and texto == "Ip":  # inapreciable (< 0,1 mm)
        return 0.0
    try:
        return float(texto)
    except ValueError:
        return None


def registros_a_filas(registros):
    """Registros diarios de AEMET -> filas (fecha, tmax, tmin, prec)."""
    filas = []
    for r in registros:
        if "fecha" not in r:
            continue
        filas.append((
            r["fecha"][:10],
            _numero(r.get("tmax")),
            _numero(r.get("tmin")),
            _numero(r.get("prec"), es_prec=True),
        ))
    return filas


def _leer_tramos(idema):
    try:
        with open(_ruta_tramos(idema), encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, json.JSONDecodeError):
        return set()


def _leer_filas(idema):
    try:
        with open(_ruta_serie(idema), encoding="utf-8", newline="") as f:
            lector = csv.reader(f)
            next(lector, None)
            return {fila[0]: fila for fila in lector if fila}
    except OSError:
        return {}


def _guardar(idema, filas_por_fecha, hechos):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_ruta_serie(idema), "w", encoding="utf-8", newline="") as f:
        escritor = csv.writer(f)
        escritor.writerow(["fecha", *VARIABLES])
        for fecha in sorted(filas_por_fecha):
            fila = filas_por_fecha[fecha]
            escritor.writerow([fila[0], *("" if v in (None, "") else v for v in fila[1:])])
    with open(_ruta_tramos(idema), "w", encoding="utf-8") as f:
        json.dump(sorted(hechos), f)


def descargar_serie(idema, pedir_tramo, sin_datos=(), pausa=1.0):
    """Descarga los tramos de la serie 1991-2020 que falten. `pedir_tramo`
    recibe (inicio, fin) y devuelve los registros diarios de AEMET; las
    excepciones de `sin_datos` significan que ese tramo no tiene datos (se
    da por hecho). Cualquier otro error detiene la descarga, guardando lo
    que ya se tenía. Devuelve True si la serie queda completa."""
    hechos = _leer_tramos(idema)
    filas = _leer_filas(idema)
    pendientes = [(i, f) for i, f in tramos() if i.isoformat() not in hechos]
    print(f"Serie 1991-2020 de {idema}: {len(pendientes)} tramo(s) pendiente(s) de {len(list(tramos()))}.")
    for inicio, fin in pendientes:
        try:
            registros = pedir_tramo(inicio, fin)
        except sin_datos:
            registros = []
            print(f"  {inicio} - {fin}: sin datos en AEMET.")
        except Exception as exc:
            print(f"  {inicio} - {fin}: error ({exc}); se guarda lo descargado y se para.")
            _guardar(idema, filas, hechos)
            return False
        for fila in registros_a_filas(registros):
            filas[fila[0]] = fila
        hechos.add(inicio.isoformat())
        _guardar(idema, filas, hechos)  # tras cada tramo: si se corta, no se pierde nada
        print(f"  {inicio} - {fin}: {len(registros)} día(s).")
        time.sleep(pausa)
    return True


def leer_serie(idema):
    """La serie 1991-2020 guardada, como DataFrame (fecha, tmax, tmin,
    prec), o vacío si no se ha descargado todavía."""
    ruta = _ruta_serie(idema)
    if not os.path.exists(ruta):
        return pd.DataFrame()
    df = pd.read_csv(ruta, parse_dates=["fecha"])
    return df if not df.empty else pd.DataFrame()


def _dia_del_anio(fechas):
    """Día del año en un calendario de 365 días (el 29 de febrero cuenta
    como el 28), de 0 a 364."""
    fechas = pd.DatetimeIndex(fechas)
    doy = fechas.dayofyear.to_numpy() - 1
    bisiesto = fechas.is_leap_year
    return np.where(bisiesto & (doy >= 59), doy - 1, doy)


def climatologia_diaria(serie):
    """Para cada día del año (0-364) y variable, los valores de la serie en
    los días de alrededor (±VENTANA_DIAS), ordenados. Es la "época" con la
    que se compara un día: {variable: [array ordenado por día del año]}."""
    if serie.empty:
        return {}
    doy = _dia_del_anio(serie["fecha"])
    resultado = {}
    for var in VARIABLES:
        if var not in serie.columns:
            continue
        valores = serie[var].to_numpy(dtype="float64")
        validos = ~np.isnan(valores)
        muestras = []
        for d in range(365):
            distancia = np.abs(doy - d)
            distancia = np.minimum(distancia, 365 - distancia)
            muestras.append(np.sort(valores[validos & (distancia <= VENTANA_DIAS)]))
        resultado[var] = muestras
    return resultado


def comparar_dia(clima, var, fecha, valor):
    """Compara un valor con la climatología de su fecha. Devuelve un dict
    con 'normal' (media de la época), 'percentil' (0-100, qué parte de la
    época queda por debajo), 'minimo'/'maximo' de la época y 'n', o None si
    no hay climatología suficiente o el valor falta."""
    if var not in clima or valor is None or pd.isna(valor):
        return None
    muestra = clima[var][int(_dia_del_anio([fecha])[0])]
    if len(muestra) < MUESTRA_MINIMA:
        return None
    debajo = np.searchsorted(muestra, valor, side="left")
    iguales = np.searchsorted(muestra, valor, side="right") - debajo
    return {
        "normal": float(muestra.mean()),
        "percentil": float(100 * (debajo + iguales / 2) / len(muestra)),
        "minimo": float(muestra[0]),
        "maximo": float(muestra[-1]),
        "n": len(muestra),
    }


def lluvia_normal(clima, fechas):
    """Lluvia media diaria (mm) y probabilidad de lluvia (≥ 1 mm) de la
    época de cada fecha, o None donde no hay climatología suficiente."""
    medias, probabilidades = [], []
    for fecha in fechas:
        muestra = clima.get("prec", [])
        muestra = muestra[int(_dia_del_anio([fecha])[0])] if len(muestra) else []
        if len(muestra) < MUESTRA_MINIMA:
            medias.append(None)
            probabilidades.append(None)
        else:
            medias.append(float(muestra.mean()))
            probabilidades.append(float((muestra >= 1).mean()))
    return medias, probabilidades


def main():
    """Descarga (o completa) la serie 1991-2020 de todas las estaciones."""
    import fetch_weather_dash as f
    from config import STATIONS

    if not os.environ.get("AEMET_API_KEY"):
        sys.exit("ERROR: define la variable de entorno AEMET_API_KEY.")

    def pedir(idema):
        def tramo(inicio, fin):
            return f.aemet_get(
                f"/valores/climatologicos/diarios/datos/fechaini/{inicio:%Y-%m-%d}T00:00:00UTC"
                f"/fechafin/{fin:%Y-%m-%d}T23:59:59UTC/estacion/{idema}"
            )
        return tramo

    completas = True
    for estacion in STATIONS:
        idema = estacion.get("idema")
        if not idema:
            continue
        completas &= descargar_serie(idema, pedir(idema), sin_datos=(f.SinDatosAEMET,))
        serie = leer_serie(idema)
        if not serie.empty:
            print(f"{idema}: {len(serie)} días guardados, {serie['fecha'].dt.year.nunique()} años con datos.")
    if not completas:
        sys.exit("La descarga quedó a medias: vuelve a lanzarla para completarla.")


if __name__ == "__main__":
    main()
