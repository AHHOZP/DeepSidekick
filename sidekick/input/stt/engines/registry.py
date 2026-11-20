"""模型注册表：YAML 里的 `stt.models` → `ModelSpec` → 具体的 OfflineRecognizer。

一份 sherpa-onnx 的 `OfflineRecognizer` 覆盖 SenseVoice / Paraformer /
Whisper / FireRedASR / Qwen3-ASR / FunASR-Nano 等十几个模型族，所以这里
是"一个引擎 + 一张表"，而不是 N 个 adapter。新增模型只改 YAML。

    stt:
      model_dir: state/asr
      models:
        sensevoice-small:
          kind: sense-voice
          dir: sensevoice-small          # 相对 model_dir
          args: {model: model.int8.onnx, tokens: tokens.txt}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# args 里这些 key 的值是"相对模型目录的文件名"，其余原样传给 sherpa。
FILE_KEYS = {
    "model", "tokens", "encoder", "decoder", "conv_frontend", "encoder_adaptor",
    "llm", "embedding", "joiner", "tokenizer",
}

# 按角色找文件时认的关键词（见 ModelSpec._guess）。
WORDS: dict[str, tuple[str, ...]] = {
    "model": ("model",),
    "encoder": ("encoder",),
    "decoder": ("decoder",),
    "joiner": ("joiner",),
    "tokens": ("tokens",),
    "conv_frontend": ("conv_frontend", "conv-frontend"),
    "encoder_adaptor": ("encoder_adaptor",),
    "llm": ("llm",),
    "embedding": ("embedding",),
}


@dataclass(slots=True)
class ModelSpec:
    id: str
    kind: str
    dir: Path
    args: dict = field(default_factory=dict)

    def path_of(self, key: str) -> str:
        """args[key] → 绝对路径（相对项拼 `dir`）。没写就按文件名自动找。"""
        v = str(self.args.get(key) or "")
        if v:
            p = Path(v)
            return v if p.is_absolute() else str(self.dir / v)
        return self._guess(key)

    def _guess(self, key: str) -> str:
        """在模型目录里按习惯命名找文件。

        发布包的命名不统一（whisper 是 `small-encoder.int8.onnx`，
        sensevoice 是 `model.int8.onnx`，paraformer 是 `model.int8.onnx`），
        所以不写死文件名，按角色匹配；int8 优先，其次名字最短的。
        """
        if not self.dir.is_dir():
            return ""
        if key == "tokenizer":
            d = self.dir / "tokenizer"
            return str(d) if d.is_dir() else ""

        words = WORDS.get(key, (key,))
        try:
            files = [p for p in self.dir.rglob("*") if p.is_file()]
        except OSError:
            return ""
        suffix = ".txt" if key == "tokens" else ".onnx"
        cand = [p for p in files
                if p.suffix.lower() == suffix
                and any(w in p.name.lower() for w in words)]
        if not cand:
            return ""
        best = min(cand, key=lambda p: (0 if ".int8." in p.name.lower() else 1,
                                        len(p.name)))
        return str(best)

    def missing(self) -> list[str]:
        """列出这个 kind 必需、但找不到的东西（返回路径或说明文字）。"""
        out = []
        for k in REQUIRED_FILES.get(self.kind, ()):
            p = self.path_of(k)
            if not p or not Path(p).exists():
                out.append(p or f"{k}（目录里没找到）")
        return out


# 每种 kind 至少要有的东西。顺序就是报错顺序。
REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    "sense-voice": ("model", "tokens"),
    "fire-red-asr-ctc": ("model", "tokens"),
    "fire-red-asr": ("encoder", "decoder", "tokens"),
    "paraformer": ("model", "tokens"),
    "qwen3-asr": ("conv_frontend", "encoder", "decoder", "tokenizer"),
    "funasr-nano": ("encoder_adaptor", "llm", "embedding", "tokenizer"),
    "whisper": ("encoder", "decoder", "tokens"),
}


def load_specs(cfg: dict, base: Path) -> dict[str, ModelSpec]:
    """读 `stt.models`。base 是项目根（config.ROOT）。"""
    st = cfg.get("stt") or {}
    root = Path(str(st.get("model_dir") or "state/asr"))
    if not root.is_absolute():
        root = Path(base) / root

    out: dict[str, ModelSpec] = {}
    for mid, raw in (st.get("models") or {}).items():
        raw = raw or {}
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        if not kind:
            raise ValueError(f"stt.models.{mid}.kind 不能为空")
        kind = "funasr-nano" if kind == "funasr_nano" else kind
        d = root / str(raw.get("dir") or mid)
        out[str(mid)] = ModelSpec(
            id=str(mid),
            kind=kind,
            dir=d,
            args=dict(raw.get("args") or {}),
        )
    return out


def build(spec: ModelSpec, setup: dict):
    """kind → OfflineRecognizer。新增模型族只在这里加分支。

    文件路径不用在 YAML 里写死：`REQUIRED_FILES` 说这个 kind 要哪几样，
    `ModelSpec.path_of` 负责"args 里写了就用，没写就在目录里按文件名找"。
    """
    import sherpa_onnx

    keys = set(REQUIRED_FILES.get(spec.kind, ())) | {
        k for k in spec.args if k in FILE_KEYS}
    paths = {k: spec.path_of(k) for k in keys}
    opts = {k: v for k, v in spec.args.items() if k not in FILE_KEYS}
    threads = max(1, int(setup.get("num_threads") or 4))
    # 官方 pip wheel 只有 CPU 版，设备不是可选项
    kw = dict(num_threads=threads, provider="cpu", debug=False)

    def need(*names: str) -> None:
        miss = [names_ for names_ in names if not paths.get(names_)]
        if miss:
            hint = "、".join(f"{n}" for n in miss)
            raise FileNotFoundError(
                f"{spec.id}（{spec.kind}）在 {spec.dir} 里找不到：{hint}")

    if spec.kind == "sense-voice":
        need("model", "tokens")
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=paths["model"], tokens=paths["tokens"],
            language=str(opts.get("language") or "zh"),
            use_itn=bool(opts.get("use_itn", True)), **kw)

    if spec.kind == "fire-red-asr-ctc":
        need("model", "tokens")
        return sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
            model=paths["model"], tokens=paths["tokens"], **kw)

    if spec.kind == "fire-red-asr":
        need("encoder", "decoder", "tokens")
        return sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
            encoder=paths["encoder"], decoder=paths["decoder"],
            tokens=paths["tokens"], **kw)

    if spec.kind == "paraformer":
        need("model", "tokens")
        return sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=paths["model"], tokens=paths["tokens"], **kw)

    if spec.kind == "qwen3-asr":
        need("conv_frontend", "encoder", "decoder", "tokenizer")
        return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=paths["conv_frontend"], encoder=paths["encoder"],
            decoder=paths["decoder"], tokenizer=paths["tokenizer"],
            max_new_tokens=int(opts.get("max_new_tokens") or 256),
            hotwords=str(opts.get("hotwords") or ""), **kw)

    if spec.kind == "funasr-nano":
        need("encoder_adaptor", "llm", "embedding", "tokenizer")
        return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=paths["encoder_adaptor"], llm=paths["llm"],
            embedding=paths["embedding"], tokenizer=paths["tokenizer"], **kw)

    if spec.kind == "whisper":
        need("encoder", "decoder", "tokens")
        return sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=paths["encoder"], decoder=paths["decoder"],
            tokens=paths["tokens"],
            language=str(opts.get("language") or "zh"), **kw)

    raise ValueError(f"未知模型族 kind={spec.kind}（在 registry.build 里补分支）")
