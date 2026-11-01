param([switch]$Tunnel)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

try { chcp 65001 | Out-Null } catch {}
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

if ($Tunnel) {
    $cloudflaredCommand = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cloudflaredCommand) {
        $env:DEFENSE_CLOUDFLARED_PATH = $cloudflaredCommand.Source
    } elseif (Test-Path -LiteralPath 'C:\Program Files (x86)\cloudflared\cloudflared.exe') {
        $env:DEFENSE_CLOUDFLARED_PATH = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
    } elseif (Test-Path -LiteralPath 'C:\Program Files\cloudflared\cloudflared.exe') {
        $env:DEFENSE_CLOUDFLARED_PATH = 'C:\Program Files\cloudflared\cloudflared.exe'
    } else {
        throw 'cloudflared was not found. Install it from https://developers.cloudflare.com/tunnel/downloads/'
    }
    $env:DEFENSE_TUNNEL = '1'
}

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
}
& '.venv\Scripts\python.exe' -c 'import openai, cryptography, qrcode, PIL'
if ($LASTEXITCODE -ne 0) {
    & '.venv\Scripts\python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}

$enteredKey = $false
if (-not $env:OPENAI_API_KEY) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object System.Windows.Forms.Form
    $form.Text = '韩语答辩助手：输入 Groq 密钥'
    $form.ClientSize = New-Object System.Drawing.Size(560, 205)
    $form.StartPosition = 'CenterScreen'
    $form.TopMost = $true
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false

    $help = New-Object System.Windows.Forms.Label
    $help.Text = '先在 Groq 网页复制完整 gsk_ 密钥，再点下方“从剪贴板粘贴”。'
    $help.SetBounds(20, 18, 520, 24)
    $form.Controls.Add($help)

    $keyBox = New-Object System.Windows.Forms.TextBox
    $keyBox.SetBounds(20, 50, 520, 28)
    $keyBox.UseSystemPasswordChar = $true
    $keyBox.ShortcutsEnabled = $true
    $form.Controls.Add($keyBox)

    $status = New-Object System.Windows.Forms.Label
    $status.Text = '密钥不会显示在屏幕上；粘贴后这里会显示字符数。'
    $status.SetBounds(20, 87, 520, 28)
    $form.Controls.Add($status)

    $pasteButton = New-Object System.Windows.Forms.Button
    $pasteButton.Text = '从剪贴板粘贴'
    $pasteButton.SetBounds(20, 130, 145, 38)
    $pasteButton.Add_Click({
        try {
            if (-not [System.Windows.Forms.Clipboard]::ContainsText()) {
                $status.Text = '剪贴板没有文字，请回 Groq 网页点 Copy。'
                return
            }
            $keyBox.Text = [System.Windows.Forms.Clipboard]::GetText().Trim()
            $status.Text = "已收到 $($keyBox.Text.Length) 个字符。"
        } catch {
            $status.Text = '读取剪贴板失败，请点击输入框按 Ctrl+V。'
        }
    })
    $form.Controls.Add($pasteButton)

    $keyBox.Add_TextChanged({ $status.Text = "已收到 $($keyBox.Text.Length) 个字符。" })

    $okButton = New-Object System.Windows.Forms.Button
    $okButton.Text = '确认并启动'
    $okButton.SetBounds(290, 130, 120, 38)
    $okButton.Add_Click({
        $candidate = $keyBox.Text.Trim().Trim('"').Trim("'")
        if ($candidate -match '\.\.\.|…' -or $candidate.Length -lt 16 -or
            -not ($candidate.StartsWith('gsk_') -or $candidate.StartsWith('sk-'))) {
            $status.Text = '不是完整密钥。请复制创建时弹窗中的整段 gsk_ 密钥。'
            return
        }
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    })
    $form.Controls.Add($okButton)

    $cancelButton = New-Object System.Windows.Forms.Button
    $cancelButton.Text = '取消'
    $cancelButton.SetBounds(420, 130, 120, 38)
    $cancelButton.Add_Click({ $form.Close() })
    $form.Controls.Add($cancelButton)
    $form.AcceptButton = $okButton
    $form.CancelButton = $cancelButton

    if ($form.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Write-Host '已取消，未启动。'
        exit 1
    }
    $env:OPENAI_API_KEY = $keyBox.Text.Trim().Trim('"').Trim("'")
    $keyBox.Clear()
    $form.Dispose()
    Write-Host '已收到密钥，正在验证。'
    $enteredKey = $true
}

try {
    & '.venv\Scripts\python.exe' -u app.py
} finally {
    if ($enteredKey) { Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue }
    if ($Tunnel) { Remove-Item Env:DEFENSE_TUNNEL -ErrorAction SilentlyContinue }
    if ($Tunnel) { Remove-Item Env:DEFENSE_CLOUDFLARED_PATH -ErrorAction SilentlyContinue }
}
