"""Structured logging helpers for yt-best-video."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

SESSION_ID = str(uuid.uuid4())[:8]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _make_handler(filename: str) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        LOGS_DIR / filename,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _make_logger(name: str, filename_prefix: str) -> logging.Logger:
    named_logger = logging.getLogger(f"yt_best_video.{name}")
    named_logger.setLevel(logging.DEBUG)

    if not named_logger.handlers:
        named_logger.addHandler(_make_handler(f"{filename_prefix}_{_today()}.log"))

        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(
            logging.Formatter(f"%(asctime)s [{name.upper()}] %(message)s")
        )
        named_logger.addHandler(console)

    named_logger.propagate = False
    return named_logger


_debug_logger = _make_logger("debug", "debug")
_ws_logger = _make_logger("ws", "ws_traffic")
_journey_logger = _make_logger("journey", "journey")


def _write(logger: logging.Logger, level: str, event: str, **kwargs) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_mono": round(time.monotonic(), 4),
        "session": SESSION_ID,
        "level": level,
        "event": event,
        **kwargs,
    }
    log_line = json.dumps(record, ensure_ascii=False, default=str)
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(log_line)


def debug(event: str, **kwargs) -> None:
    _write(_debug_logger, "DEBUG", event, **kwargs)


def info(event: str, **kwargs) -> None:
    _write(_debug_logger, "INFO", event, **kwargs)


def warning(event: str, **kwargs) -> None:
    _write(_debug_logger, "WARNING", event, **kwargs)


def error(event: str, **kwargs) -> None:
    _write(_debug_logger, "ERROR", event, **kwargs)


def ws_sent(direction: str, action: str, payload: dict, port: int = 0) -> None:
    _write(
        _ws_logger,
        "DEBUG",
        "ws_message",
        direction=direction,
        port=port,
        action=action,
        payload_keys=list(payload.keys()),
        request_id=payload.get("request_id"),
        execution_id=payload.get("execution_id"),
        journey_id=payload.get("journey_id"),
        status=payload.get("status"),
    )
    _write(
        _ws_logger,
        "DEBUG",
        "ws_payload",
        direction=direction,
        port=port,
        payload=payload,
    )


def journey_event(
    event: str,
    execution_id: str | None = None,
    journey_id: str | None = None,
    **kwargs,
) -> None:
    _write(
        _journey_logger,
        "INFO",
        event,
        execution_id=execution_id,
        journey_id=journey_id,
        **kwargs,
    )
    info(event, execution_id=execution_id, journey_id=journey_id, **kwargs)