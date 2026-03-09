"""
TaxAI 2026 - Logging Configuration
Centralized logging with file rotation and colored console output
"""

from loguru import logger
import sys
from pathlib import Path
from .config import config

# Remove default handler
logger.remove()

# ==========================================
# CONSOLE HANDLER (colored, user-friendly)
# ==========================================
logger.add(
    sys.stdout,
    colorize=True,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
    level=config.LOG_LEVEL,
    filter=lambda record: record["level"].name != "DEBUG" or config.DEBUG_MODE
)

# ==========================================
# FILE HANDLER (detailed, with rotation)
# ==========================================
logger.add(
    config.LOG_DIR / "taxai_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",  # Always log DEBUG to file
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{name}:{function}:{line} | "
        "{message}"
    ),
    encoding="utf-8",
)

# ==========================================
# ERROR FILE HANDLER (errors only)
# ==========================================
logger.add(
    config.LOG_DIR / "errors_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="ERROR",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "{name}:{function}:{line} | "
        "{message}\n"
        "{exception}"
    ),
    encoding="utf-8",
)

# ==========================================
# AUDIT LOG HANDLER (if enabled)
# ==========================================
if config.ENABLE_AUDIT_LOG:
    logger.add(
        config.AUDIT_LOG_DIR / "audit_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="90 days",  # Keep audit logs longer
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
        encoding="utf-8",
        filter=lambda record: "AUDIT" in record["extra"]
    )


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def log_audit(event: str, details: dict = None):
    """
    Log audit event
    
    Usage:
        log_audit("USER_QUERY", {"query": "...", "user_id": "..."})
    """
    logger.bind(AUDIT=True).info(
        f"EVENT={event} | DETAILS={details or {}}"
    )


def log_parsing_progress(current: int, total: int, doc_name: str):
    """Log parsing progress"""
    percentage = (current / total * 100) if total > 0 else 0
    logger.info(
        f"📄 Parsing {doc_name}: {current}/{total} ({percentage:.1f}%)"
    )


def log_model_usage(model: str, tokens: int, latency_ms: float):
    """Log LLM usage"""
    logger.info(
        f"🤖 Model: {model} | Tokens: {tokens} | Latency: {latency_ms:.0f}ms"
    )


# ==========================================
# STARTUP MESSAGE
# ==========================================
if config.DEBUG_MODE:
    logger.debug("🐛 Debug mode enabled")

logger.info(f"🚀 TaxAI 2026 initialized - Mode: {config.SYSTEM_MODE}")
logger.info(f"📊 Logging level: {config.LOG_LEVEL}")
logger.info(f"💾 Log directory: {config.LOG_DIR}")


# ==========================================
# EXPORTS
# ==========================================
__all__ = [
    "logger",
    "log_audit",
    "log_parsing_progress",
    "log_model_usage",
]