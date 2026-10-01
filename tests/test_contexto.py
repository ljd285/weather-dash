"""Bloque meteorológico del correo de los avisos (sin red)."""
from datetime import datetime, timedelta, timezone

import pandas as pd

import avisos_notificar as n
import contexto as c

INICIO = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # 14:00 hora local


def episodio(fenomeno="lluvias", descripcion="Precipitación acumulada en una hora: 40 mm. En 12 horas: 120 mm"):
    return {"zona": "Litoral norte de Valencia", "fenomeno": fenomeno, "nivel": "naranja",
            "inicio": INICIO, "fin": INICIO + timedelta(hours=7),
            "tramos": [{"nivel": "naranja", "inicio": INICIO, "fin": INICIO + timedelta(hours=7),
                        "descripcion": descripcion, "probabilidad": "40%-70%"}]}


def prediccion():
    horas = pd.date_range("2026-10-01 12:00", periods=12, freq="h")  # hora local
    prec = [0, 0, 1, 3, 12, 8, 6, 2, 0, 0, 0, 0]
    return pd.DataFrame({"fecha": horas, "prec": prec, "prob_tormenta": [10, 20, 40, 65, 65, 50, 30, 10, 0, 0, 0, 0],
                         "racha": [20, 25, 30, 55, 50, 40, 30, 20, 15, 15, 15, 15], "dir": ["E"] * 12,
                         "temp": [22.0] * 12})


NORMALES = [{"mes": "10", "p_mes_md": "77.0", "tm_max_md": "25.4"}]
EXTREMOS = {"prec": [{"valor": 155.0, "dia": 9.0, "anio": 1957.0, "unidad": "mm"}] * 12}


def test_bloque_de_lluvia():
    bloque = c.bloque_contexto(episodio(), "Valencia", "Valencia", prediccion(), None, NORMALES, EXTREMOS)
    assert "**Predicción de AEMET para Valencia en esas horas**" in bloque
    assert "Unos **32 mm** en total, lo más intenso hacia las **16 h** (12,0 mm en una hora)." in bloque
    assert "hasta el **65 %**" in bloque and "Rachas de hasta **55 km/h** del E." in bloque
    assert "120 mm sería más de lo que llueve de media en todo octubre (77 mm)." in bloque
    assert "El récord de lluvia en un día de octubre en Valencia es de 155,0 mm (9/10/1957)." in bloque


def test_bloque_de_calor_y_sin_datos():
    ep = episodio("temperaturas máximas", "Temperatura máxima: 39 ºC")
    bloque = c.bloque_contexto(ep, "Valencia", "Valencia", prediccion(), None, NORMALES, None)
    assert "Máxima de **22 °C**" in bloque
    assert "39 °C están 14 °C por encima de la máxima media de octubre en Valencia (25,4 °C)." in bloque
    assert c.bloque_contexto(episodio(), "Valencia", "Valencia") == ""


def test_el_contexto_va_en_el_correo_y_se_conserva():
    ep = dict(episodio(), contexto="**Para hacerse una idea:** mucho.")
    texto = n.cuerpo(ep)
    assert "**Para hacerse una idea:** mucho." in texto and "(probabilidad 40–70 %)" in texto
    assert n.leer_estado(texto)["contexto"] == "**Para hacerse una idea:** mucho."
