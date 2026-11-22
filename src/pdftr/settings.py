"""Settings and data locations.

Import this module before any heavy library (docling, torch, transformers):
``configure_environment`` points every model cache and temp folder at the data
drive, because drive C on the development machine is nearly full.
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDFTR_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Root for caches, models, outputs. Defaults to the project folder.
    home: Path = PROJECT_ROOT
    database_url: str = "postgresql://pdftr:pdftr@127.0.0.1:5433/pdftr"

    # GPU safety: work runs in small batches; between batches the GPU is
    # checked and the run pauses while it is hotter than ``gpu_pause_at_c``.
    gpu_device: str = "cuda"  # "cuda" or "cpu"
    gpu_pause_at_c: int = 75
    gpu_resume_at_c: int = 65
    gpu_cooldown_s: float = 2.0
    docling_batch_pages: int = 5

    @property
    def cache_dir(self) -> Path:
        return self.home / ".cache"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def configure_environment(settings: Settings | None = None) -> None:
    """Send model caches and temp files to the data drive (idempotent)."""
    s = settings or get_settings()
    cache = s.cache_dir
    tmp = cache / "tmp"
    targets = {
        "HF_HOME": cache / "huggingface",
        "TORCH_HOME": cache / "torch",
        "XDG_CACHE_HOME": cache,
        "TMP": tmp,
        "TEMP": tmp,
        "TMPDIR": tmp,
    }
    for key, path in targets.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    tempfile.tempdir = str(tmp)
    s.data_dir.mkdir(parents=True, exist_ok=True)
