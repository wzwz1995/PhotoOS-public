# photo-tool-gui.ps1 - click-and-run window for the photo/video pipeline
#
# Zero dependencies: plain PowerShell + WinForms (ships with Windows).
# Type or drop a folder, press 开始处理, watch the live log.
#
# All library paths come from photo-tool.config.json next to this script (editable
# in the 设置 dialog) - nothing is baked in. Command-line parameters still win,
# which is what the self test uses.
#
# NOTE: keep this file UTF-8 with BOM (PowerShell 5.1 reads BOM-less files as GBK).
#       Run check-tools.ps1 after editing.

[CmdletBinding()]
param(
    [string]$PhotoRoot = '',
    [string]$VideoRoot = '',
    [string]$WorkRoot = '',
    [string]$CameraStagingRoot = '',
    [string]$ConfigPath = '',
    [string]$StatePath = '',
    [switch]$SelfTest,          # automated check: plant -TestSource, press start, wait, exit
    [string]$TestSource = '',
    [switch]$TestExecute,
    [int]$CameraMaxFiles = 0    # test knob: limit how many files come off a camera
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$tools = $PSScriptRoot
$runner = Join-Path $tools 'run-pipeline.ps1'
if (-not (Test-Path -LiteralPath $runner)) { throw "缺少 run-pipeline.ps1：$runner" }
if (-not $ConfigPath) { $ConfigPath = Join-Path $tools 'photo-tool.config.json' }
if (-not $StatePath) { $StatePath = Join-Path $tools 'gui-state.txt' }

# ---------------------------------------------------------------- settings
function Get-DefaultConfig {
    return [ordered]@{
        PhotoRoot         = 'C:\Photos'
        VideoRoot         = 'E:\视频'
        WorkRoot          = 'C:\Photos\_归类清单'
        CameraStagingRoot = 'E:\_相机导入'
    }
}
function Load-Config {
    $cfg = Get-DefaultConfig
    if (Test-Path -LiteralPath $ConfigPath) {
        try {
            $j = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($k in @($cfg.Keys)) { if ($j.$k) { $cfg[$k] = [string]$j.$k } }
        }
        catch { }
    }
    return $cfg
}
function Save-Config($cfg) {
    try {
        ($cfg | ConvertTo-Json) | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
        return $true
    }
    catch { return $false }
}

$cfg = Load-Config
if (-not $PhotoRoot) { $PhotoRoot = $cfg.PhotoRoot }
if (-not $VideoRoot) { $VideoRoot = $cfg.VideoRoot }
if (-not $WorkRoot) { $WorkRoot = $cfg.WorkRoot }
if (-not $CameraStagingRoot) { $CameraStagingRoot = $cfg.CameraStagingRoot }

$script:quarantine = Join-Path $WorkRoot '_重复文件（确认后可删除）'
function Update-Quarantine { $script:quarantine = Join-Path $WorkRoot '_重复文件（确认后可删除）' }

# ------------------------------------------------------------------ state
$script:logPath = $null
$script:logPos = 0
$script:proc = $null
$script:running = $false
$script:lastExit = $null

function Load-State {
    if (Test-Path -LiteralPath $StatePath) {
        $lines = @(Get-Content -LiteralPath $StatePath -Encoding UTF8)
        if ($lines.Count -ge 1) { return $lines[0] }
    }
    return ''
}
function Save-State($src) {
    try { Set-Content -LiteralPath $StatePath -Value $src -Encoding UTF8 } catch { }
}
function Quote-Arg([string]$a) {
    # Start-Process -ArgumentList does NOT quote array elements, so a path with a
    # space ("...\可移动存储 10001\...") would arrive as several arguments.
    if ($null -eq $a -or $a -eq '') { return '""' }
    if ($a -notmatch '[\s"]') { return $a }
    $s = $a -replace '(\\*)"', '$1$1\"'
    $s = $s -replace '(\\+)$', '$1$1'
    return '"' + $s + '"'
}

# ------------------------------------------------------------------- form
$font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$form = New-Object System.Windows.Forms.Form
$form.Text = '照片自动归类工具'
$form.Size = New-Object System.Drawing.Size(940, 720)
$form.StartPosition = 'CenterScreen'
$form.Font = $font
$form.MinimumSize = New-Object System.Drawing.Size(780, 600)
$form.AllowDrop = $true

$lblSrc = New-Object System.Windows.Forms.Label
$lblSrc.Text = '素材路径：'
$lblSrc.Location = New-Object System.Drawing.Point(14, 18)
$lblSrc.Size = New-Object System.Drawing.Size(72, 22)
$form.Controls.Add($lblSrc)

$txtSrc = New-Object System.Windows.Forms.TextBox
$txtSrc.Location = New-Object System.Drawing.Point(88, 15)
$txtSrc.Size = New-Object System.Drawing.Size(620, 25)
$txtSrc.Anchor = 'Top,Left,Right'
$txtSrc.Text = (Load-State)
$form.Controls.Add($txtSrc)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Text = '浏览...'
$btnBrowse.Location = New-Object System.Drawing.Point(716, 14)
$btnBrowse.Size = New-Object System.Drawing.Size(80, 26)
$btnBrowse.Anchor = 'Top,Right'
$form.Controls.Add($btnBrowse)

$btnOpenSrc = New-Object System.Windows.Forms.Button
$btnOpenSrc.Text = '打开'
$btnOpenSrc.Location = New-Object System.Drawing.Point(802, 14)
$btnOpenSrc.Size = New-Object System.Drawing.Size(60, 26)
$btnOpenSrc.Anchor = 'Top,Right'
$form.Controls.Add($btnOpenSrc)

$chkExecute = New-Object System.Windows.Forms.CheckBox
$chkExecute.Text = '实际执行（把文件归位并隔离重复）—— 不勾选＝试运行，只看计划、不动任何文件'
$chkExecute.Location = New-Object System.Drawing.Point(16, 48)
$chkExecute.Size = New-Object System.Drawing.Size(880, 24)
$chkExecute.Anchor = 'Top,Left,Right'
$form.Controls.Add($chkExecute)

$grpOpt = New-Object System.Windows.Forms.GroupBox
$grpOpt.Text = '选项'
$grpOpt.Location = New-Object System.Drawing.Point(14, 78)
$grpOpt.Size = New-Object System.Drawing.Size(896, 100)
$grpOpt.Anchor = 'Top,Left,Right'
$form.Controls.Add($grpOpt)

$lblGap = New-Object System.Windows.Forms.Label
$lblGap.Text = '时段阈值（小时）：'
$lblGap.Location = New-Object System.Drawing.Point(12, 26)
$lblGap.Size = New-Object System.Drawing.Size(120, 22)
$grpOpt.Controls.Add($lblGap)

$numGap = New-Object System.Windows.Forms.NumericUpDown
$numGap.Location = New-Object System.Drawing.Point(136, 23)
$numGap.Size = New-Object System.Drawing.Size(60, 25)
$numGap.Minimum = 1
$numGap.Maximum = 168
$numGap.Value = 12
$grpOpt.Controls.Add($numGap)

$lblMax = New-Object System.Windows.Forms.Label
$lblMax.Text = '只处理前 N 个（0＝全部）：'
$lblMax.Location = New-Object System.Drawing.Point(240, 26)
$lblMax.Size = New-Object System.Drawing.Size(170, 22)
$grpOpt.Controls.Add($lblMax)

$numMax = New-Object System.Windows.Forms.NumericUpDown
$numMax.Location = New-Object System.Drawing.Point(414, 23)
$numMax.Size = New-Object System.Drawing.Size(80, 25)
$numMax.Minimum = 0
$numMax.Maximum = 1000000
$numMax.Value = 0
$grpOpt.Controls.Add($numMax)

$chkSkipDup = New-Object System.Windows.Forms.CheckBox
$chkSkipDup.Text = '跳过查重'
$chkSkipDup.Location = New-Object System.Drawing.Point(540, 24)
$chkSkipDup.Size = New-Object System.Drawing.Size(110, 24)
$grpOpt.Controls.Add($chkSkipDup)

$chkCross = New-Object System.Windows.Forms.CheckBox
$chkCross.Text = '跨文件夹也算重复'
$chkCross.Location = New-Object System.Drawing.Point(660, 24)
$chkCross.Size = New-Object System.Drawing.Size(180, 24)
$grpOpt.Controls.Add($chkCross)

$lblHint = New-Object System.Windows.Forms.Label
$lblHint.Text = '时段阈值：相邻两张间隔超过它就切成新时段（连续跨天的不拆）。「只处理前 N 个」用来小批量试跑，确认效果后再全量。'
$lblHint.Location = New-Object System.Drawing.Point(12, 60)
$lblHint.Size = New-Object System.Drawing.Size(860, 22)
$lblHint.Anchor = 'Top,Left,Right'
$lblHint.ForeColor = [System.Drawing.Color]::DimGray
$grpOpt.Controls.Add($lblHint)

$btnStart = New-Object System.Windows.Forms.Button
$btnStart.Text = '▶  开始处理'
$btnStart.Location = New-Object System.Drawing.Point(14, 190)
$btnStart.Size = New-Object System.Drawing.Size(130, 34)
$btnStart.BackColor = [System.Drawing.Color]::FromArgb(0, 120, 215)
$btnStart.ForeColor = [System.Drawing.Color]::White
$btnStart.FlatStyle = 'Flat'
$form.Controls.Add($btnStart)

$btnStop = New-Object System.Windows.Forms.Button
$btnStop.Text = '■  停止'
$btnStop.Location = New-Object System.Drawing.Point(152, 190)
$btnStop.Size = New-Object System.Drawing.Size(90, 34)
$btnStop.Enabled = $false
$form.Controls.Add($btnStop)

$btnLib = New-Object System.Windows.Forms.Button
$btnLib.Text = '打开照片库'
$btnLib.Location = New-Object System.Drawing.Point(254, 190)
$btnLib.Size = New-Object System.Drawing.Size(100, 34)
$form.Controls.Add($btnLib)

$btnQuar = New-Object System.Windows.Forms.Button
$btnQuar.Text = '打开隔离区'
$btnQuar.Location = New-Object System.Drawing.Point(360, 190)
$btnQuar.Size = New-Object System.Drawing.Size(100, 34)
$form.Controls.Add($btnQuar)

$btnRuns = New-Object System.Windows.Forms.Button
$btnRuns.Text = '打开日志目录'
$btnRuns.Location = New-Object System.Drawing.Point(466, 190)
$btnRuns.Size = New-Object System.Drawing.Size(110, 34)
$form.Controls.Add($btnRuns)

$btnCam = New-Object System.Windows.Forms.Button
$btnCam.Text = '找相机'
$btnCam.Location = New-Object System.Drawing.Point(582, 190)
$btnCam.Size = New-Object System.Drawing.Size(90, 34)
$form.Controls.Add($btnCam)

$btnSet = New-Object System.Windows.Forms.Button
$btnSet.Text = '设置'
$btnSet.Location = New-Object System.Drawing.Point(680, 190)
$btnSet.Size = New-Object System.Drawing.Size(80, 34)
$form.Controls.Add($btnSet)

$btnClean = New-Object System.Windows.Forms.Button
$btnClean.Text = '清理试跑文件'
$btnClean.Location = New-Object System.Drawing.Point(766, 190)
$btnClean.Size = New-Object System.Drawing.Size(120, 34)
$form.Controls.Add($btnClean)

$tip = New-Object System.Windows.Forms.ToolTip
$tip.SetToolTip($btnCam, '相机/手机用 USB 连上后点这里：自动扫描便携设备，把 DCIM 路径填进输入框（只读复制，卡上文件不动）')
$tip.SetToolTip($btnSet, '修改照片库 / 视频库 / 日志缓存 / 相机暂存目录的位置，保存在 photo-tool.config.json')
$tip.SetToolTip($btnQuar, '重复文件被移到这里（不是删除）。确认无误后清空该目录即可释放空间')
$tip.SetToolTip($chkCross, '默认只隔离"同一文件夹内"的重复副本；勾上后跨文件夹的重复也会被隔离')
$tip.SetToolTip($btnClean, '试运行（dry run）会把相机文件复制到暂存目录但不动它们，久了会占空间。这里只删"照片库里已经能找到同内容副本"的那些暂存目录，核对不上的整份保留')

$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Text = '就绪。全程不删除任何文件；重复只隔离到隔离区。相机/手机连上后点「找相机」。'
$lblStatus.Location = New-Object System.Drawing.Point(16, 232)
$lblStatus.Size = New-Object System.Drawing.Size(896, 22)
$lblStatus.Anchor = 'Top,Left,Right'
$lblStatus.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($lblStatus)

$lblPaths = New-Object System.Windows.Forms.Label
$lblPaths.Location = New-Object System.Drawing.Point(16, 254)
$lblPaths.Size = New-Object System.Drawing.Size(896, 20)
$lblPaths.Anchor = 'Top,Left,Right'
$lblPaths.ForeColor = [System.Drawing.Color]::DimGray
$lblPaths.AutoEllipsis = $true     # very long paths get "..." instead of being cut mid-character
$form.Controls.Add($lblPaths)

function Update-PathLabel {
    $lblPaths.Text = "照片库 $PhotoRoot   |   视频库 $VideoRoot   |   日志/缓存 $WorkRoot   |   相机暂存 $CameraStagingRoot"
    $tip.SetToolTip($lblPaths, $lblPaths.Text)
}
Update-PathLabel

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Multiline = $true
$txtLog.ScrollBars = 'Vertical'
$txtLog.ReadOnly = $true
$txtLog.WordWrap = $false
$txtLog.Location = New-Object System.Drawing.Point(14, 278)
$txtLog.Size = New-Object System.Drawing.Size(896, 380)
$txtLog.Anchor = 'Top,Bottom,Left,Right'
$txtLog.BackColor = [System.Drawing.Color]::FromArgb(30, 30, 30)
$txtLog.ForeColor = [System.Drawing.Color]::Gainsboro
$txtLog.Font = New-Object System.Drawing.Font('Consolas', 9)
$form.Controls.Add($txtLog)

# ------------------------------------------------------------------ timer
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 600

$script:AppendLog = {
    param($text)
    $txtLog.AppendText($text)
    $txtLog.SelectionStart = $txtLog.TextLength
    $txtLog.ScrollToCaret()
}

$timer.Add_Tick({
    if ($script:logPath -and (Test-Path -LiteralPath $script:logPath)) {
        try {
            $fs = [System.IO.File]::Open($script:logPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            try {
                if ($fs.Length -gt $script:logPos) {
                    $fs.Position = $script:logPos
                    $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
                    $new = $sr.ReadToEnd()
                    $script:logPos = $fs.Length
                    if ($new) { & $script:AppendLog $new }
                }
            }
            finally { $fs.Dispose() }
        }
        catch { }
    }
    if ($script:proc -and $script:proc.HasExited -and $script:running) {
        $script:running = $false
        $timer.Stop()
        $exe = $script:proc.ExitCode
        $script:lastExit = $exe
        & $script:AppendLog ("`r`n[进程结束，退出码 {0}]`r`n" -f $exe)
        if ($exe -eq 0) { $lblStatus.Text = '完成。' } else { $lblStatus.Text = "结束但有错误（退出码 $exe），请查看日志。" }
        $lblStatus.ForeColor = if ($exe -eq 0) { [System.Drawing.Color]::Green } else { [System.Drawing.Color]::Firebrick }
        $btnStart.Enabled = $true; $btnStop.Enabled = $false; $chkExecute.Enabled = $true; $txtSrc.Enabled = $true
        if (-not $SelfTest) {
            [System.Windows.Forms.MessageBox]::Show(
                $(if ($exe -eq 0) { '处理完成。' } else { "处理结束但有错误（退出码 $exe）。" }),
                '照片归类工具', 'OK',
                $(if ($exe -eq 0) { 'Information' } else { 'Warning' })) | Out-Null
            Start-Process explorer.exe -ArgumentList "`"$WorkRoot`""
        }
    }
})

# ---------------------------------------------------------------- actions
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = '选择要归类的素材目录（相机卡 / 手机导出 / 下载目录）'
    if ($txtSrc.Text -and (Test-Path -LiteralPath $txtSrc.Text)) { $dlg.SelectedPath = $txtSrc.Text }
    if ($dlg.ShowDialog() -eq 'OK') { $txtSrc.Text = $dlg.SelectedPath }
})

$btnOpenSrc.Add_Click({
    if ($txtSrc.Text -and (Test-Path -LiteralPath $txtSrc.Text)) { Start-Process explorer.exe -ArgumentList "`"$($txtSrc.Text)`"" }
    else { [System.Windows.Forms.MessageBox]::Show('这个路径打不开：相机等便携设备路径不是文件夹，请改用「找相机」。', '照片归类工具') | Out-Null }
})

$btnLib.Add_Click({
    if (Test-Path -LiteralPath $PhotoRoot) { Start-Process explorer.exe -ArgumentList "`"$PhotoRoot`"" }
    else { [System.Windows.Forms.MessageBox]::Show("照片库目录还不存在：`r`n$PhotoRoot`r`n`r`n第一次运行会自动创建。", '照片归类工具') | Out-Null }
})
$btnQuar.Add_Click({
    Update-Quarantine
    if (-not (Test-Path -LiteralPath $script:quarantine)) { New-Item -ItemType Directory -Force -Path $script:quarantine | Out-Null }
    Start-Process explorer.exe -ArgumentList "`"$script:quarantine`""
})
$btnRuns.Add_Click({
    if (-not (Test-Path -LiteralPath $WorkRoot)) { New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null }
    Start-Process explorer.exe -ArgumentList "`"$WorkRoot`""
})

function Get-CameraFolders {
    # portable devices (MTP) show up under This PC with a ::{20D04FE0-...} path;
    # they have no drive letter, so a normal file path can never reach them
    $out = New-Object System.Collections.ArrayList
    try {
        $sh = New-Object -ComObject Shell.Application
        foreach ($dev in @($sh.NameSpace(17).Items())) {
            if ($dev.Path -notlike '::{20D04FE0-3AEA-1069-A2D8-08002B30309D}*') { continue }
            $df = $null; try { $df = $dev.GetFolder } catch { }
            if ($null -eq $df) { continue }
            foreach ($vol in @($df.Items())) {
                if (-not $vol.IsFolder) { continue }
                $vf = $null; try { $vf = $vol.GetFolder } catch { }
                if ($null -eq $vf) { continue }
                $dcim = @($vf.Items()) | Where-Object { $_.Name -eq 'DCIM' } | Select-Object -First 1
                if (-not $dcim) { continue }
                $dcf = $null; try { $dcf = $dcim.GetFolder } catch { }
                if ($null -eq $dcf) { continue }
                $subs = @(@($dcf.Items()) | Where-Object { $_.IsFolder })
                if ($subs.Count -eq 0) { [void]$out.Add("此电脑\$($dev.Name)\$($vol.Name)\DCIM") }
                else { foreach ($sub in $subs) { [void]$out.Add("此电脑\$($dev.Name)\$($vol.Name)\DCIM\$($sub.Name)") } }
            }
        }
    }
    catch { }
    return $out
}

$btnCam.Add_Click({
    $lblStatus.Text = '正在扫描便携设备 ...'
    $lblStatus.ForeColor = [System.Drawing.Color]::DarkOrange
    [System.Windows.Forms.Application]::DoEvents()
    $folders = @(Get-CameraFolders)
    if ($folders.Count -eq 0) {
        $lblStatus.Text = '没有找到相机/便携设备。'
        $lblStatus.ForeColor = [System.Drawing.Color]::Firebrick
        [System.Windows.Forms.MessageBox]::Show(
            "没有找到带 DCIM 的相机或手机。`r`n`r`n可以检查：`r`n  1) 相机是否开机、USB 线是否连好`r`n  2) 相机的 USB 模式是否选了 MTP/PTP`r`n  3) 或者把存储卡拿出来用读卡器——那样会直接出现盘符，用起来更快",
            '找相机', 'OK', 'Information') | Out-Null
        return
    }
    if ($folders.Count -eq 1) {
        $txtSrc.Text = $folders[0]
        $lblStatus.Text = "已填入：$($folders[0])"
        $lblStatus.ForeColor = [System.Drawing.Color]::Green
        return
    }
    $pf = New-Object System.Windows.Forms.Form
    $pf.Text = '选择相机里的目录'
    $pf.Size = New-Object System.Drawing.Size(620, 300)
    $pf.StartPosition = 'CenterParent'
    $pf.Font = $font
    $lb = New-Object System.Windows.Forms.ListBox
    $lb.Location = New-Object System.Drawing.Point(12, 12)
    $lb.Size = New-Object System.Drawing.Size(580, 200)
    $lb.Anchor = 'Top,Bottom,Left,Right'
    foreach ($x in $folders) { [void]$lb.Items.Add($x) }
    $lb.SelectedIndex = 0
    $okb = New-Object System.Windows.Forms.Button
    $okb.Text = '确定'; $okb.DialogResult = 'OK'
    $okb.Location = New-Object System.Drawing.Point(420, 222); $okb.Size = New-Object System.Drawing.Size(80, 28)
    $cb = New-Object System.Windows.Forms.Button
    $cb.Text = '取消'; $cb.DialogResult = 'Cancel'
    $cb.Location = New-Object System.Drawing.Point(510, 222); $cb.Size = New-Object System.Drawing.Size(80, 28)
    $pf.Controls.AddRange(@($lb, $okb, $cb))
    $pf.AcceptButton = $okb; $pf.CancelButton = $cb
    if ($pf.ShowDialog() -eq 'OK' -and $lb.SelectedItem) {
        $txtSrc.Text = [string]$lb.SelectedItem
        $lblStatus.Text = "已填入：$($lb.SelectedItem)"
        $lblStatus.ForeColor = [System.Drawing.Color]::Green
    }
    $pf.Dispose()
})

$btnSet.Add_Click({
    $sf = New-Object System.Windows.Forms.Form
    $sf.Text = '设置路径（保存在 photo-tool.config.json）'
    $sf.Size = New-Object System.Drawing.Size(720, 280)
    $sf.StartPosition = 'CenterParent'
    $sf.Font = $font
    $sf.FormBorderStyle = 'FixedDialog'
    $sf.MaximizeBox = $false
    $sf.MinimizeBox = $false

    $rows = @(
        @{ Key = 'PhotoRoot';         Label = '照片库根目录：' },
        @{ Key = 'VideoRoot';         Label = '视频库根目录：' },
        @{ Key = 'WorkRoot';          Label = '日志/缓存目录：' },
        @{ Key = 'CameraStagingRoot'; Label = '相机暂存目录：' }
    )
    $boxes = @{}
    $cur = @{ PhotoRoot = $PhotoRoot; VideoRoot = $VideoRoot; WorkRoot = $WorkRoot; CameraStagingRoot = $CameraStagingRoot }
    $y = 18
    foreach ($r in $rows) {
        $l = New-Object System.Windows.Forms.Label
        $l.Text = $r.Label
        $l.Location = New-Object System.Drawing.Point(14, ($y + 4)); $l.Size = New-Object System.Drawing.Size(120, 22)
        $sf.Controls.Add($l)
        $t = New-Object System.Windows.Forms.TextBox
        $t.Location = New-Object System.Drawing.Point(138, $y); $t.Size = New-Object System.Drawing.Size(430, 25)
        $t.Text = [string]$cur[$r.Key]
        $sf.Controls.Add($t)
        $b = New-Object System.Windows.Forms.Button
        $b.Text = '浏览...'
        $b.Location = New-Object System.Drawing.Point(576, ($y - 1)); $b.Size = New-Object System.Drawing.Size(70, 27)
        $b.Tag = $t
        $b.Add_Click({
            $d = New-Object System.Windows.Forms.FolderBrowserDialog
            $d.Description = '选择目录'
            if ($this.Tag.Text -and (Test-Path -LiteralPath $this.Tag.Text)) { $d.SelectedPath = $this.Tag.Text }
            if ($d.ShowDialog() -eq 'OK') { $this.Tag.Text = $d.SelectedPath }
        })
        $sf.Controls.Add($b)
        $boxes[$r.Key] = $t
        $y += 38
    }
    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = '目录不存在会自动创建；改「日志/缓存目录」会同时移动重复文件隔离区（在其下的 _重复文件（确认后可删除））。'
    $hint.Location = New-Object System.Drawing.Point(14, ($y + 6)); $hint.Size = New-Object System.Drawing.Size(680, 22)
    $hint.ForeColor = [System.Drawing.Color]::DimGray
    $sf.Controls.Add($hint)
    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = '保存'; $ok.DialogResult = 'OK'
    $ok.Location = New-Object System.Drawing.Point(500, ($y + 36)); $ok.Size = New-Object System.Drawing.Size(70, 28)
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = '取消'; $cancel.DialogResult = 'Cancel'
    $cancel.Location = New-Object System.Drawing.Point(580, ($y + 36)); $cancel.Size = New-Object System.Drawing.Size(70, 28)
    $sf.Controls.AddRange(@($ok, $cancel))
    $sf.AcceptButton = $ok; $sf.CancelButton = $cancel

    if ($sf.ShowDialog() -eq 'OK') {
        $newCfg = [ordered]@{
            PhotoRoot         = $boxes['PhotoRoot'].Text.Trim().Trim('"')
            VideoRoot         = $boxes['VideoRoot'].Text.Trim().Trim('"')
            WorkRoot          = $boxes['WorkRoot'].Text.Trim().Trim('"')
            CameraStagingRoot = $boxes['CameraStagingRoot'].Text.Trim().Trim('"')
        }
        $empty = @($newCfg.Keys | Where-Object { -not $newCfg[$_] })
        if ($empty.Count -gt 0) {
            [System.Windows.Forms.MessageBox]::Show('路径不能为空。', '设置', 'OK', 'Warning') | Out-Null
            $sf.Dispose(); return
        }
        $PhotoRoot = $newCfg.PhotoRoot
        $VideoRoot = $newCfg.VideoRoot
        $WorkRoot = $newCfg.WorkRoot
        $CameraStagingRoot = $newCfg.CameraStagingRoot
        Update-Quarantine
        foreach ($k in @($newCfg.Keys)) {
            if (-not (Test-Path -LiteralPath $newCfg[$k])) {
                try { New-Item -ItemType Directory -Force -Path $newCfg[$k] | Out-Null } catch { }
            }
        }
        if (Save-Config $newCfg) {
            Update-PathLabel
            $lblStatus.Text = '设置已保存。'
            $lblStatus.ForeColor = [System.Drawing.Color]::Green
            & $script:AppendLog ("设置已更新：照片库=$PhotoRoot 视频库=$VideoRoot 日志=$WorkRoot 相机暂存=$CameraStagingRoot`r`n")
        }
        else {
            [System.Windows.Forms.MessageBox]::Show("保存失败，配置文件可能不可写：`r`n$ConfigPath", '设置', 'OK', 'Warning') | Out-Null
        }
    }
    $sf.Dispose()
})

$btnClean.Add_Click({
    if ($script:running) {
        [System.Windows.Forms.MessageBox]::Show('有任务正在运行，等它结束再清理。', '清理试跑文件', 'OK', 'Warning') | Out-Null
        return
    }
    $cleanup = Join-Path $tools 'cleanup-staging.ps1'
    if (-not (Test-Path -LiteralPath $cleanup)) {
        [System.Windows.Forms.MessageBox]::Show("缺少 cleanup-staging.ps1：`r`n$cleanup", '清理试跑文件', 'OK', 'Warning') | Out-Null
        return
    }
    $lblStatus.Text = '正在核对暂存目录 ...'
    $lblStatus.ForeColor = [System.Drawing.Color]::DarkOrange
    [System.Windows.Forms.Application]::DoEvents()

    $scan = ''
    try { $scan = (& $cleanup -ConfigPath $ConfigPath 2>&1 | Out-String) }
    catch { $scan = '执行失败：' + $_.Exception.Message }

    $df = New-Object System.Windows.Forms.Form
    $df.Text = '清理试跑文件 —— 核对结果'
    $df.Size = New-Object System.Drawing.Size(760, 520)
    $df.StartPosition = 'CenterParent'
    $df.Font = $font
    $box = New-Object System.Windows.Forms.TextBox
    $box.Multiline = $true
    $box.ReadOnly = $true
    $box.ScrollBars = 'Both'
    $box.WordWrap = $false
    $box.Font = New-Object System.Drawing.Font('Consolas', 9)
    $box.Location = New-Object System.Drawing.Point(12, 12)
    $box.Size = New-Object System.Drawing.Size(720, 400)
    $box.Anchor = 'Top,Bottom,Left,Right'
    $box.Text = $scan + "`r`n规则：只删除「每个文件都能在照片库里按 文件名+字节数 对上」的暂存目录；`r`n      对不上、或库里根本没有的那一份，整份保留。`r`n      需要逐字节核对可改用命令行加 -DeepVerify（较慢）。"
    $df.Controls.Add($box)
    $okDel = New-Object System.Windows.Forms.Button
    $okDel.Text = '删除可清理的'; $okDel.DialogResult = 'OK'
    $okDel.Location = New-Object System.Drawing.Point(440, 424); $okDel.Size = New-Object System.Drawing.Size(120, 30)
    $okDel.Anchor = 'Bottom,Right'
    $cbDel = New-Object System.Windows.Forms.Button
    $cbDel.Text = '取消'; $cbDel.DialogResult = 'Cancel'
    $cbDel.Location = New-Object System.Drawing.Point(570, 424); $cbDel.Size = New-Object System.Drawing.Size(80, 30)
    $cbDel.Anchor = 'Bottom,Right'
    $df.Controls.AddRange(@($okDel, $cbDel))
    $df.AcceptButton = $okDel; $df.CancelButton = $cbDel
    $choice = $df.ShowDialog()
    $df.Dispose()

    if ($choice -ne 'OK') {
        $lblStatus.Text = '已取消，没有删除任何东西。'
        $lblStatus.ForeColor = [System.Drawing.Color]::DimGray
        & $script:AppendLog "`r`n[清理试跑文件：已取消]`r`n`r`n$scan`r`n"
        return
    }

    $lblStatus.Text = '正在删除 ...'
    [System.Windows.Forms.Application]::DoEvents()
    $res = ''
    try { $res = (& $cleanup -ConfigPath $ConfigPath -Execute 2>&1 | Out-String) }
    catch { $res = '执行失败：' + $_.Exception.Message }
    & $script:AppendLog "`r`n===== 清理试跑文件 =====`r`n$scan`r`n$res`r`n"
    $lblStatus.Text = '清理完成，详情见下方日志。'
    $lblStatus.ForeColor = [System.Drawing.Color]::Green
    [System.Windows.Forms.MessageBox]::Show(($res -split "`r?`n" | Where-Object { $_ -match '已删除目录|没有可删除|失败' } | Select-Object -First 3) -join "`r`n", '清理试跑文件', 'OK', 'Information') | Out-Null
})

$btnStop.Add_Click({    if ($script:proc -and -not $script:proc.HasExited) {
        $r = [System.Windows.Forms.MessageBox]::Show(
            "停止会中断正在进行的处理。`r`n`r`n已完成的移动都已写入日志，可以按日志一键还原；不会丢文件。`r`n`r`n确定停止吗？",
            '确认停止', 'YesNo', 'Question')
        if ($r -eq 'Yes') {
            try { $script:proc.Kill() } catch { }
            & $script:AppendLog "`r`n[已请求停止]`r`n"
        }
    }
})

$btnStart.Add_Click({
    $src = $txtSrc.Text.Trim().Trim('"')
    if (-not $src) { [System.Windows.Forms.MessageBox]::Show('请先填写素材路径。', '照片归类工具', 'OK', 'Warning') | Out-Null; return }
    $isDevice = $false
    if (-not (Test-Path -LiteralPath $src)) {
        # not a filesystem path: could be a camera/phone (MTP) shell path.
        # The pipeline resolves those by copying to a staging folder first.
        if ($src -match '\\') { $isDevice = $true }
        else { [System.Windows.Forms.MessageBox]::Show("路径不存在：`r`n$src", '照片归类工具', 'OK', 'Warning') | Out-Null; return }
    }
    if ($isDevice) { $full = $src }
    else {
        $full = (Resolve-Path -LiteralPath $src).Path
        if ($full -like "$PhotoRoot*" -or $full -like "$VideoRoot*") {
            [System.Windows.Forms.MessageBox]::Show("这个目录已经在照片库里了，请选择外部的素材目录。`r`n`r`n$full", '照片归类工具', 'OK', 'Warning') | Out-Null; return
        }
    }
    Save-State $src

    if ($chkExecute.Checked -and -not $SelfTest) {
        if ($isDevice) {
            $msg = "即将从【相机/便携设备】导入并归类：`r`n`r`n路径：$full`r`n`r`n流程：先把设备里的照片/视频复制到暂存目录「$CameraStagingRoot」，再归类并查重。`r`n相机/存储卡上的文件不会被改动、更不会被删除。`r`n`r`n确定继续吗？"
        }
        else {
            $n = @(Get-ChildItem -LiteralPath $full -Recurse -File -ErrorAction SilentlyContinue).Count
            $msg = "即将【实际执行】归类：`r`n`r`n来源：$full`r`n文件数：约 $n 个`r`n时段阈值：$($numGap.Value) 小时`r`n查重：$(if ($chkSkipDup.Checked) { '跳过' } else { '执行' })`r`n`r`n不会删除任何文件：重复只会被移到隔离目录。`r`n确定继续吗？"
        }
        $r = [System.Windows.Forms.MessageBox]::Show($msg, '确认执行', 'YesNo', 'Question')
        if ($r -ne 'Yes') { return }
    }

    $txtLog.Clear()
    $script:logPos = 0
    $script:logPath = Join-Path $WorkRoot ("gui-run-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    & $script:AppendLog ("日志：{0}`r`n`r`n" -f $script:logPath)

    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runner,
        '-Source', $full,
        '-PhotoRoot', $PhotoRoot,
        '-VideoRoot', $VideoRoot,
        '-WorkRoot', $WorkRoot,
        '-CameraStagingRoot', $CameraStagingRoot,
        '-LogPath', $script:logPath,
        '-GapHours', ([double]$numGap.Value).ToString([Globalization.CultureInfo]::InvariantCulture)
    )
    if ($chkExecute.Checked) { $argList += '-Execute' }
    if ($chkSkipDup.Checked) { $argList += '-SkipDedupe' }
    if ($chkCross.Checked) { $argList += '-AllowCrossFolderDedupe' }
    if ($numMax.Value -gt 0) { $argList += @('-MaxFiles', ([int]$numMax.Value).ToString()) }
    if ($CameraMaxFiles -gt 0) { $argList += @('-CameraMaxFiles', $CameraMaxFiles.ToString()) }

    try {
        $argString = (($argList | ForEach-Object { Quote-Arg ([string]$_) }) -join ' ')
        $script:proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argString -PassThru -WindowStyle Hidden
    }
    catch {
        [System.Windows.Forms.MessageBox]::Show("启动失败：$($_.Exception.Message)", '照片归类工具', 'OK', 'Error') | Out-Null
        return
    }
    $script:running = $true
    $btnStart.Enabled = $false; $btnStop.Enabled = $true; $chkExecute.Enabled = $false; $txtSrc.Enabled = $false
    $lblStatus.Text = if ($chkExecute.Checked) { '正在处理（真实移动）...' } else { '正在试运行（不会移动文件）...' }
    $lblStatus.ForeColor = [System.Drawing.Color]::DarkOrange
    $timer.Start()
})

$form.Add_DragEnter({
    if ($_.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) { $_.Effect = 'Copy' }
})
$form.Add_DragDrop({
    $items = $_.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop)
    if ($items -and $items.Count -gt 0) { $txtSrc.Text = $items[0] }
})

$form.Add_FormClosing({
    if ($script:running) {
        $r = [System.Windows.Forms.MessageBox]::Show('任务还在运行，确定退出吗？（已完成的移动都已记录在日志里）', '照片归类工具', 'YesNo', 'Warning')
        if ($r -ne 'Yes') { $_.Cancel = $true; return }
        try { $script:proc.Kill() } catch { }
    }
})

# ------------------------------------------------------------------- show
& $script:AppendLog ("照片自动归类工具`r`n素材目录：输入或拖入一个目录，然后点「开始处理」。`r`n`r`n照片库：{0}`r`n视频库：{1}`r`n日志与回滚：{2}`r`n相机暂存：{3}`r`n`r`n" -f $PhotoRoot, $VideoRoot, $WorkRoot, $CameraStagingRoot)

if ($SelfTest) {
    # headless smoke test: plant the path, run the real workflow, report and exit
    if (-not $TestSource) { Write-Host 'SELFTEST: no -TestSource given'; exit 2 }
    $txtSrc.Text = $TestSource
    $chkExecute.Checked = [bool]$TestExecute
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    $btnStart.PerformClick()
    $deadline = (Get-Date).AddMinutes(15)
    while ($script:running -and (Get-Date) -lt $deadline) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 200
    }
    $stillRunning = $script:running
    if ($stillRunning) { try { $script:proc.Kill() } catch { } }
    $lines = 0
    $status = ''
    if ($script:logPath -and (Test-Path -LiteralPath $script:logPath)) {
        $logLines = @(Get-Content -LiteralPath $script:logPath -Encoding UTF8)
        $lines = $logLines.Count
        $status = [string](@($logLines | Where-Object { $_ -like 'PIPELINE-STATUS:*' } | Select-Object -Last 1))
    }
    Write-Host ("SELFTEST: running={0} exit={1} status='{2}' log={3} lines={4}" -f $stillRunning, $script:lastExit, $status, $script:logPath, $lines)
    Write-Host ("SELFTEST: window-title='{0}' controls={1} config='{2}'" -f $form.Text, $form.Controls.Count, $ConfigPath)
    Write-Host ("SELFTEST: paths photo='{0}' video='{1}' work='{2}' camera='{3}'" -f $PhotoRoot, $VideoRoot, $WorkRoot, $CameraStagingRoot)

    # layout check: a label whose text is wider than its box would be truncated.
    # Labels with AutoEllipsis are intentionally elided (they carry a tooltip).
    $clipped = New-Object System.Collections.ArrayList
    $walk = {
        param($parent)
        foreach ($c in $parent.Controls) {
            if ($c -is [System.Windows.Forms.Label] -and $c.Text -and -not $c.AutoEllipsis) {
                $need = [System.Windows.Forms.TextRenderer]::MeasureText($c.Text, $c.Font).Width
                if ($need -gt $c.Width) { [void]$clipped.Add(('"{0}" need={1} have={2}' -f $c.Text, $need, $c.Width)) }
            }
            if ($c.HasChildren) { & $walk $c }
        }
    }
    & $walk $form
    if ($clipped.Count -eq 0) { Write-Host 'SELFTEST: layout OK (no truncated labels)' }
    else { Write-Host ('SELFTEST: TRUNCATED x{0}' -f $clipped.Count); $clipped | ForEach-Object { Write-Host ('   ' + $_) } }

    # the backend must have reported OK - "process ended" alone is not enough
    $ok = (-not $stillRunning) -and ($status -like '*PIPELINE-STATUS: OK*') -and ($script:lastExit -eq 0) -and ($clipped.Count -eq 0)
    $form.Close()
    if ($ok) { Write-Host 'SELFTEST: OK'; exit 0 } else { Write-Host 'SELFTEST: FAILED'; exit 1 }
}

[void]$form.ShowDialog()
$timer.Dispose()
$form.Dispose()
