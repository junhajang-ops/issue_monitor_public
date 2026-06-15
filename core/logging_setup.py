"""stdout/stderr를 콘솔 + 일별 로그파일에 동시 기록(tee)하고 30일 후 자동 삭제.

- 파일: data/logs/issue_monitor.log (자정마다 issue_monitor.log.YYYY-MM-DD 로 로테이션)
- 보관: backupCount=30 → 30개(=30일) 초과분은 로테이션 시 자동 삭제.
- 기존 print() 코드는 그대로 둔 채 sys.stdout/err만 래핑하므로 호출부 수정이 없다.

main 프로세스 시작 시 setup_file_logging()을 1회 호출한다.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import config


class _StreamToLogger:
    """sys.stdout/err를 가로채 (1) 원래 콘솔에 그대로 출력 + (2) logger에 줄 단위 기록."""

    def __init__(self, orig, logger: logging.Logger, level: int) -> None:
        self._orig = orig
        self._logger = logger
        self._level = level
        self._buf = ""

    def write(self, message: str) -> int:
        if self._orig is not None:
            try:
                self._orig.write(message)
            except Exception:
                pass
        self._buf += message
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._logger.log(self._level, line)
        return len(message)

    def flush(self) -> None:
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return bool(getattr(self._orig, "isatty", lambda: False)())


_CONFIGURED = False


def setup_file_logging() -> Path | None:
    """stdout/stderr를 콘솔+일별 로그파일에 동시 기록하도록 설정. 반환: 로그파일 경로.

    - 자정마다 로테이션, 30일(backupCount=30) 초과분 자동 삭제.
    - 중복 호출은 무시(_CONFIGURED).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return None

    logdir = config.DATA_DIR / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / "issue_monitor.log"

    handler = TimedRotatingFileHandler(
        str(logfile),
        when="midnight",
        interval=1,
        backupCount=30,  # 30일치만 유지, 초과분은 로테이션 시 자동 삭제
        encoding="utf-8",
        delay=True,
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))

    logger = logging.getLogger("issue_monitor.console")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False

    sys.stdout = _StreamToLogger(sys.__stdout__, logger, logging.INFO)
    sys.stderr = _StreamToLogger(sys.__stderr__, logger, logging.ERROR)

    _CONFIGURED = True
    return logfile
