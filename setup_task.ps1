# =========================================================
# 一键注册 / 卸载 Windows 计划任务
# 用法（在 PowerShell 里，脚本所在目录）：
#   .\setup_task.ps1 -Install          # 注册任务（每 30 分钟）
#   .\setup_task.ps1 -Install -Minutes 15
#   .\setup_task.ps1 -Uninstall
#   .\setup_task.ps1 -RunNow           # 立即跑一次
#
# 注意：如果用 -File / 中文路径报错，请改用 -Command。
# 默认使用本机 python（PATH 中的 python）。可用 -Python 指定路径。
# =========================================================

param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$RunNow,
    [int]$Minutes = 30,
    [string]$Python = "python",
    [string]$TaskName = "RateMonitor"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Here "monitor.py"
$LogFile = Join-Path $Here "monitor.log"

# 组合命令：优先 -Command 以兼容中文路径
$Cmd = "$Python -u `"$Script`" --once"

if ($Uninstall) {
    Write-Host "卸载计划任务 $TaskName ..."
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "已卸载" -ForegroundColor Green
    } catch {
        Write-Host "任务不存在或已卸载：$_" -ForegroundColor Yellow
    }
    return
}

if ($Install -or -not ($Uninstall -or $RunNow)) {
    # 默认动作：注册
    if (-not (Test-Path $Script)) {
        Write-Host "找不到 monitor.py：$Script" -ForegroundColor Red
        exit 1
    }

    $Action = New-ScheduledTaskAction -Execute $Python `
        -Argument "-u `"$Script`" --once" `
        -WorkingDirectory $Here

    # 每 $Minutes 分钟触发，无限重复
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $Minutes)

    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Write-Host "注册计划任务：$TaskName （每 $Minutes 分钟，python=$Python）"
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $TaskName `
            -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
        Write-Host "已注册。下次触发时间约 1 分钟后，之后每 $Minutes 分钟一次。" -ForegroundColor Green
        Write-Host "如需查看日志：日志由 monitor.py 内置 file 配置控制；可在 config.yaml 的 log.file 设置。"
    } catch {
        Write-Host "注册失败：$_" -ForegroundColor Red
        exit 1
    }
    return
}

if ($RunNow) {
    Write-Host "立即运行一次：$Cmd"
    & $Python -u $Script --once
    return
}
