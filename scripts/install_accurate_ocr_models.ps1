param(
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path (Split-Path -Parent $PSScriptRoot) "averon_import\models\tessdata_best"
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

$models = @{
    "rus.traineddata" = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/rus.traineddata"
    "eng.traineddata" = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/eng.traineddata"
}

Write-Host "Installing official tessdata_best models..."
foreach ($name in $models.Keys) {
    $target = Join-Path $Destination $name
    $temporary = "$target.download"
    Write-Host "  Downloading $name"
    Invoke-WebRequest -UseBasicParsing -Uri $models[$name] -OutFile $temporary
    $size = (Get-Item $temporary).Length
    if ($size -lt 1000000) {
        Remove-Item $temporary -Force -ErrorAction SilentlyContinue
        throw "Downloaded file $name is unexpectedly small."
    }
    Move-Item -Force $temporary $target
}

Write-Host "Accurate OCR models installed into: $Destination"
