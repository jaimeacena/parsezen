param(
    [switch]$InstallInnoSetup
)

$ErrorActionPreference = "Stop"
$repository = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repository
$venvPython = Join-Path $repository ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
}
else {
    (Get-Command python -ErrorAction Stop).Source
}
$buildCache = Join-Path $env:LOCALAPPDATA "Parsezen\BuildCache\pyinstaller"
$packageDirectory = Join-Path $repository "outputs\package"
New-Item -ItemType Directory -Force -Path $buildCache, $packageDirectory | Out-Null
$bundleDirectory = Join-Path $packageDirectory "Parsezen"
if (Test-Path -LiteralPath $bundleDirectory) {
    $resolvedBundle = (Resolve-Path -LiteralPath $bundleDirectory).Path
    $resolvedParent = [System.IO.Path]::GetDirectoryName($resolvedBundle)
    if (-not $resolvedParent.Equals(
        $packageDirectory,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "La carpeta de paquete resuelta queda fuera de outputs\package."
    }
    Remove-Item -LiteralPath $resolvedBundle -Recurse -Force
}

& $python "distribution\windows\generate_notices.py" `
    --output "distribution\windows\THIRD-PARTY-NOTICES.txt"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $python -m PyInstaller --noconfirm --clean `
    --workpath $buildCache `
    --distpath $packageDirectory `
    "distribution\windows\Parsezen.spec"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$applicationPath = Join-Path $packageDirectory "Parsezen\Parsezen.exe"
$env:QT_QPA_PLATFORM = "offscreen"
$smokeProcess = Start-Process `
    -FilePath $applicationPath `
    -ArgumentList "--package-smoke" `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($smokeProcess.ExitCode -ne 0) {
    throw "El ejecutable empaquetado no superó la prueba de arranque."
}

if (-not $InstallInnoSetup) {
    exit 0
}

function Find-InnoSetupCompiler {
    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $compilerCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $compilerCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

$compilerPath = Find-InnoSetupCompiler
if (-not $compilerPath) {
    $chocolatey = Get-Command choco.exe -ErrorAction SilentlyContinue
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($chocolatey) {
        & $chocolatey.Source install innosetup --yes --no-progress
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    elseif ($winget) {
        & $winget.Source install `
            --id JRSoftware.InnoSetup `
            --exact `
            --silent `
            --accept-package-agreements `
            --accept-source-agreements `
            --disable-interactivity
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    else {
        throw "Instala Inno Setup 6: no se encontró Chocolatey ni WinGet."
    }
    $compilerPath = Find-InnoSetupCompiler
}
if (-not $compilerPath) {
    throw "Inno Setup 6 no está disponible para crear el instalador."
}

& $compilerPath "distribution\windows\Parsezen.iss"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
