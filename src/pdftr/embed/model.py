"""BGE-M3 dense embeddings (1024 dimensions), on the GPU under the GPU guard.

Only the files the dense model needs are downloaded (about 2.2 GB, into the Hugging
Face cache on drive D). The vector is BGE-M3's dense output: the normalised [CLS]
state of the last layer, so cosine distance and inner product agree.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from pdftr.gpu import GpuGuard

MODEL_FILES = [
    "config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]
WEIGHTS = ("model.safetensors", "pytorch_model.bin")  # BAAI/bge-m3 only has the .bin
DIM = 1024


def model_dir(model_id: str) -> str:
    """The local model folder: from the cache if complete (no network), else downloaded
    with just the config, tokenizer and one weights file."""
    from huggingface_hub import HfApi, snapshot_download

    try:
        path = snapshot_download(
            model_id, allow_patterns=[*MODEL_FILES, *WEIGHTS], local_files_only=True
        )
        if any((Path(path) / w).exists() for w in WEIGHTS):
            return path
    except (OSError, ValueError):  # not cached yet
        pass
    files = {f.rfilename for f in HfApi().model_info(model_id).siblings}
    weights = next((w for w in WEIGHTS if w in files), None)
    if weights is None:
        raise OSError(f"{model_id} has no model weights to download")
    return snapshot_download(model_id, allow_patterns=[*MODEL_FILES, weights])


class Embedder(Protocol):
    name: str

    def encode(self, texts: list[str]) -> np.ndarray:
        """Unit-length float32 vectors, one row per text."""
        ...


class BgeM3:
    """BAAI/bge-m3 through transformers; loaded on first use."""

    def __init__(
        self,
        model_id: str = "BAAI/bge-m3",
        *,
        device: str = "cuda",
        batch_size: int = 16,
        max_tokens: int = 512,
        guard: GpuGuard | None = None,
        rest_ratio: float = 1.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.model_id = model_id
        self.name = model_id.split("/")[-1].lower()  # "bge-m3", part of every embed key
        self.device = device
        self.batch_size = batch_size
        self.max_tokens = max_tokens
        self.guard = guard
        self.rest_ratio = rest_ratio
        self.log = log
        self._model = None
        self._tokenizer = None

    def load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer

        if self.device == "cuda" and not torch.cuda.is_available():
            self.log("No CUDA GPU available: embedding on the CPU (slower)")
            self.device = "cpu"
        started = time.monotonic()
        path = model_dir(self.model_id)
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        self._tokenizer = AutoTokenizer.from_pretrained(path)
        self._model = AutoModel.from_pretrained(path, dtype=dtype).to(self.device).eval()
        self.log(f"Loaded {self.model_id} on {self.device} in {time.monotonic() - started:.0f}s")

    def unload(self) -> None:
        """Free the GPU memory now."""
        if self._model is None:
            return
        import torch

        self._model = self._tokenizer = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def encode(self, texts: list[str]) -> np.ndarray:
        import torch

        self.load()
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))  # less padding
        for start in range(0, len(order), self.batch_size):
            idx = order[start : start + self.batch_size]
            if self.guard is not None and self.device == "cuda":
                self.guard.check()
            began = time.monotonic()
            batch = self._tokenizer(
                [texts[i] for i in idx],
                padding=True,
                truncation=True,
                max_length=self.max_tokens,
                return_tensors="pt",
            ).to(self.device)
            if self.guard is not None and self.device == "cuda":
                with self.guard.watch(), torch.inference_mode():
                    hidden = self._model(**batch).last_hidden_state[:, 0]
            else:
                with torch.inference_mode():
                    hidden = self._model(**batch).last_hidden_state[:, 0]
            vectors = torch.nn.functional.normalize(hidden.float(), dim=-1).cpu().numpy()
            out[idx] = vectors
            if self.guard is not None and self.device == "cuda":
                self.guard.rest(time.monotonic() - began, self.rest_ratio)
        return out


def make_embedder(settings, guard: GpuGuard | None = None, log=print) -> BgeM3:
    return BgeM3(
        settings.embed_model,
        device=settings.gpu_device,
        batch_size=settings.embed_batch,
        max_tokens=settings.embed_max_tokens,
        guard=guard,
        rest_ratio=settings.gpu_rest_ratio,
        log=log,
    )
