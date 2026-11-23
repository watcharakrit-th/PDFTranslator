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

    # GPU safety. All limits are the GPU *hot spot* (its hottest sensor), which on the
    # RTX 2070 SUPER runs about 25 °C above the core temperature under load.
    gpu_device: str = "cuda"  # "cuda" or "cpu"
    gpu_pause_at_c: int = 75  # no new GPU work at or above this...
    gpu_resume_at_c: int = 65  # ...until the hot spot is back at this
    gpu_abort_at_c: int = 90  # a translation request is stopped the moment it gets here
    gpu_hotspot_margin_c: int = 25  # where the hot spot can't be read: core + this
    # Refuse long GPU jobs while the card's power limit is above this (0 = don't check).
    # The limit resets to the default (235 W on the RTX 2070 SUPER) after every reboot.
    gpu_max_power_w: int = 0
    gpu_cooldown_s: float = 2.0
    docling_batch_pages: int = 5
    # After each translation request the GPU rests for this fraction of the request's
    # time (1.0 = busy at most half the time).
    gpu_rest_ratio: float = 1.0

    # Translation (M3)
    translate_engine: str = "typhoon"
    translate_model: str = "scb10x/typhoon-translate1.5-4b"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_exe: Path | None = None  # default: .cache/tools/ollama, then PATH

    # Embeddings (M5): BGE-M3 dense vectors; texts per GPU batch; longer texts are cut.
    embed_model: str = "BAAI/bge-m3"
    embed_batch: int = 16
    embed_max_tokens: int = 512

    @property
    def cache_dir(self) -> Path:
        return self.home / ".cache"

    @property
    def ollama_models_dir(self) -> Path:
        return self.cache_dir / "ollama" / "models"

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
        "PYTHAINLP_DATA_DIR": cache / "pythainlp",
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
