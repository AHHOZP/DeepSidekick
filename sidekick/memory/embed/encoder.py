"""FastEmbed 封装：只暴露 load/query/documents 三个动作。

上层不需要知道 ONNX、tokenizer、池化；换模型/换实现只改这一层。
不同模型的 query/doc 前缀、池化方式由 catalog 条目提供；Qwen 的指令
不交给 FastEmbed，由这里拼。
"""
from __future__ import annotations

import numpy as np

_CUSTOM_REGISTERED: set[str] = set()


def _pooling(name: str):
    from fastembed.common.model_description import PoolingType

    return {
        "cls": PoolingType.CLS,
        "mean": PoolingType.MEAN,
        "last": PoolingType.LAST_TOKEN,
    }.get(str(name or "").lower(), PoolingType.MEAN)


class FastEmbedEncoder:
    def __init__(self, entry, path, threads: int = 4) -> None:
        self.entry = entry
        self.path = str(path)
        self.threads = max(1, int(threads))
        self._model = None
        self.dim = 0

    def _register_custom(self) -> None:
        if not getattr(self.entry, "custom", False):
            return
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource

        if self.entry.model_name in _CUSTOM_REGISTERED:
            return
        onnx = next((f.path for f in self.entry.files
                     if f.path.endswith(".onnx")), "onnx/model.onnx")
        extra = [f.path for f in self.entry.files
                 if "onnx_data" in f.path or f.path.endswith(".onnx_data")]
        TextEmbedding.add_custom_model(
            model=self.entry.model_name,
            pooling=_pooling(self.entry.pooling),
            normalization=True,
            sources=ModelSource(hf=self.entry.hf_repo),
            dim=int(self.entry.dim),
            model_file=onnx,
            additional_files=extra,
            license=self.entry.license,
        )
        _CUSTOM_REGISTERED.add(self.entry.model_name)

    def load(self) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise RuntimeError(
                "没有安装 fastembed；执行 pip install -r requirements.txt") from e
        self._register_custom()
        try:
            self._model = TextEmbedding(
                model_name=self.entry.model_name,
                specific_model_path=self.path,
                threads=self.threads,
                providers=["CPUExecutionProvider"],
            )
            self.dim = int(self._model.embedding_size)
        except Exception as e:
            raise RuntimeError(f"加载语义模型失败：{type(e).__name__}: {e}") from e

    @property
    def ready(self) -> bool:
        return self._model is not None

    def query(self, text: str) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("语义模型还没加载")
        vec = next(iter(self._model.query_embed(
            [self.entry.query_prefix + str(text)])))
        return self._normalize(vec)

    def documents(self, texts: list[str]) -> list[np.ndarray]:
        if self._model is None:
            raise RuntimeError("语义模型还没加载")
        docs = [self.entry.doc_prefix + str(x) for x in texts]
        return [self._normalize(x) for x in self._model.embed(docs)]

    @staticmethod
    def _normalize(vec) -> np.ndarray:
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(arr))
        return arr / norm if norm > 0 else arr

    def identity(self) -> str:
        return (f"{self.entry.id}|d{self.dim}|{self.entry.template}|"
                f"mx{self.entry.max_context}|fmt-1")
