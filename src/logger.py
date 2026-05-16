import logging
import sys
from pathlib import Path

import structlog


_LOG_DIR = Path("logs")
_SIGNAL_LOG = _LOG_DIR / "signals.log"


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog with dual output: JSON file + pretty console."""
    _LOG_DIR.mkdir(exist_ok=True)

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    # File handler — JSON
    file_handler = logging.FileHandler(_SIGNAL_LOG, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    # Console handler — pretty
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # File handler uses JSON renderer
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    file_handler.setFormatter(file_formatter)

    # Console handler uses ConsoleRenderer
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(),
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
