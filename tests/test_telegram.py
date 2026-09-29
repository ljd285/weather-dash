"""Cliente de Telegram (sin red: requests.post se sustituye)."""
import pytest

import publicar_telegram as t


class Respuesta:
    def __init__(self, estado, cuerpo):
        self.status_code, self._cuerpo, self.content = estado, cuerpo, b"x"
        self.ok = estado < 400

    def json(self):
        return self._cuerpo


def cliente_con(monkeypatch, *respuestas):
    llamadas, pausas = type('L', (list,), {})(), []

    def falso_post(url, json, timeout):
        llamadas.append((url, json))
        return respuestas[len(llamadas) - 1]

    monkeypatch.setattr(t.requests, "post", falso_post)
    monkeypatch.setattr(t.time, "sleep", pausas.append)
    llamadas.pausas = pausas
    return t.Telegram("TOKEN", "@canal"), llamadas


def test_enviar_devuelve_el_id_y_responde_al_mensaje(monkeypatch):
    tg, llamadas = cliente_con(monkeypatch, Respuesta(200, {"ok": True, "result": {"message_id": 7}}))
    assert tg.enviar("hola", responder_a=3) == 7
    url, datos = llamadas[0]
    assert url.endswith("/botTOKEN/sendMessage") and datos["chat_id"] == "@canal"
    assert datos["parse_mode"] == "HTML" and datos["reply_parameters"]["message_id"] == 3


def test_429_espera_y_reintenta_una_vez(monkeypatch):
    limite = Respuesta(429, {"ok": False, "parameters": {"retry_after": 2}})
    tg, llamadas = cliente_con(monkeypatch, limite, Respuesta(200, {"ok": True, "result": {"message_id": 1}}))
    assert tg.enviar("hola") == 1 and llamadas.pausas == [2]


def test_el_error_no_incluye_el_token(monkeypatch):
    tg, _ = cliente_con(monkeypatch, Respuesta(400, {"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(t.TelegramError) as exc:
        tg.enviar("hola")
    assert "chat not found" in str(exc.value) and "TOKEN" not in str(exc.value)


def test_editar_sin_cambios_no_es_un_error(monkeypatch):
    tg, _ = cliente_con(monkeypatch, Respuesta(400, {"ok": False, "description": "Bad Request: message is not modified"}))
    tg.editar(5, "igual")


def test_desde_entorno(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert t.Telegram.desde_entorno() is None
    assert t.Telegram.desde_entorno(simulacro=True).simulacro
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@canal")
    assert t.Telegram.desde_entorno().chat_id == "@canal"


def test_borrar_ignora_un_mensaje_que_ya_no_existe(monkeypatch):
    tg, llamadas = cliente_con(monkeypatch, Respuesta(400, {"ok": False, "description": "Bad Request: message to delete not found"}))
    tg.borrar(5)
    assert llamadas[0][0].endswith("/deleteMessage")
