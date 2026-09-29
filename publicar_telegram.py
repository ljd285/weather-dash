"""
Cliente mínimo de la Bot API de Telegram para publicar los avisos de AEMET
en un canal (avisos_notificar.py). Solo usa `requests`.

Configuración (secretos de GitHub, ver el README):
    TELEGRAM_BOT_TOKEN   token que da @BotFather
    TELEGRAM_CHAT_ID     el canal (@nombre_del_canal) o un chat numérico
"""

import json
import os
import time

import requests

API = "https://api.telegram.org"
# Espera máxima ante un 429 (Telegram indica cuántos segundos esperar).
ESPERA_MAXIMA = 30


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id, simulacro=False):
        self.token = token
        self.chat_id = chat_id
        self.simulacro = simulacro

    @classmethod
    def desde_entorno(cls, simulacro=False):
        """Cliente con las variables de entorno, o None si Telegram no está
        configurado. En simulacro siempre hay cliente: solo imprime."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if simulacro:
            return cls(token, chat_id or "@canal", simulacro=True)
        return cls(token, chat_id) if token and chat_id else None

    def _llamar(self, metodo, **datos):
        if self.simulacro:
            print(f"[simulacro] Telegram {metodo}: {json.dumps(datos, ensure_ascii=False)[:400]}")
            return None
        for intento in range(2):
            resp = requests.post(f"{API}/bot{self.token}/{metodo}", json=datos, timeout=30)
            cuerpo = resp.json() if resp.content else {}
            if resp.status_code == 429 and intento == 0:
                time.sleep(min(cuerpo.get("parameters", {}).get("retry_after", 5), ESPERA_MAXIMA))
                continue
            if not resp.ok or not cuerpo.get("ok"):
                # Sin la URL de la petición: lleva el token.
                raise TelegramError(f"{metodo}: {resp.status_code} {cuerpo.get('description', '')}".strip())
            return cuerpo.get("result")

    def enviar(self, texto, responder_a=None):
        """Publica un mensaje (HTML) y devuelve su message_id (None en simulacro)."""
        datos = {"chat_id": self.chat_id, "text": texto, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if responder_a:
            datos["reply_parameters"] = {"message_id": responder_a, "allow_sending_without_reply": True}
        resultado = self._llamar("sendMessage", **datos)
        return resultado["message_id"] if resultado else None

    def editar(self, message_id, texto):
        try:
            self._llamar("editMessageText", chat_id=self.chat_id, message_id=message_id, text=texto,
                         parse_mode="HTML", link_preview_options={"is_disabled": True})
        except TelegramError as exc:
            if "not modified" not in str(exc):  # editar sin cambios no es un error
                raise
