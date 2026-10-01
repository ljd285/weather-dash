"""
Información meteorológica para el correo de los avisos: qué dice la
predicción por horas de AEMET para las horas del aviso, cómo está ahora la
estación y, para lluvia y temperaturas, una comparación con lo normal y con
los récords («para hacerse una idea»).

bloque_contexto() solo da formato (se prueba sin red); preparar_contexto()
descarga lo necesario y nunca falla: lo que no se pueda obtener, se omite.
"""
import re
from datetime import datetime, timezone

import pandas as pd

import fetch_weather_dash as f
from config import STATIONS


def _tipo(fenomeno):
    """'lluvia', 'viento', 'calor', 'frio' u otro, según el fenómeno."""
    nombre = f._normalizar(fenomeno)
    if any(p in nombre for p in ("lluvia", "precipitacion", "tormenta")):
        return "lluvia"
    if "costero" in nombre or "viento" in nombre:
        return "viento"
    if "maxima" in nombre:
        return "calor"
    if "minima" in nombre:
        return "frio"
    return "otro"


def _local(momento):
    return pd.Timestamp(momento).tz_convert(f.ZONA_HORARIA).tz_localize(None)


def _numeros(textos, unidad):
    """Números seguidos de `unidad` («40 mm», «39 ºC») en los textos."""
    patron = rf"(\d+(?:[.,]\d+)?)\s*{unidad}"
    return [float(n.replace(",", ".")) for t in textos for n in re.findall(patron, t or "")]


def _prediccion(episodio, df, tipo):
    """Frases con la predicción por horas en la franja del aviso."""
    if df is None or df.empty:
        return []
    inicio, fin = _local(episodio["inicio"]), _local(episodio["fin"])
    franja = df[(df["fecha"] >= inicio.floor("h")) & (df["fecha"] < fin)]
    if franja.empty:
        return []
    frases = []
    if tipo == "lluvia" and franja["prec"].notna().any():
        total = franja["prec"].sum()
        pico = franja.loc[franja["prec"].idxmax()]
        if total >= 0.5:
            frases.append(f"Unos **{f._es(total, 0)} mm** en total, lo más intenso hacia las **{pico['fecha']:%H} h** "
                          f"({f._es(pico['prec'])} mm en una hora).")
        else:
            frases.append("Apenas da lluvia en la estación en esas horas (el aviso es para toda la zona).")
        if franja["prob_tormenta"].notna().any() and franja["prob_tormenta"].max() > 0:
            frases.append(f"Probabilidad de tormenta: hasta el **{franja['prob_tormenta'].max():.0f} %**.")
    if tipo in ("lluvia", "viento") and franja["racha"].notna().any():
        fila = franja.loc[franja["racha"].idxmax()]
        direccion = f" del {fila['dir']}" if isinstance(fila.get("dir"), str) and fila["dir"] not in ("", "C") else ""
        frases.append(f"Rachas de hasta **{fila['racha']:.0f} km/h**{direccion}.")
    if tipo in ("calor", "frio") and franja["temp"].notna().any():
        fila = franja.loc[franja["temp"].idxmax() if tipo == "calor" else franja["temp"].idxmin()]
        palabra = "Máxima" if tipo == "calor" else "Mínima"
        frases.append(f"{palabra} de **{f._es(fila['temp'], 0)} °C** hacia las {fila['fecha']:%H} h.")
    return frases


def _ahora(observaciones, tipo, nombre_estacion):
    """Frase con la situación actual en la estación."""
    if not observaciones:
        return ""
    lectura = observaciones[-1]
    partes = []
    if tipo == "lluvia":
        acumulados = f.lluvia_por_dia(observaciones)
        if acumulados:
            partes.append(f"**{f._es(acumulados['hoy'][0])} mm** caídos hoy")
        if lectura.get("prec") is not None:
            partes.append(f"{f._es(lectura['prec'])} mm en la última hora")
    elif tipo == "viento":
        if lectura.get("vmax") is not None:
            partes.append(f"racha de **{lectura['vmax'] * 3.6:.0f} km/h** en la última hora")
        if lectura.get("vv") is not None:
            partes.append(f"viento medio de {lectura['vv'] * 3.6:.0f} km/h")
    elif tipo in ("calor", "frio") and lectura.get("ta") is not None:
        partes.append(f"**{f._es(lectura['ta'])} °C**")
    if not partes:
        return ""
    hora = f._fint_utc(lectura.get("fint"))
    cuando = f" (a las {_local(hora.tz_localize('UTC')):%H:%M})" if hora is not None else ""
    return f"**Ahora mismo en {nombre_estacion}{cuando}:** " + " · ".join(partes) + "."


