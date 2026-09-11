"""Уведомления: Telegram (bot API) — текст отчёта кусками по 4000 символов, без Markdown-разметки."""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/sendMessage"
CHUNK = 3900


def telegram_send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None, timeout: float = 20) -> int:
    """Отправляет текст (при необходимости — несколькими сообщениями). Возвращает число отправленных сообщений."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    parts = _split(text)
    for i, part in enumerate(parts, 1):
        body = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
        r = requests.post(TG_API.format(token=token), json=body, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram: HTTP {r.status_code} {r.text[:200]}")
    return len(parts)


def _split(text: str, limit: int = CHUNK) -> list[str]:
    """Режем по строкам, чтобы таблицы не рвались посреди строки."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            parts.append(cur)
            cur = ""
        while len(line) > limit:            # очень длинная строка — режем жёстко
            parts.append(line[:limit]); line = line[limit:]
        cur += line
    if cur:
        parts.append(cur)
    return parts or [""]


def strip_markdown(md: str) -> str:
    """Telegram без parse_mode показывает разметку буквально — убираем заголовки/жирный/код."""
    out, in_code = [], False
    for line in md.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        line = line.lstrip("#").strip() if line.startswith("#") else line
        line = line.replace("**", "").replace("`", "")
        out.append(line)
    return "\n".join(out)
