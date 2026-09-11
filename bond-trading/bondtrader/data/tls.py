"""Доверенные сертификаты УЦ Минцифры для российских хостов (tinkoff.ru, acra-ratings.ru и др.).

Собирает бандл certifi + Russian Trusted Root/Sub CA (скачиваются с портала Госуслуг) и кэширует
его в data/cache/ru_ca_bundle.pem. Переменная окружения RU_CA_BUNDLE (или TINVEST_CA_BUNDLE)
переопределяет путь к готовому бандлу.
"""
from __future__ import annotations

import logging
import os
import ssl
from typing import Optional

import requests

log = logging.getLogger(__name__)

RU_CA_URLS = [
    "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
    "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
]
_cached: Optional[str] = None


def _to_pem(raw: bytes) -> str:
    if b"BEGIN CERTIFICATE" in raw:
        return raw.decode("utf-8", "ignore")
    return ssl.DER_cert_to_PEM_cert(raw)


def ru_ca_bundle(cache_dir: str = "data/cache", force: bool = False) -> Optional[str]:
    """Путь к PEM-бандлу с сертификатами Минцифры или None, если собрать не удалось."""
    global _cached
    env = os.environ.get("RU_CA_BUNDLE") or os.environ.get("TINVEST_CA_BUNDLE")
    if env and os.path.exists(env):
        return env
    if _cached and not force and os.path.exists(_cached):
        return _cached
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "ru_ca_bundle.pem")
    if os.path.exists(path) and not force:
        _cached = path
        return path
    try:
        import certifi
        parts = [open(certifi.where(), encoding="utf-8").read()]
        for url in RU_CA_URLS:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            parts.append(_to_pem(r.content))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(p.strip() + "\n" for p in parts))
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(path)  # валидация бандла
        _cached = path
        log.info("бандл сертификатов Минцифры собран: %s", path)
        return path
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось собрать бандл сертификатов Минцифры: %s", e)
        return None
