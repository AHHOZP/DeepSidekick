# Spine 运行时

`spine38.dll` / `spine40.dll` / `spine41.dll` / `spine42.dll`：spine-c 对应版本的编译产物（x64），
用 `tools/spine/` 里的桥接层编译，带 3 处本地补丁（源码里搜 `DeepSidekick 补丁`）。

它们**不是**本项目 MIT 许可的一部分，适用同目录的 `SPINE-RUNTIMES-LICENSE-<版本>.txt`
（Spine Runtimes License，Esoteric Software LLC）。许可全文与二进制必须一起分发，删掉就不能再分发。

使用 Spine 形象需要你自行持有 Spine Editor 许可（https://esotericsoftware.com/spine-purchase）。

重建方法见 `tools/spine/build.ps1`；不随仓库分发 spine-c 源码。
