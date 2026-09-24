param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$config = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
$stdoutPath = $Request + '.stdout'
$stderrPath = $Request + '.stderr'
$cancelPath = $Request + '.cancel'

function Read-Tail([string]$Path, [int]$Limit) {
    $stream = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try {
        $length = [Math]::Min($stream.Length, $Limit)
        [void]$stream.Seek(-$length, 'End')
        $buffer = New-Object byte[] $length
        $count = $stream.Read($buffer, 0, $buffer.Length)
        $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $count)
        if ($stream.Length -gt $Limit) { return "[output truncated; full output in $Path]`n" + $text }
        return $text
    } finally { $stream.Dispose() }
}

$process = $null
$timedOut = $false
try {
    foreach ($property in $config.env.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process')
    }
    # Execute as a command string, not a batch file: Harbor's directory helpers
    # use interactive FOR variables (%I), which mean something else in .bat.
    # /S strips the enclosing pair of quotes while retaining command quoting.
    $commandLine = '"chcp 65001 >nul & ' + $config.command + '"'
    if ($commandLine.Length -gt 8000) { throw 'cmd.exe command exceeds supported length; upload a script instead' }
    if (Test-Path -LiteralPath $cancelPath) { throw 'Command cancelled before start' }
    $process = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/s', '/c', $commandLine) -WorkingDirectory $config.cwd -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru -NoNewWindow
    # Cache the process handle before polling, so ExitCode remains available
    # after a short-lived process exits on Windows PowerShell 5.1.
    $null = $process.Handle
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while (-not $process.WaitForExit(200)) {
        if ($clock.Elapsed.TotalSeconds -ge $config.timeout_sec -or (Test-Path -LiteralPath $cancelPath)) {
            $timedOut = $true
            & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F >$null 2>&1
            if (-not $process.WaitForExit(10000)) { throw 'Unable to terminate command process tree' }
            break
        }
    }
    $process.WaitForExit()
    $result = @{
        stdout = Read-Tail $stdoutPath $config.max_output_bytes
        stderr = Read-Tail $stderrPath $config.max_output_bytes
        return_code = $process.ExitCode
        timed_out = $timedOut
    }
    [Console]::Write((ConvertTo-Json -InputObject $result -Compress))
} finally {
    if ($process -and -not $process.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F >$null 2>&1
    }
    # Remove requests containing environment secrets. Keep output for diagnosis
    # until the isolated VM is deleted.
    Remove-Item -LiteralPath $Request, $cancelPath -Force -ErrorAction SilentlyContinue
}
