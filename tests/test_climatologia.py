"""Climatología diaria 1991-2020 y calendario frente a lo normal."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import climatologia as c
import fetch_weather_dash as f


def serie_sintetica():
    """30 años con una máxima que sigue una onda anual (15 °C en enero,
    31 en julio) más ruido, mínima 10 °C por debajo y lluvia 1 de cada 5 días."""
    fechas = pd.date_range("1991-01-01", "2020-12-31", freq="D")
    rng = np.random.default_rng(0)
    onda = 23 - 8 * np.cos(2 * np.pi * (fechas.dayofyear - 15) / 365.25)
    tmax = onda + rng.normal(0, 2, len(fechas))
    return pd.DataFrame({"fecha": fechas, "tmax": tmax, "tmin": tmax - 10,
                         "prec": np.where(np.arange(len(fechas)) % 5 == 0, 4.0, 0.0)})


@pytest.fixture(scope="module")
def clima():
    return c.climatologia_diaria(serie_sintetica())


def test_tramos_cubren_la_serie_sin_huecos():
    lista = list(c.tramos())
    assert lista[0][0] == date(1991, 1, 1) and lista[-1][1] == date(2020, 12, 31)
    assert all(b[0] == a[1] + timedelta(days=1) for a, b in zip(lista, lista[1:], strict=False))
    assert all((fin - ini).days <= c.DIAS_TRAMO for ini, fin in lista)


def test_registros_de_aemet():
    filas = c.registros_a_filas([{"fecha": "1991-01-01", "tmax": "14,2", "tmin": "5,0", "prec": "Ip"},
                                 {"fecha": "1991-01-02", "tmax": "", "prec": "Acum"}])
    assert filas == [("1991-01-01", 14.2, 5.0, 0.0), ("1991-01-02", None, None, None)]


def test_normal_y_percentil_del_dia(clima):
    julio = c.comparar_dia(clima, "tmax", pd.Timestamp("2026-07-15"), 31.0)
    assert julio["normal"] == pytest.approx(31, abs=0.5) and 35 < julio["percentil"] < 65
    assert c.comparar_dia(clima, "tmax", pd.Timestamp("2026-01-15"), 31.0)["percentil"] == 100  # en enero, nunca
    assert c.comparar_dia(clima, "tmax", pd.Timestamp("2028-02-29"), None) is None
    assert c.comparar_dia({}, "tmax", pd.Timestamp("2026-07-15"), 31.0) is None


def test_descarga_reanudable(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "CACHE_DIR", str(tmp_path))
    llamadas = []

    def pedir(inicio, fin):
        llamadas.append(inicio)
        if len(llamadas) == 3:
            raise RuntimeError("AEMET caído")
        return [{"fecha": f"{inicio}T00:00:00", "tmax": "20,0", "tmin": "10,0", "prec": "0,0"}]

    assert c.descargar_serie("X", pedir, pausa=0) is False
    assert len(c.leer_serie("X")) == 2
    llamadas.clear()
    assert c.descargar_serie("X", lambda i, f_: [], pausa=0) is True
    assert len(c.leer_serie("X")) == 2 and c._leer_tramos("X") == {i.isoformat() for i, _ in c.tramos()}


def test_calendario(clima):
    fin = pd.Timestamp("2026-07-19")  # domingo
    historico = pd.DataFrame({"fecha": pd.date_range("2025-07-01", "2026-07-17"), "tmax": 30.0, "tmin": 20.0, "prec": 0.0})
    historico.loc[historico["fecha"] == "2026-07-10", "tmax"] = 45.0
    provisional = pd.DataFrame({"fecha": pd.to_datetime(["2026-07-18"]), "tmax": [31.0], "tmin": [21.0], "prec": [12.0]})
    datos = f.datos_calendario(historico, provisional, fin=fin)
    assert len(datos) == 7 * f.SEMANAS_CALENDARIO and datos["fecha"].iloc[0].weekday() == 0
    assert datos.set_index("fecha").loc["2026-07-18", "provisional"]
    html = f.construir_calendario(datos, clima)
    assert html.count('class="cal-vista"') == 3 and "hidden" in html
    assert "fuera de todo lo registrado" in html  # los 45 °C de julio
    assert "t6 fuera" in html and "prov" in html and "q3" in html  # 12 mm = clase 5-15
    assert "aún no se ha descargado" in f.construir_calendario(datos, {})
