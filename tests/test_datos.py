"""Conversión de las respuestas de AEMET en tablas."""
import pandas as pd

import fetch_weather_dash as f


def test_num_admite_coma_decimal_y_vacios():
    assert f._num("12,5") == 12.5
    assert f._num("7") == 7.0
    assert f._num("") is None
    assert f._num(None) is None
    assert f._num("Varias") is None


def test_historico_convierte_unidades_y_precipitacion_inapreciable():
    df = f.historico_a_dataframe([
        {"fecha": "2026-08-02", "tmax": "31,0", "tmin": "22,4", "prec": "Ip", "velmedia": "2,5", "racha": "10,0", "hrMedia": "70"},
        {"fecha": "2026-08-01", "tmax": "30,2", "tmin": "21,0", "prec": "3,4", "velmedia": "1,0", "racha": "5,0", "hrMedia": "65"},
    ])
    assert list(df["fecha"].dt.day) == [1, 2]  # ordenado por fecha
    assert df["prec"].tolist() == [3.4, 0.0]
    assert df["velmedia"].tolist() == [3.6, 9.0]  # m/s -> km/h
    assert df["hrmedia"].tolist() == [65, 70]  # nombres de columna en minúsculas


def test_prediccion_diaria_extrae_cielo_viento_y_uv():
    prediccion = {"prediccion": {"dia": [{
        "fecha": "2026-09-24T00:00:00",
        "temperatura": {"maxima": 30, "minima": 20},
        "probPrecipitacion": [{"value": 10, "periodo": "00-12"}, {"value": 40, "periodo": "12-24"}],
        "estadoCielo": [{"value": "", "periodo": "00-24"}, {"value": "13", "periodo": "00-12", "descripcion": "Intervalos nubosos"}],
        "viento": [{"direccion": "E", "velocidad": 15, "periodo": "00-24"}],
        "uvMax": 7,
    }]}}
    fila = f.prediccion_a_dataframe(prediccion).iloc[0]
    assert fila["prob_precip"] == 40  # la mayor de los tramos
    assert fila["cielo"] == "13" and fila["cielo_texto"] == "Intervalos nubosos"
    assert fila["viento_dir"] == "E" and fila["viento_max"] == 15
    assert fila["uv_max"] == 7


def test_prediccion_horaria_reparte_tramos_y_calcula_noches():
    dia = {
        "fecha": "2026-09-24T00:00:00", "orto": "07:48", "ocaso": "19:52",
        "temperatura": [{"value": str(h), "periodo": f"{h:02d}"} for h in range(24)],
        "precipitacion": [{"value": "Ip" if h == 3 else "0,4", "periodo": f"{h:02d}"} for h in range(24)],
        "probPrecipitacion": [{"value": "80", "periodo": "1420"}, {"value": "5", "periodo": "2002"}],
        "vientoAndRachaMax": [{"direccion": ["E"], "velocidad": ["12"], "periodo": "15"}, {"value": "30", "periodo": "15"}],
    }
    df, noches = f.prediccion_horaria_a_dataframe({"prediccion": {"dia": [dia]}}, ahora=pd.Timestamp("2026-09-24 00:00"))
    por_hora = df.set_index(df["fecha"].dt.hour)
    assert por_hora.loc[3, "prec"] == 0 and por_hora.loc[4, "prec"] == 0.4
    assert por_hora.loc[16, "prob_precip"] == 80 and por_hora.loc[22, "prob_precip"] == 5
    assert por_hora.loc[15, "viento"] == 12 and por_hora.loc[15, "racha"] == 30 and por_hora.loc[15, "dir"] == "E"
    assert (pd.Timestamp("2026-09-24 19:52"), pd.Timestamp("2026-09-25 06:52")) in noches


def test_normales_diarios_coinciden_a_mitad_de_mes():
    normales = [{"mes": f"{m:02d}", "tm_max_md": str(10 + m)} for m in range(1, 13)]
    df = f.normales_diarios(pd.Series(pd.to_datetime(["2026-06-01", "2026-08-31"])), normales, ["tm_max_md"])
    valores = df.set_index("fecha")["tm_max_md"]
    assert valores[pd.Timestamp("2026-07-15")] == 17  # julio = 10 + 7
    assert 16 < valores[pd.Timestamp("2026-07-01")] < 17  # interpolado entre junio y julio


def test_extremos_en_decimas_y_campos_ausentes():
    crudos = {"T": {"temMax": ["445"] * 13, "diaMax": ["10"] * 13, "anioMax": ["2023"] * 13,
                    "temMin": ["-20"] * 13, "diaMin": ["1"] * 13, "anioMin": ["1985"] * 13},
              "P": {"precMaxDia": ["1344"] * 13, "diaMaxDia": ["4"] * 13, "anioMaxDia": ["1989"] * 13}}
    ext = f.extremos_por_mes(crudos)
    assert ext["tmax"][7]["valor"] == 44.5 and ext["tmax"][7]["anio"] == 2023
    assert ext["tmin"][0]["valor"] == -2.0
    assert ext["prec"][8]["valor"] == 134.4  # sigue a la temperatura: décimas
    assert f.extremos_por_mes({"T": {"otro": 1}}) == {}


def test_csv_en_formato_espanol_con_provisionales(tmp_path):
    df = pd.DataFrame({"fecha": pd.to_datetime(["2026-09-17", "2026-09-18"]), "tmax": [30.25, 31.0], "prec": [0.0, 12.4]})
    prov = pd.DataFrame({"fecha": pd.to_datetime(["2026-09-19"]), "tmax": [29.0], "prec": [1.5]})
    ruta = tmp_path / "datos" / "estacion.csv"
    f.escribir_csv(df, prov, str(ruta))
    lineas = ruta.read_text(encoding="utf-8-sig").splitlines()
    assert lineas[0] == "fecha;temp_max_C;precipitacion_mm;provisional"
    assert lineas[1] == "2026-09-17;30,2;0,0;no"
    assert lineas[3] == "2026-09-19;29,0;1,5;sí"
