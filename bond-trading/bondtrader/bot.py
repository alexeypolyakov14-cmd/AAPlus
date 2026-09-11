"""Telegram-бот: кнопки «по запросу» — отчёт, позиции, новости, скрин ВДО, алерты.

Бот без вебхука: читает обновления через getUpdates (long polling локально, «раз в прогон» в GitHub Actions),
отвечает только чату из TELEGRAM_CHAT_ID (остальным — «доступ закрыт»), нажатия кнопок (callback_query) и
команды (/report, /positions, /news, /screen, /alerts, /menu) рендерит теми же секциями, что и ежедневный отчёт.
Рынок и портфель собираются лениво — один раз за прогон (или раз в ttl секунд в режиме --poll), только если
кто-то что-то запросил. Смещение обновлений (update_id) хранится в файле, чтобы не отвечать дважды.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Optional

from . import report_tg as tg
from .notify import telegram_send, tg_call

log = logging.getLogger(__name__)

ACTIONS = ("report", "positions", "news", "screen", "alerts", "menu", "help")
BUTTONS = [
    [("📊 Отчёт", "report"), ("📋 Позиции", "positions")],
    [("📰 Новости", "news"), ("🔎 Скрин ВДО", "screen")],
    [("🚨 Алерты", "alerts")],
]
COMMANDS = {"/start": "help", "/help": "help", "/menu": "menu", "/report": "report", "/positions": "positions",
            "/news": "news", "/screen": "screen", "/alerts": "alerts"}
HELP = ("<b>bondtrader</b> — ВДО-портфель на MOEX.\n"
        "Кнопки ниже (или команды): /report — полный отчёт, /positions — позиции и P&amp;L, /news — новости по позициям и целям, "
        "/screen — топ кандидатов value_hy, /alerts — стоп-факторы и просадки, /menu — кнопки.\n"
        "Ежедневный отчёт приходит сам в 10:40 МСК по будням.")


def keyboard() -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": a} for t, a in row] for row in BUTTONS]}


def render(action: str, d: Optional[dict]) -> str:
    """HTML-текст ответа на действие. d — данные collect_report (None только для help/menu)."""
    if action == "help":
        return HELP
    if action == "menu":
        return "Что показать?"
    assert d is not None
    if action == "report":
        return tg.render_telegram(d)
    parts = [tg.section_header(d)]
    if action == "positions":
        parts += [tg.section_portfolio(d), tg.section_positions(d)]
    elif action == "news":
        parts += [tg.section_news(d, days=7, limit=20)]
    elif action == "screen":
        parts += [tg.section_screen(d, limit=20), tg.section_stops(d)]
    elif action == "alerts":
        parts += [tg.section_alerts(d, limit=25), tg.section_stops(d)]
    else:
        return f"Не знаю команду «{action}». /help"
    return "\n\n".join("\n".join(p) for p in parts if p).strip()


def parse_update(upd: dict) -> Optional[tuple[str, str, Optional[str]]]:
    """(chat_id, action, callback_query_id) или None, если обновление — не команда и не кнопка."""
    cq = upd.get("callback_query")
    if cq:
        chat = (cq.get("message") or {}).get("chat") or {}
        data = (cq.get("data") or "").strip()
        if not chat.get("id"):
            return None
        return str(chat["id"]), (data if data in ACTIONS else "help"), cq.get("id")
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    if not chat.get("id") or not text.startswith("/"):
        return None
    cmd = text.split()[0].split("@")[0].lower()
    return str(chat["id"]), COMMANDS.get(cmd, "help"), None


class OffsetStore:
    """update_id последнего обработанного обновления — чтобы между прогонами не отвечать повторно."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> int:
        try:
            with open(self.path, encoding="utf-8") as f:
                return int(json.load(f).get("offset", 0))
        except (OSError, ValueError):
            return 0

    def save(self, offset: int) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"offset": offset, "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)


class Bot:
    def __init__(self, collect: Callable[[], dict], chat_id: str, offset_path: str = "state/telegram_offset.json",
                 token: Optional[str] = None, ttl: float = 900):
        self.collect, self.chat_id, self.token, self.ttl = collect, str(chat_id).strip(), token, ttl
        self.store = OffsetStore(offset_path)
        self._data: Optional[dict] = None
        self._data_at = 0.0
        self.handled = 0

    def data(self) -> dict:
        if self._data is None or time.time() - self._data_at > self.ttl:
            self._data = self.collect()
            self._data_at = time.time()
        return self._data

    def send(self, text: str, chat_id: Optional[str] = None, menu: bool = True) -> int:
        return telegram_send(text, token=self.token, chat_id=chat_id or self.chat_id, parse_mode="HTML",
                             reply_markup=keyboard() if menu else None)

    def handle(self, upd: dict) -> Optional[str]:
        parsed = parse_update(upd)
        if parsed is None:
            return None
        chat_id, action, cq_id = parsed
        if cq_id:
            try:
                tg_call("answerCallbackQuery", self.token, callback_query_id=cq_id)
            except RuntimeError as e:   # старая кнопка (>1 ч) — Telegram отвечает ошибкой, это не мешает ответить текстом
                log.debug("answerCallbackQuery: %s", e)
        if chat_id != self.chat_id:
            log.warning("запрос из чужого чата %s — игнорирую", chat_id)
            try:
                telegram_send("Доступ закрыт.", token=self.token, chat_id=chat_id)
            except RuntimeError:
                pass
            return None
        try:
            text = render(action, None if action in ("help", "menu") else self.data())
        except Exception as e:  # noqa: BLE001 — рынок/брокер недоступны: скажем об этом в чате, а не упадём
            log.exception("не удалось собрать данные для %s", action)
            text = f"⚠️ Не удалось собрать данные: {type(e).__name__}: {str(e)[:200]}"
        self.send(text)
        self.handled += 1
        return action

    def run_once(self, timeout: int = 0) -> int:
        """Обработать накопившиеся обновления и выйти (режим GitHub Actions). Возвращает число ответов."""
        offset = self.store.load()
        body = {"allowed_updates": ["message", "callback_query"]}
        if offset:
            body["offset"] = offset
        if timeout:
            body["timeout"] = timeout          # long polling: Telegram держит запрос до появления обновления
        updates = tg_call("getUpdates", self.token, timeout=timeout + 20, **body)
        n = 0
        for upd in updates or []:
            if self.handle(upd):
                n += 1
            offset = upd["update_id"] + 1
            self.store.save(offset)
        return n

    def run_forever(self, timeout: int = 25, interval: float = 1.0) -> None:
        """Long polling (локальный запуск): отвечает мгновенно."""
        log.info("бот запущен: чат %s, long polling %ds", self.chat_id, timeout)
        while True:
            try:
                self.run_once(timeout=timeout)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001 — сеть моргнула: подождём и продолжим
                log.warning("polling: %s", e)
                time.sleep(max(interval, 5.0))
                continue
            time.sleep(interval)
