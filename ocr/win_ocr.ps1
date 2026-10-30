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

    # OcrLine.BoundingRect is not reachable from PowerShell 5.1, but OcrWord.BoundingRect is,
    # so each line box is the union of its words' rectangles. JSON is emitted as one line.
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $records = New-Object System.Collections.Generic.List[string]
    foreach ($line in $result.Lines) {
        $words = @($line.Words)
        if ($words.Count -eq 0) { continue }
        $text = [string]$line.Text
        if ([string]::IsNullOrWhiteSpace($text)) { continue }
        $minX = [double]::MaxValue
        $minY = [double]::MaxValue
        $maxRight = [double]::MinValue
        $maxBottom = [double]::MinValue
        foreach ($word in $words) {
            $rect = $word.BoundingRect
            $left = [double]$rect.X
            $top = [double]$rect.Y
            $right = $left + [double]$rect.Width
            $bottom = $top + [double]$rect.Height
            if ($left -lt $minX) { $minX = $left }
            if ($top -lt $minY) { $minY = $top }
            if ($right -gt $maxRight) { $maxRight = $right }
            if ($bottom -gt $maxBottom) { $maxBottom = $bottom }
        }
        $textJson = ConvertTo-Json -InputObject $text -Compress
        $record = '{"text":' + $textJson +
            ',"x":' + $minX.ToString("0.##", $invariant) +
            ',"y":' + $minY.ToString("0.##", $invariant) +
            ',"w":' + ($maxRight - $minX).ToString("0.##", $invariant) +
            ',"h":' + ($maxBottom - $minY).ToString("0.##", $invariant) + '}'
        $records.Add($record)
    }
    $json = '{"lines":[' + ($records -join ',') + ']}'
    [Console]::Out.WriteLine($json)
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
