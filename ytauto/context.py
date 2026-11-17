"""RunContext: folder kerja per run, logger, dan penanda langkah pipeline."""
from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .config import Config


class StepError(Exception):
    """Dibungkus di sekitar error apa pun supaya kita tahu langkah mana yang gagal."""

    def __init__(self, step: str, cause: BaseException):
        super().__init__(f"[{step}] {type(cause).__name__}: {cause}")
        self.step = step
        self.cause = cause


def setup_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    # Teruskan log modul lain (retry, dll.) ke handler yang sama
    for lib in ("ytauto.retry",):
        lg = logging.getLogger(lib)
        lg.handlers = logger.handlers
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return logger


def read_tail(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


@dataclass
class RunContext:
    run_id: str
    config: Config
    dry_run: bool = False
    scope: str = "plan"  # "plan", "video_0", ..., "finalize"
    logger: logging.Logger = field(init=False)

    def __post_init__(self) -> None:
        self.logger = setup_logger(f"ytauto.{self.run_id}.{self.scope}", self.dir() / "run.log")

    @property
    def run_root(self) -> Path:
        return self.config.path(self.config.paths.work_dir) / self.run_id

    def dir(self, *parts: str) -> Path:
        p = self.run_root / self.scope
        for part in parts:
            p = p / part
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_file(self) -> Path:
        return self.run_root / self.scope / "run.log"

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir() / name
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def read_json(self, name: str) -> Any:
        return json.loads((self.dir() / name).read_text(encoding="utf-8"))

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        self.logger.info("▶ %s", name)
        try:
            yield
        except StepError:
            raise
        except Exception as e:  # noqa: BLE001 - semua error dibungkus dengan nama langkah
            self.logger.error("✖ %s gagal: %s: %s", name, type(e).__name__, e)
            raise StepError(name, e) from e
        self.logger.info("✔ %s selesai", name)
