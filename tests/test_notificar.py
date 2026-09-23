"""Decisiones del notificador de avisos (sin red)."""
from datetime import datetime, timedelta, timezone

import avisos_notificar as n

AHORA = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
ZONA = "Litoral norte de Valencia"


def tramo(nivel, desde_h, hasta_h, fenomeno="lluvias", descripcion="Acumulada en 1 h: 40 mm"):
    return {"nivel": nivel, "fenomeno": fenomeno, "descripcion": descripcion,
            "inicio": AHORA + timedelta(hours=desde_h), "fin": AHORA + timedelta(hours=desde_h + (hasta_h - desde_h))}


def abierto(numero, episodio, ausente_desde=None):
    """Simula un issue abierto: su estado sale del texto que se publicaría."""
    return numero, n.leer_estado(n.cuerpo(episodio, ausente_desde=ausente_desde))


def test_tramos_solapados_forman_un_episodio_con_el_nivel_mas_alto():
    eps = n.episodios([tramo("amarillo", 2, 6), tramo("naranja", 6, 12), tramo("amarillo", 30, 34),
                       tramo("amarillo", 3, 8, fenomeno="tormentas")], ZONA)
    lluvias = sorted((e for e in eps if e["fenomeno"] == "lluvias"), key=lambda e: e["inicio"])
    assert len(lluvias) == 2  # el tramo de dentro de 30 h es otro episodio
    assert lluvias[0]["nivel"] == "naranja" and len(lluvias[0]["tramos"]) == 2
    assert lluvias[0]["fin"] == AHORA + timedelta(hours=12)
    assert any(e["fenomeno"] == "tormentas" for e in eps)


def test_nivel_minimo():
    assert n.episodios([tramo("amarillo", 2, 6)], ZONA, nivel_minimo="naranja") == []


def test_aviso_nuevo_se_crea_y_repetido_no_hace_nada():
    ep = n.episodios([tramo("amarillo", 2, 6)], ZONA)
    assert [a[0] for a in n.decidir(ep, [], AHORA)] == ["crear"]
    assert n.decidir(ep, [abierto(1, ep[0])], AHORA) == []


def test_subida_de_nivel_y_ampliacion_se_notifican():
    antes = n.episodios([tramo("amarillo", 2, 6)], ZONA)[0]
    despues = n.episodios([tramo("amarillo", 2, 6), tramo("naranja", 6, 10)], ZONA)
    (accion,) = n.decidir(despues, [abierto(7, antes)], AHORA)
    assert accion[0] == "actualizar" and accion[1] == 7
    textos = " ".join(accion[3])
    assert "Sube a nivel **naranja**" in textos and "Se amplía" in textos


def test_reemision_de_aviso_en_curso_con_otro_inicio_no_se_notifica():
    antes = n.episodios([tramo("amarillo", -3, 6)], ZONA)[0]
    despues = n.episodios([tramo("amarillo", -1, 6)], ZONA)  # ya había empezado: solo cambia el "inicio"
    assert n.decidir(despues, [abierto(3, antes)], AHORA) == []


def test_desaparece_se_marca_y_luego_se_retira():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    (accion,) = n.decidir([], [abierto(4, ep)], AHORA)
    assert accion[0] == "marcar_ausente"
    # 15 minutos después sigue sin aparecer: aún se espera
    assert n.decidir([], [abierto(4, ep, ausente_desde=AHORA)], AHORA + timedelta(minutes=15)) == []
    (accion,) = n.decidir([], [abierto(4, ep, ausente_desde=AHORA)], AHORA + timedelta(minutes=30))
    assert accion[0] == "retirar"


def test_si_reaparece_se_quita_la_marca_de_ausencia():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)
    (accion,) = n.decidir(ep, [abierto(4, ep[0], ausente_desde=AHORA)], AHORA + timedelta(minutes=15))
    assert accion[0] == "sin_cambios"


def test_termina_a_su_hora_se_cierra_sin_mas():
    ep = n.episodios([tramo("amarillo", -6, -1)], ZONA)[0]
    assert n.decidir([], [abierto(5, ep)], AHORA) == [("cerrar_terminado", 5)]


def test_si_falla_aemet_no_se_toca_nada():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    assert n.decidir([], [abierto(4, ep)], AHORA, consulta_ok=False) == []


def test_titulo_cuerpo_y_estado():
    ep = n.episodios([tramo("amarillo", 2, 6), tramo("naranja", 6, 10)], ZONA)[0]
    assert n.titulo(ep).startswith("🟠 Aviso naranja por lluvias – Litoral norte de Valencia (")
    texto = n.cuerpo(ep)
    assert texto.startswith("@") and "🟡 **amarillo**" in texto and "🟠 **naranja**" in texto
    estado = n.leer_estado(texto)
    assert estado["nivel"] == "naranja" and estado["fin"] == ep["fin"] and estado["ausente_desde"] is None
    assert n.leer_estado("un issue cualquiera") is None


class GitHubFalso:
    def __init__(self):
        self.llamadas = []

    def crear(self, titulo, cuerpo):
        self.llamadas.append(("crear", titulo))

    def comentar(self, numero, texto):
        self.llamadas.append(("comentar", numero, texto))

    def editar(self, numero, **campos):
        self.llamadas.append(("editar", numero, campos))


def test_aplicar_acciones():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    github = GitHubFalso()
    n.aplicar([("crear", ep), ("retirar", 9, ep), ("cerrar_terminado", 2)], github, AHORA)
    tipos = [ll[0] for ll in github.llamadas]
    assert tipos == ["crear", "comentar", "editar", "editar"]
    assert "retirado" in github.llamadas[1][2]
    assert github.llamadas[2][2] == {"state": "closed", "state_reason": "not_planned"}
    assert github.llamadas[3][2] == {"state": "closed", "state_reason": "completed"}


def test_aplicar_marcar_ausente_y_reaparecer_conservan_el_estado():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    numero, estado = abierto(4, ep)
    github = GitHubFalso()
    n.aplicar([("marcar_ausente", numero, estado), ("sin_cambios", numero, ep)], github, AHORA)
    marcado = n.leer_estado(github.llamadas[0][2]["body"])
    assert marcado["ausente_desde"] == AHORA and marcado["nivel"] == "naranja"
    assert n.leer_estado(github.llamadas[1][2]["body"])["ausente_desde"] is None
    assert not any(ll[0] == "comentar" for ll in github.llamadas)  # sin correos por esto
