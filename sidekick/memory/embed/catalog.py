"""语义记忆模型清单：WebUI 模型表、下载、校验与编码模板的唯一来源。

一个条目 = 一个 FastEmbed 模型名 + 需要落到 `state/embed/<id>/` 的文件，
外加检索侧的 query/doc 前缀和池化方式。模型源优先 HF，失败后切 ModelScope。

体积/哈希来自 HF 仓库文件元数据；小文件只校验大小。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HF = "https://huggingface.co/{repo}/resolve/main"
MS = "https://modelscope.cn/models/{repo}/resolve/master"

QWEN_INSTRUCT = ("Instruct: Given a Chinese conversation, retrieve relevant "
                 "memories and setting entries\nQuery: ")


@dataclass(frozen=True, slots=True)
class FileSpec:
    path: str
    bytes: int
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    model_name: str
    name: str
    files: tuple[FileSpec, ...]
    hf_repo: str
    ms_repo: str
    dim: int
    max_context: int
    template: str
    pooling: str = ""
    query_prefix: str = ""
    doc_prefix: str = ""
    custom: bool = False
    note: str = ""
    license: str = "Apache-2.0"
    langs: str = ""
    latency: str = ""
    mem: str = ""
    cmteb: str = ""
    mteb: str = ""
    mmteb: str = ""

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    @property
    def size(self) -> str:
        return f"{self.bytes / 1e9:.2f} GB"

    def base(self, source: str) -> str:
        if source == "ModelScope":
            return MS.format(repo=self.ms_repo)
        return HF.format(repo=self.hf_repo)

    def url(self, spec: FileSpec, source: str) -> str:
        return f"{self.base(source)}/{spec.path}"

    def missing(self, root) -> list[str]:
        base = Path(root)
        return [f.path for f in self.files if not (base / f.path).is_file()]


CATALOG: tuple[Entry, ...] = (
    Entry(
        id="qwen3-embedding-0.6b-q",
        model_name="Qwen/Qwen3-Embedding-0.6B-Q",
        name="Qwen3-Embedding-0.6B（int8）",
        files=(
            FileSpec("onnx/model_quantized.onnx", 1_120_604_114,
                     "b39d0623bfc7d2dfc2dcc0d32152c5c5b8d61e9e74679e8222dcce5cf32486b9"),
            FileSpec("tokenizer.json", 11_423_705,
                     "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"),
            FileSpec("tokenizer_config.json", 5_402),
            FileSpec("special_tokens_map.json", 613),
            FileSpec("config.json", 747),
        ),
        hf_repo="Qdrant/Qwen3-Embedding-0.6B-onnx",
        ms_repo="Qdrant/Qwen3-Embedding-0.6B-onnx",
        dim=1024, max_context=32768, template="qwen-instr",
        pooling="last", query_prefix=QWEN_INSTRUCT,
        langs="100+ 语种",
        latency="60–140 ms / 句", mem="1.5–2.2 GB",
        cmteb="66.33", mteb="65.80", mmteb="64.33",
        note="Qwen3-0.6B 596M，28 层 GQA；阿里 Qwen；指令感知 + MRL，32K 上下文",
    ),
    Entry(
        id="qwen3-embedding-0.6b",
        model_name="Qwen/Qwen3-Embedding-0.6B",
        name="Qwen3-Embedding-0.6B（fp32）",
        files=(
            FileSpec("onnx/model.onnx", 4_031_521,
                     "754d6ba1c7a323d7b3af0d85b8268f8b716b0cd362ce73295e0f71d3dd831639"),
            FileSpec("onnx/model.onnx_data", 2_383_106_048,
                     "15d02a315e3b95296700c73d72a436e79facf4e17d919345bed730e98433bcb4"),
            FileSpec("tokenizer.json", 11_423_705,
                     "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"),
            FileSpec("tokenizer_config.json", 5_402),
            FileSpec("special_tokens_map.json", 613),
            FileSpec("config.json", 747),
        ),
        hf_repo="Qdrant/Qwen3-Embedding-0.6B-onnx",
        ms_repo="Qdrant/Qwen3-Embedding-0.6B-onnx",
        dim=1024, max_context=32768, template="qwen-instr",
        pooling="last", query_prefix=QWEN_INSTRUCT,
        langs="100+ 语种",
        latency="120–260 ms / 句", mem="3.0–4.0 GB",
        cmteb="66.33", mteb="65.80", mmteb="64.33",
        note="同 int8（596M / 28 层 GQA）；阿里 Qwen；无量化损失，内存翻倍，日常建议 int8",
    ),
    Entry(
        id="jina-embeddings-v3",
        model_name="jinaai/jina-embeddings-v3",
        name="jina-embeddings-v3",
        files=(
            FileSpec("onnx/model.onnx", 1_513_159,
                     "836322cc5d78f0047dbb336d0049b087fd81e164505c347a313cd44a772f34cc"),
            FileSpec("onnx/model.onnx_data", 2_291_339_168,
                     "022b369faa8015add1d036a72927ed64f8c8196435bc37a4fc9d36fc8ebc5cc2"),
            FileSpec("tokenizer.json", 17_082_756,
                     "f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af"),
            FileSpec("tokenizer_config.json", 1_148),
            FileSpec("special_tokens_map.json", 964),
            FileSpec("config.json", 1_799),
        ),
        hf_repo="jinaai/jina-embeddings-v3",
        ms_repo="jinaai/jina-embeddings-v3",
        dim=1024, max_context=8192, template="jina-task",
        pooling="mean", license="CC-BY-NC-4.0",
        langs="89+ 语种",
        latency="110–230 ms / 句", mem="2.8–3.5 GB",
        cmteb="64.3", mteb="65.52", mmteb="67.4",
        note="XLM-R 570M + RoPE；Jina AI；5 组 Task-LoRA + MRL，仅限非商用",
    ),
    Entry(
        id="bge-m3",
        model_name="BAAI/bge-m3",
        name="BGE-M3",
        files=(
            FileSpec("onnx/model.onnx", 724_923,
                     "f84251230831afb359ab26d9fd37d5936d4d9bb5d1d5410e66442f630f24435b"),
            FileSpec("onnx/model.onnx_data", 2_266_820_608,
                     "1eebfb28493f67bba03ce0ef64bfdc7fc5a3bd9d7493f818bb1d78cd798416b4"),
            FileSpec("tokenizer.json", 17_098_108,
                     "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08"),
            FileSpec("tokenizer_config.json", 444),
            FileSpec("special_tokens_map.json", 964),
            FileSpec("config.json", 687),
        ),
        hf_repo="BAAI/bge-m3",
        ms_repo="BAAI/bge-m3",
        dim=1024, max_context=8192, template="bge-cls",
        pooling="cls", custom=True, license="MIT",
        langs="100+ 语种",
        latency="120–250 ms / 句", mem="2.8–3.5 GB",
        cmteb="64.5", mteb="63.0", mmteb="69.2",
        note="XLM-R-large 568M；智源 BAAI；稠密 + 稀疏 + 多向量三合一，多语言标杆",
    ),
)

_BY_ID = {e.id: e for e in CATALOG}


def all_entries() -> tuple[Entry, ...]:
    return CATALOG


def get(mid: str) -> Entry | None:
    return _BY_ID.get(str(mid or ""))
