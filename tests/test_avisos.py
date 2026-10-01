"""Lectura y filtrado de los avisos CAP de AEMET."""
import io
import tarfile
from datetime import datetime, timedelta, timezone

import fetch_weather_dash as f

AHORA = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def cap(nivel, zona, evento, horas_inicio, horas_fin, tipo="Alert", codigo="774601"):
    inicio = (AHORA + timedelta(hours=horas_inicio)).isoformat()
    fin = (AHORA + timedelta(hours=horas_fin)).isoformat()
    return f"""<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><msgType>{tipo}</msgType>
<info><language>es-ES</language><event>{evento}</event><onset>{inicio}</onset><expires>{fin}</expires>
<description>Descripción</description>
<parameter><valueName>AEMET-Meteoalerta nivel</valueName><value>{nivel}</value></parameter>
<area><areaDesc>{zona}</areaDesc><geocode><valueName>AEMET-Meteoalerta zona</valueName><value>{codigo}</value></geocode></area></info>
<info><language>en-GB</language><event>English</event>
<parameter><valueName>AEMET-Meteoalerta nivel</valueName><value>{nivel}</value></parameter></info></alert>""".encode()


def empaquetar(*xmls):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for i, xml in enumerate(xmls):
            info = tarfile.TarInfo(f"{i}.xml")
            info.size = len(xml)
            tar.addfile(info, io.BytesIO(xml))
    return buffer.getvalue()


def test_lee_tar_y_filtra_por_zona_nivel_y_caducidad():
    contenido = empaquetar(
        cap("naranja", "Litoral norte de Valencia", "Aviso naranja por lluvias", -2, 10),
        cap("amarillo", "Litoral norte de Valencia", "Aviso caducado", -20, -1),
        cap("verde", "Litoral norte de Valencia", "Sin aviso", -1, 5),
        cap("rojo", "Litoral sur de Alicante", "Otra zona", -1, 5, codigo="770302"),
        cap("amarillo", "Litoral norte de Valencia", "Cancelado", -1, 5, tipo="Cancel"),
    )
    avisos = []
    for xml in f._ficheros_cap(contenido):
        avisos.extend(f._avisos_de_cap(f.ET.fromstring(xml)))
    assert len(avisos) == 3  # solo en español; sin "verde" ni cancelados
    zona = f.avisos_para_zona(avisos, "litoral norte de valencia", ahora=AHORA)
    assert [a["evento"] for a in zona] == ["Aviso naranja por lluvias"]
    assert f.avisos_para_zona(avisos, "774601", ahora=AHORA) == zona  # también por código


def test_xml_suelto_sin_tar():
    xml = cap("amarillo", "Litoral norte de Valencia", "Aviso", 1, 5)
    assert f._ficheros_cap(xml) == [xml]


def test_banner_sin_avisos_y_con_error():
    assert "Sin avisos" in f.construir_banner_avisos([], "Litoral norte de Valencia")
    assert "No se pudieron consultar" in f.construir_banner_avisos([], "Zona", error=True)


def test_banner_con_avisos_toma_el_color_del_peor_nivel():
    aviso = {"nivel": "amarillo", "evento": "Aviso amarillo por lluvias", "titular": "", "descripcion": "40 mm en 1 h",
             "inicio": None, "fin": None, "zonas": []}
    banner = f.construir_banner_avisos([aviso, dict(aviso, nivel="naranja", evento="Aviso naranja por tormentas")], "Zona")
    assert "avisos-naranja" in banner and "2 avisos meteorológicos" in banner
    assert banner.count('class="aviso-meteo') == 2



def test_banner_agrupa_por_dia_y_ordena_por_nivel():
    ahora = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)  # miércoles
    def aviso(nivel, evento, desde_h, hasta_h):
        return {"nivel": nivel, "evento": evento, "titular": "", "descripcion": "", "zonas": [],
                "inicio": ahora + timedelta(hours=desde_h), "fin": ahora + timedelta(hours=hasta_h)}
    banner = f.construir_banner_avisos([
        aviso("amarillo", "Lluvias hoy", -2, 6), aviso("naranja", "Tormentas hoy", 1, 8),
        aviso("amarillo", "Lluvias jueves", 20, 30), aviso("rojo", "Lluvias jueves rojo", 24, 30),
        aviso("amarillo", "Viento viernes", 46, 50),
    ], "Zona", ahora=ahora)
    orden = [banner.index(t) for t in (
        "Hoy, miércoles 30 de septiembre", ">Tormentas hoy<", ">Lluvias hoy<",
        "Mañana, jueves 1 de octubre", ">Lluvias jueves rojo<", ">Lluvias jueves<",
        "Viernes 2 de octubre", ">Viento viernes<")]
    assert orden == sorted(orden) and len(set(orden)) == len(orden)


def test_aviso_de_varios_dias_sale_en_cada_dia():
    ahora = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)  # miércoles, 12:00 local

    def aviso(nivel, evento, inicio, fin):
        return {"nivel": nivel, "evento": evento, "titular": "", "descripcion": "60 mm en 1 h", "zonas": [],
                "inicio": inicio, "fin": fin}
    rojo = aviso("rojo", "Rojo jue-vie", datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc),  # jue 18:00 local
                 datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc))  # vie 09:00 local
    costero = aviso("amarillo", "Costero mié-vie", datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc),  # ya en vigor
                    datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc))  # sáb 00:00 local: no pasa al sábado
    naranja = aviso("naranja", "Naranja vie", datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
                    datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc))
    banner = f.construir_banner_avisos([costero, rojo, naranja], "Zona", ahora=ahora)
    assert "3 avisos meteorológicos" in banner and "sábado" not in banner
    hoy, jueves, viernes = (banner.split('<p class="avisos-dia">')[i] for i in (1, 2, 3))
    assert hoy.startswith("Hoy, miércoles") and "En vigor hasta las 24:00 · sigue el jueves" in hoy
    assert jueves.index(">Rojo jue-vie<") < jueves.index(">Costero mié-vie<")
    assert "De 18:00 a 24:00 · sigue el viernes" in jueves and "Todo el día · viene del miércoles · sigue el viernes" in jueves
    assert viernes.index(">Rojo jue-vie<") < viernes.index(">Naranja vie<") < viernes.index(">Costero mié-vie<")
    assert "Hasta las 09:00 · viene del jueves" in viernes and "Hasta las 24:00 · viene del jueves" in viernes
    assert "De 06:00 a 12:00" in viernes
    assert banner.count("60 mm en 1 h") == 6  # la descripción se repite en cada día
