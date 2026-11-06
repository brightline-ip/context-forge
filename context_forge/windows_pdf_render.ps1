param(
    [Parameter(Mandatory=$true)][string]$InputPdf,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [Parameter(Mandatory=$true)][string]$PagesCsv
)
$ErrorActionPreference = 'Stop'
# Renders pages at the same scale as windows_pdf_ocr.ps1 so both OCR engines read the same image.
if ([Console]::ReadLine() -ne 'go') { exit 2 }
try {
    if ($PagesCsv -notmatch '^\d+(,\d+){0,7}$') { throw 'Invalid pages' }
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
    $asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $asTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
    } | Select-Object -First 1
    function Await-Result($Operation, [Type]$ResultType) {
        $task = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        if (-not $task.Wait(10000)) { throw 'Operation timeout' }
        return $task.Result
    }
    function Await-Action($Operation) {
        $task = $asTaskAction.Invoke($null, @($Operation))
        if (-not $task.Wait(10000)) { throw 'Operation timeout' }
    }
    $source = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($InputPdf)) ([Windows.Storage.StorageFile])
    $pdf = Await-Result ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($source)) ([Windows.Data.Pdf.PdfDocument])
    foreach ($number in @($PagesCsv.Split(',') | ForEach-Object { [int]$_ })) {
        $page = $null; $stream = $null; $reader = $null; $file = $null
        try {
            if ($number -lt 1 -or $number -gt $pdf.PageCount) { throw 'Invalid page' }
            $page = $pdf.GetPage([uint32]($number - 1))
            $width = [double]$page.Size.Width; $height = [double]$page.Size.Height
            if ($width -le 0 -or $height -le 0 -or $width -gt 14400 -or $height -gt 14400) { throw 'Page dimensions rejected' }
            $scale = [Math]::Min(200.0 / 96.0, 2600 / [Math]::Max($width, $height))
            $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
            $options.DestinationWidth = [uint32][Math]::Max(1, [Math]::Floor($width * $scale))
            $options.DestinationHeight = [uint32][Math]::Max(1, [Math]::Floor($height * $scale))
            $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
            Await-Action ($page.RenderToStreamAsync($stream, $options))
            $stream.Seek(0)
            $reader = [System.IO.WindowsRuntimeStreamExtensions]::AsStream($stream)
            $file = [IO.File]::Create((Join-Path $OutputDir "page-$number.png"))
            $reader.CopyTo($file)
        } catch {
            # A page that cannot be rendered is reported by the caller as not checked.
        } finally {
            if ($null -ne $file) { $file.Dispose() }
            if ($null -ne $reader) { $reader.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
            if ($null -ne $page) { $page.Dispose() }
        }
    }
} catch {
    exit 3
}
