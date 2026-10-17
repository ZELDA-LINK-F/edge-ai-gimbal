# ============================================================
#  STM32 自动烧录工具（FlyMcu 自动化封装）
#  用法: powershell -File flash.ps1 <hex文件路径>
#  说明: 自动设置端口/文件/选项，启动 FlyMcu，置顶窗口，
#        用真实鼠标点击"开始编程"，等待结果
# ============================================================
param([Parameter(Mandatory=$true)][string]$HexFile)

$ErrorActionPreference = "Continue"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class FM {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int m);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr h, StringBuilder s, int m);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint d, UIntPtr e);
    public static List<IntPtr> C = new List<IntPtr>();
}
"@

$fmDir = "$env:USERPROFILE\Downloads\FlyMcu_串口下载软件2"
$exe   = Join-Path $fmDir "FlyMcu.exe"
$ini   = Join-Path $fmDir "FlyMcuConfig.ini"
$log   = Join-Path $fmDir "ProgramLogs.txt"

if (-not (Test-Path $exe)) { Write-Output "ERROR: 找不到 FlyMcu.exe"; exit 1 }
if (-not (Test-Path $HexFile)) { Write-Output ("ERROR: 找不到 " + $HexFile); exit 1 }

# ---- 1. 修改 INI ----
Get-Process FlyMcu -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 600
$lines = Get-Content $ini -Encoding Default
$out = New-Object System.Collections.ArrayList
foreach ($l in $lines) {
    $t = $l.Trim()
    if     ($t -match '^Port=\d+$')                { [void]$out.Add('Port=2') }
    elseif ($t -match '^PortBps=\d+$')             { [void]$out.Add('PortBps=4') }
    elseif ($t -match '^FileName=')                 { [void]$out.Add('FileName=' + (Resolve-Path $HexFile).Path) }
    elseif ($t -match '^cbSTMWriteOption=\d+$')    { [void]$out.Add('cbSTMWriteOption=0') }
    elseif ($t -match '^cbSTMExecAfterFlash=\d+$') { [void]$out.Add('cbSTMExecAfterFlash=1') }
    elseif ($t -match '^cbVerify=\d+$')            { [void]$out.Add('cbVerify=1') }
    else { [void]$out.Add($l) }
}
Set-Content -Path $ini -Value $out -Encoding Default

# ---- 2. 启动并置顶 ----
$before = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }
$proc = Start-Process -FilePath $exe -WorkingDirectory $fmDir -PassThru
Start-Sleep -Seconds 3
$main = $proc.MainWindowHandle
[void][FM]::ShowWindow($main, 9)
Start-Sleep -Milliseconds 400
[void][FM]::SetWindowPos($main, [IntPtr](-1), 30, 30, 720, 560, 0x0040)
[void][FM]::SetForegroundWindow($main)
Start-Sleep -Seconds 1

# ---- 3. 找按钮并点击 ----
[FM]::C.Clear()
[void][FM]::EnumChildWindows($main, [FM+EnumProc]{ param($h,$l) [FM]::C.Add($h); return $true }, [IntPtr]::Zero)
$btn = [IntPtr]::Zero
foreach ($h in [FM]::C) {
    $tt = New-Object System.Text.StringBuilder 256
    $cc = New-Object System.Text.StringBuilder 128
    [void][FM]::GetWindowText($h, $tt, 256)
    [void][FM]::GetClassName($h, $cc, 128)
    if ($cc.ToString() -eq "TButton" -and $tt.ToString().Contains("(P)") -and [FM]::IsWindowVisible($h)) { $btn = $h }
}
if ($btn -eq [IntPtr]::Zero) { Write-Output "ERROR: 找不到开始编程按钮"; $proc.Kill(); exit 1 }

$r = New-Object FM+RECT
[void][FM]::GetWindowRect($btn, [ref]$r)
[void][FM]::SetCursorPos([int](($r.Left + $r.Right)/2), [int](($r.Top + $r.Bottom)/2))
Start-Sleep -Milliseconds 500
[void][FM]::SetForegroundWindow($main)
Start-Sleep -Milliseconds 300
[FM]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 200
[FM]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)

# ---- 4. 等待结果 ----
$ok = $false
for ($i = 1; $i -le 60; $i++) {
    Start-Sleep -Seconds 1
    $now = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }
    if ($now -gt $before) { $ok = $true; break }
}

try { $proc.Kill() } catch { }

if ($ok) {
    $last = Get-Content $log -Encoding Default -Tail 1
    if ($last -match "Program Success") { Write-Output "OK"; Write-Output $last }
    else { Write-Output "FAIL"; Write-Output $last }
    exit 0
} else {
    Write-Output "TIMEOUT: 60 秒内没有烧录完成"
    exit 1
}
