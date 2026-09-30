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
    assert "hora peninsular" not in texto
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


def test_cambios_en_los_avisos_actualizan_el_dashboard():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    assert n.hay_que_actualizar_dashboard([("crear", ep)])
    assert n.hay_que_actualizar_dashboard([("cerrar_terminado", 2)])
    assert not n.hay_que_actualizar_dashboard([("sin_cambios", 4, ep), ("marcar_ausente", 4, {})])
    assert not n.hay_que_actualizar_dashboard([])


# --- Telegram ---------------------------------------------------------------

class TelegramFalso:
    def __init__(self, falla=False):
        self.llamadas, self.falla, self.siguiente = [], falla, 100

    def enviar(self, texto, responder_a=None):
        if self.falla:
            raise RuntimeError("caído")
        self.llamadas.append(("enviar", texto, responder_a))
        self.siguiente += 1
        return self.siguiente

    def enlace_mensaje(self, message_id):
        return f"https://t.me/canal/{message_id}"

    def editar(self, message_id, texto):
        if self.falla:
            raise RuntimeError("caído")
        self.llamadas.append(("editar", message_id, texto))

    def borrar(self, message_id):
        if self.falla:
            raise RuntimeError("caído")
        self.llamadas.append(("borrar", message_id))


class GitHubConNumero(GitHubFalso):
    def crear(self, titulo, cuerpo):
        super().crear(titulo, cuerpo)
        return {"number": 42}


def test_texto_telegram_escapa_html_y_marca_el_cierre():
    ep = n.episodios([tramo("naranja", 2, 8, descripcion="Racha <90 km/h> & lluvia")], ZONA)[0]
    texto = n.texto_telegram(ep)
    assert texto.startswith("🟠 Aviso <b>naranja</b> por lluvias") and "Dashboard" not in texto and "hora peninsular" not in texto and "Racha &lt;90 km/h&gt; &amp; lluvia" in texto
    assert "Fuente: AEMET" in texto and len(texto) < 4096
    assert n.texto_telegram(ep, cierre="retirado").startswith("🚫") and "⬜" not in n.texto_telegram(ep)
    assert "retirado" in n.texto_respuesta_telegram()


def test_aviso_nuevo_se_publica_y_guarda_el_id_en_el_issue():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    github, telegram = GitHubConNumero(), TelegramFalso()
    n.aplicar([("crear", ep)], github, AHORA, telegram)
    assert telegram.llamadas[0][0] == "enviar"
    assert n.leer_estado(github.llamadas[-1][2]["body"])["telegram_id"] == 101


def test_actualizacion_publica_un_mensaje_nuevo_completo_y_marca_el_anterior_como_sustituido():
    antes = n.episodios([tramo("amarillo", 2, 6)], ZONA)[0]
    antes["telegram_id"] = 55
    despues = n.episodios([tramo("naranja", 2, 6)], ZONA)
    (accion,) = n.decidir(despues, [abierto(7, antes)], AHORA)
    github, telegram = GitHubFalso(), TelegramFalso()
    n.aplicar([accion], github, AHORA, telegram, {7: abierto(7, antes)[1]})
    enviar, editar = telegram.llamadas
    llamada = enviar
    assert llamada[0] == "enviar" and llamada[2] == 55  # responde al anterior
    # el anterior queda marcado como sustituido, con enlace al nuevo (id 101)
    assert editar[:2] == ("editar", 55) and editar[2].startswith('🔄 <b>Aviso sustituido por <a href="https://t.me/canal/101">uno posterior</a>')
    assert "🟡 Aviso <b>amarillo</b>" in editar[2]
    texto = llamada[1]
    assert texto.startswith("🔄 <b>Aviso actualizado</b>") and "Sube a nivel <b>naranja</b>" in texto
    assert '<a href="https://t.me/canal/55">aviso anterior</a>' in texto
    assert "🟠 Aviso <b>naranja</b> por lluvias" in texto and "📍 Litoral norte de Valencia" in texto  # aviso completo
    assert n.leer_estado(github.llamadas[-1][2]["body"])["telegram_id"] == 101  # el cierre actuará sobre el nuevo


def test_si_falla_el_mensaje_nuevo_se_conserva_el_id_anterior():
    antes = n.episodios([tramo("amarillo", 2, 6)], ZONA)[0]
    antes["telegram_id"] = 55
    (accion,) = n.decidir(n.episodios([tramo("naranja", 2, 6)], ZONA), [abierto(7, antes)], AHORA)
    github = GitHubFalso()
    n.aplicar([accion], github, AHORA, TelegramFalso(falla=True), {7: abierto(7, antes)[1]})
    assert n.leer_estado(github.llamadas[-1][2]["body"])["telegram_id"] == 55


def test_retirada_y_fin_marcan_el_mensaje_original():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    ep["telegram_id"] = 9
    numero, estado = abierto(4, ep)
    telegram = TelegramFalso()
    n.aplicar([("retirar", numero, estado), ("cerrar_terminado", numero)], GitHubFalso(), AHORA, telegram, {numero: estado})
    assert [ll[0] for ll in telegram.llamadas] == ["editar", "enviar", "editar"]
    assert telegram.llamadas[0][2].startswith("🚫") and telegram.llamadas[2][2].startswith("⌛")


def test_si_telegram_falla_los_issues_siguen_y_se_reintenta_despues():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    github = GitHubConNumero()
    n.aplicar([("crear", ep)], github, AHORA, TelegramFalso(falla=True))
    assert github.llamadas == [("crear", n.titulo(ep))]  # el issue se creó y no hubo más ediciones
    sin_publicar = abierto(42, ep)
    telegram, github = TelegramFalso(), GitHubFalso()
    n.reintentar_telegram([sin_publicar], [], github, telegram, AHORA)
    assert telegram.llamadas[0][0] == "enviar"
    assert n.leer_estado(github.llamadas[0][2]["body"])["telegram_id"] == 101
    # con id, ausente o terminado, no se vuelve a publicar
    ya = dict(ep, telegram_id=1)
    telegram = TelegramFalso()
    n.reintentar_telegram([abierto(1, ya), abierto(2, ep, ausente_desde=AHORA)], [], github, telegram, AHORA)
    n.reintentar_telegram([sin_publicar], [], github, telegram, AHORA + timedelta(days=2))
    assert telegram.llamadas == []


def test_republicar_borra_el_mensaje_y_guarda_el_id_nuevo():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    ep["telegram_id"] = 9
    github, telegram = GitHubFalso(), TelegramFalso()
    n.republicar_telegram([abierto(4, ep)], [], github, telegram)
    assert [ll[0] for ll in telegram.llamadas] == ["borrar", "enviar"] and telegram.llamadas[0][1] == 9
    assert n.leer_estado(github.llamadas[0][2]["body"])["telegram_id"] == 101


def test_republicar_no_publica_si_no_puede_borrar():
    ep = n.episodios([tramo("naranja", 2, 8)], ZONA)[0]
    ep["telegram_id"] = 9
    github, telegram = GitHubFalso(), TelegramFalso(falla=True)
    n.republicar_telegram([abierto(4, ep)], [], github, telegram)
    assert github.llamadas == [] and telegram.llamadas == []
