# Build a patched spine-c DLL with VS2022 BuildTools.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\spine\build.ps1 `
#     -Version 3.8 -SourceRoot <spine-runtimes-3.8>\spine-c\spine-c
#
# SourceRoot must point at the upstream spine-c directory containing include/
# and src/spine/. The script stages the bridge sources inside SourceRoot and
# compiles with relative source paths, so __FILE__ does not embed this machine's
# absolute build path. Remove _deepsidekick_build after a failed build.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
if (-not $OutDir) { $OutDir = Join-Path $here "..\..\vendor\spine" }
$OutDir = [IO.Path]::GetFullPath($OutDir)

$inc = Join-Path $SourceRoot "include"
$srcDir = Join-Path $SourceRoot "src\spine"
if (-not (Test-Path $inc) -or -not (Test-Path $srcDir)) {
    throw "SourceRoot 不是 spine-c 目录（缺少 include/ 或 src/spine/）：$SourceRoot"
}

# Discover Visual Studio instead of hard-coding one installation path.
$vcvars = ""
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $vswhere) {
    $install = @(& $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath)[0]
    if ($install) {
        $vcvars = Join-Path $install "VC\Auxiliary\Build\vcvars64.bat"
    }
}
if (-not $vcvars -or -not (Test-Path $vcvars)) {
    throw "找不到 VS2022 BuildTools（vcvars64.bat）；请先安装 C++ 生成工具"
}

$stage = Join-Path $SourceRoot "_deepsidekick_build"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage, $OutDir | Out-Null
Copy-Item (Join-Path $here "spine_stub.c") $stage
Copy-Item (Join-Path $here "spine_renpy.c") $stage
Copy-Item (Join-Path $here "export_config.h") $stage

$verNum = $Version -replace '\.', ''      # 3.8 -> 38
$out = Join-Path $OutDir "spine$verNum.dll"
$obj = Join-Path $stage "obj"
New-Item -ItemType Directory -Force -Path $obj | Out-Null

$srcs = @(Get-ChildItem (Join-Path $srcDir "*.c") | ForEach-Object {
    "src\spine\" + $_.Name
})
$srcs += "_deepsidekick_build\spine_stub.c"
$srcs += "_deepsidekick_build\spine_renpy.c"

$cmd = @"
call "$vcvars" >nul || exit /b 1
cd /d "$SourceRoot"
cl /nologo /O2 /LD /FI "export_config.h" /D SPINE_RENPY_VER=$verNum /I "_deepsidekick_build" /I "include" /Fo"_deepsidekick_build\obj\\" $($srcs -join ' ') /Fe:"$out"
"@
$cmd | Out-File -Encoding ascii (Join-Path $obj "build.bat")
& cmd /c (Join-Path $obj "build.bat")
if ($LASTEXITCODE -ne 0) { throw "编译失败：$Version" }

Remove-Item -Recurse -Force $stage
Remove-Item -Force -ErrorAction SilentlyContinue `
    (Join-Path $OutDir "spine$verNum.lib"),
    (Join-Path $OutDir "spine$verNum.exp")
Write-Host ">>> $out"
