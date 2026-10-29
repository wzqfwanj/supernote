param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$LangTag = "zh-Hans-CN"
)

# Windows PowerShell 5.1 reads .ps1 as ANSI by default, so this file is kept ASCII-only.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

    # Locate the generic AsTask(IAsyncOperation<T>) overload for awaiting WinRT calls.
    $candidates = @([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1
    })
    $asTaskGeneric = $null
    foreach ($m in $candidates) {
        $p = $m.GetParameters()[0].ParameterType
        if ($p.IsGenericType -and $p.GetGenericTypeDefinition().Name -eq "IAsyncOperation``1") {
            $asTaskGeneric = $m
            break
        }
    }
    if ($null -eq $asTaskGeneric) { throw "Windows Runtime async bridge is unavailable." }

    function Await($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $engine = $null
    try {
        $language = New-Object Windows.Globalization.Language $LangTag
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    } catch {
        $engine = $null
    }
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine) { throw "No OCR language pack is installed." }

    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @($result.Lines | ForEach-Object { $_.Text })
    [Console]::Out.WriteLine(($lines -join "`n"))
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