def _idea(episodio, tipo, normales, extremos, nombre_estacion):
    """«Para hacerse una idea»: la cantidad del aviso frente a lo normal del
    mes y al récord de la estación."""
    mes = _local(episodio["inicio"]).month
    nombre_mes = f.NOMBRES_MES[mes]
    descripciones = [t.get("descripcion", "") for t in episodio.get("tramos", [])]
    normal = f.normales_por_mes(normales).get(mes, {})
    frases = []
    if tipo == "lluvia":
        cantidades = _numeros(descripciones, "mm")
        if not cantidades:
            return ""
        mm = max(cantidades)
        media = f._num(normal.get("p_mes_md"))
        if media:
            if mm >= media:
                comparacion = f"más de lo que llueve de media en todo {nombre_mes} ({f._es(media, 0)} mm)"
            elif mm >= media / 2:
                comparacion = f"más de la mitad de lo que llueve de media en todo {nombre_mes} ({f._es(media, 0)} mm)"
            else:
                comparacion = f"un {100 * mm / media:.0f} % de lo que llueve de media en {nombre_mes} ({f._es(media, 0)} mm)"
            frases.append(f"{f._es(mm, 0)} mm sería {comparacion}.")
        record = (extremos or {}).get("prec", [None] * 12)[mes - 1]
        if record and record.get("valor") is not None:
            fecha = f._fecha_record(record, mes)
            frases.append(f"El récord de lluvia en un día de {nombre_mes} en {nombre_estacion} es de "
                          f"{f._es(record['valor'])} mm{f' ({fecha})' if fecha else ''}.")
    elif tipo in ("calor", "frio"):
        grados = _numeros(descripciones, r"[º°]\s*C")
        if not grados:
            return ""
        valor = max(grados) if tipo == "calor" else min(grados)
        campo, clave, palabra = ("tm_max_md", "tmax", "máxima") if tipo == "calor" else ("tm_min_md", "tmin", "mínima")
        media = f._num(normal.get(campo))
        if media is not None:
            diferencia = valor - media
            frases.append(f"{f._es(valor, 0)} °C están {f._es(abs(diferencia), 0)} °C "
                          f"{'por encima' if diferencia >= 0 else 'por debajo'} de la {palabra} media de "
                          f"{nombre_mes} en {nombre_estacion} ({f._es(media)} °C).")
        record = (extremos or {}).get(clave, [None] * 12)[mes - 1]
        if record and record.get("valor") is not None:
            fecha = f._fecha_record(record, mes)
            frases.append(f"El récord de {palabra} de {nombre_mes} es de {f._es(record['valor'])} °C"
                          f"{f' ({fecha})' if fecha else ''}.")
    return ("**Para hacerse una idea:** " + " ".join(frases)) if frases else ""


def bloque_contexto(episodio, nombre_estacion, nombre_municipio, prediccion=None, observaciones=None,
                    normales=None, extremos=None):
    """Bloque en Markdown para el correo, o "" si no hay nada que contar."""
    tipo = _tipo(episodio["fenomeno"])
    partes = []
    frases = _prediccion(episodio, prediccion, tipo)
    if frases:
        partes.append(f"**Predicción de AEMET para {nombre_municipio} en esas horas**\n"
                      + "\n".join(f"- {frase}" for frase in frases))
    ahora = _ahora(observaciones, tipo, nombre_estacion)
    if ahora:
        partes.append(ahora)
    idea = _idea(episodio, tipo, normales, extremos, nombre_estacion)
    if idea:
        partes.append(idea)
    return "\n\n".join(partes)


_cache = {}


def _una_vez(clave, funcion):
    """Cada dato se descarga una sola vez por ejecución; si falla, None."""
    if clave not in _cache:
        try:
            _cache[clave] = funcion()
        except Exception as exc:
            print(f"Aviso: no se pudo obtener {clave} para el correo del aviso: {exc}")
            _cache[clave] = None
    return _cache[clave]


def preparar_contexto(episodio):
    """Descarga lo necesario y devuelve el bloque de contexto (o "")."""
    estacion = next((e for e in STATIONS if e.get("zona_avisos") == episodio["zona"]), None)
    if not estacion:
        return ""
    idema, municipio = estacion.get("idema"), estacion.get("municipio")
    idema_obs = estacion.get("idema_tiempo_real") or idema
    prediccion = _una_vez(("prediccion_horaria", municipio),
                          lambda: f.prediccion_horaria_a_dataframe(f.obtener_prediccion_horaria(municipio))[0])
    observaciones = _una_vez(("observaciones", idema_obs), lambda: f.obtener_observaciones(idema_obs))
    normales = _una_vez(("normales", idema), lambda: f.obtener_normales(idema)) if idema else None
    extremos = _una_vez(("extremos", idema), lambda: f.obtener_extremos(idema)) if idema else None
    try:
        return bloque_contexto(episodio, estacion["nombre"], estacion.get("nombre_municipio", estacion["nombre"]),
                               prediccion, observaciones, normales, extremos)
    except Exception as exc:  # el correo sale igual, sin este bloque
        print(f"Aviso: no se pudo preparar el contexto del aviso: {exc}")
        return ""


if __name__ == "__main__":  # prueba rápida: python contexto.py
    ahora = datetime.now(timezone.utc)
    print(preparar_contexto({"zona": STATIONS[0]["zona_avisos"], "fenomeno": "lluvias", "inicio": ahora,
                             "fin": ahora + pd.Timedelta(hours=6), "tramos": []}))
