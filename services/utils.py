"""
Prompt utilities: loading, date/time injection, env validation, logging setup.
"""

import os
import sys
from datetime import datetime

from loguru import logger


def load_prompt(path: str) -> str:
    """Load a prompt file and return its contents."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def inject_datetime(prompt: str) -> str:
    """
    Replace {current_date} and {current_time} placeholders with live values.
    No-op if the prompt doesn't contain the placeholders.
    """
    now = datetime.now()
    return (
        prompt
        .replace("{current_date}", now.strftime("%Y-%m-%d"))
        .replace("{current_time}", now.strftime("%H:%M"))
    )


def validate_env(required: list[str]) -> None:
    """
    Check that all required environment variables are set.
    Logs a clear error and exits if any are missing.
    """
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        for key in missing:
            logger.error(f"[ENV] Required variable not set: {key}")
        logger.error("Set the missing variables in your .env file and restart.")
        sys.exit(1)
    logger.info(f"[ENV] all {len(required)} required variables present")


def configure_logging(log_file: str = "logs/voice_agent.log") -> None:
    """
    Set up loguru: stderr for development + rotating file for persistence.
    10 MB rotation, keep 3 files.
    """
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")
    logger.add(
        log_file,
        rotation="10 MB",
        retention=3,
        level="INFO",
        encoding="utf-8",
    )
    logger.info(f"[logging] file logging → {log_file}")
