import logging
import os

_LOG_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_configured = False


def _configure_root_logger() -> None:
    """Configure the root logger exactly once for the process.

    Calling logging.basicConfig on every get_logger() call is wasteful and makes
    the log level/format depend on call order. Configure once at first use and
    honour the LOG_LEVEL environment variable.
    """
    global _configured
    if _configured:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    _configured = True


class Logger:
    @staticmethod
    def get_logger(name: str) -> logging.Logger:
        _configure_root_logger()
        return logging.getLogger(name)
