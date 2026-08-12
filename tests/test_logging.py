from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from parsezen.__main__ import (
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    configure_logging,
)
from parsezen.processing import ProcessRequest, process_document


def test_configures_one_rotating_local_log(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path / "logs")
    logger = logging.getLogger("parsezen")

    try:
        handlers = [
            handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)
        ]
        assert log_path == tmp_path / "logs" / "parsezen.log"
        assert log_path.exists()
        assert len(handlers) == 1
        assert handlers[0].maxBytes == LOG_MAX_BYTES
        assert handlers[0].backupCount == LOG_BACKUP_COUNT
    finally:
        _close_app_log_handlers(logger)


def test_processing_log_omits_document_name_path_and_content(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path / "logs")
    logger = logging.getLogger("parsezen")
    source = tmp_path / "private-customer-name.txt"
    source.write_text("TOP SECRET DOCUMENT CONTENT", encoding="utf-8")

    try:
        result = process_document(ProcessRequest(source, convert_to_markdown=True))
        assert result.final_path.exists()
        for handler in logger.handlers:
            handler.flush()
        log_text = log_path.read_text(encoding="utf-8")
    finally:
        _close_app_log_handlers(logger)

    assert "processing_started" in log_text
    assert "processing_completed" in log_text
    assert "extension=.txt" in log_text
    attempt_ids = set(re.findall(r"attempt_id=([0-9a-f]{32})", log_text))
    assert len(attempt_ids) == 1
    assert "private-customer-name" not in log_text
    assert str(tmp_path) not in log_text
    assert "TOP SECRET DOCUMENT CONTENT" not in log_text


def _close_app_log_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
