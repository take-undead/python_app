<#
    win_rpa の練習用ダミーアプリ（架空の「売上管理システム」）。

    自動操作の対象アプリを模した WinForms アプリ。実機がなくても
    ピッカー・実行エンジン・CSV 結合の検証ができるようにするためのもの。

    Tkinter ではなく WinForms を使っているのは、Tkinter のウィジェットが
    UIA からまったく見えないため（名前のない Pane にしかならない）。
    WinForms なら実際の業務アプリと同じように AutomationId と Name が出る。

    起動は tools/mock_target.py から行う。このファイルを直接
    powershell -File で叩くと、Windows PowerShell 5.1 が BOM なし UTF-8 を
    ANSI として読むため日本語が壊れる。

    受け取る変数（tools/mock_target.py が先頭に差し込む）:
        $Delay   ... 「集計」にかかる秒数。待ち処理の検証用
        $Variant ... $true にするとボタン名が変わる。アプリ更新の再現用
#>

if (-not (Test-Path variable:Delay))   { $Delay = 3 }
if (-not (Test-Path variable:Variant)) { $Variant = $false }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

[System.Windows.Forms.Application]::EnableVisualStyles()

$font = New-Object System.Drawing.Font('Meiryo UI', 10)

$form = New-Object System.Windows.Forms.Form
$form.Text = '売上管理システム'
$form.Name = 'frmMain'
$form.ClientSize = New-Object System.Drawing.Size(460, 290)
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.Font = $font
$form.StartPosition = 'CenterScreen'

# --- 対象年月 -----------------------------------------------------------
$lblMonth = New-Object System.Windows.Forms.Label
$lblMonth.Text = '対象年月'
$lblMonth.Name = 'lblMonth'
$lblMonth.Location = New-Object System.Drawing.Point(20, 24)
$lblMonth.Size = New-Object System.Drawing.Size(80, 24)
$form.Controls.Add($lblMonth)

$txtMonth = New-Object System.Windows.Forms.TextBox
$txtMonth.Name = 'txtMonth'
$txtMonth.Text = (Get-Date).ToString('yyyy-MM')
$txtMonth.Location = New-Object System.Drawing.Point(104, 21)
$txtMonth.Size = New-Object System.Drawing.Size(120, 24)
$form.Controls.Add($txtMonth)

# --- 集計区分（選択肢のある項目の検証用）--------------------------------
$lblKind = New-Object System.Windows.Forms.Label
$lblKind.Text = '集計区分'
$lblKind.Name = 'lblKind'
$lblKind.Location = New-Object System.Drawing.Point(20, 60)
$lblKind.Size = New-Object System.Drawing.Size(80, 24)
$form.Controls.Add($lblKind)

$cmbKind = New-Object System.Windows.Forms.ComboBox
$cmbKind.Name = 'cmbKind'
$cmbKind.DropDownStyle = 'DropDownList'
[void]$cmbKind.Items.AddRange(@('商品別', '担当者別', '得意先別'))
$cmbKind.SelectedIndex = 0
$cmbKind.Location = New-Object System.Drawing.Point(104, 57)
$cmbKind.Size = New-Object System.Drawing.Size(160, 24)
$form.Controls.Add($cmbKind)

# --- 税抜き表示（チェックの検証用）--------------------------------------
$chkTax = New-Object System.Windows.Forms.CheckBox
$chkTax.Name = 'chkTax'
$chkTax.Text = '税抜きで出力する'
$chkTax.Location = New-Object System.Drawing.Point(104, 92)
$chkTax.Size = New-Object System.Drawing.Size(200, 24)
$form.Controls.Add($chkTax)

# --- ボタン -------------------------------------------------------------
$btnAggregate = New-Object System.Windows.Forms.Button
$btnAggregate.Name = 'btnAggregate'
if ($Variant) { $btnAggregate.Text = '集計処理' } else { $btnAggregate.Text = '集計' }
$btnAggregate.Location = New-Object System.Drawing.Point(104, 128)
$btnAggregate.Size = New-Object System.Drawing.Size(110, 34)
$form.Controls.Add($btnAggregate)

$btnExport = New-Object System.Windows.Forms.Button
$btnExport.Name = 'btnExport'
if ($Variant) { $btnExport.Text = 'CSV書き出し' } else { $btnExport.Text = 'CSV出力' }
$btnExport.Location = New-Object System.Drawing.Point(224, 128)
$btnExport.Size = New-Object System.Drawing.Size(110, 34)
$btnExport.Enabled = $false          # 集計するまで押せない（待ち条件の検証用）
$form.Controls.Add($btnExport)

# --- 合計金額（画面にしか出ない数値の検証用）----------------------------
# CSV には出さない。「表示された数値を記録する」で拾う対象。
# 桁区切り付きで出すのは、数値の取り出し（1,234,567 → 1234567）を試すため
$lblTotal = New-Object System.Windows.Forms.Label
$lblTotal.Text = '合計金額'
$lblTotal.Name = 'lblTotal'
$lblTotal.Location = New-Object System.Drawing.Point(20, 176)
$lblTotal.Size = New-Object System.Drawing.Size(80, 24)
$form.Controls.Add($lblTotal)

$txtTotal = New-Object System.Windows.Forms.TextBox
$txtTotal.Name = 'txtTotal'
$txtTotal.Text = '0'
$txtTotal.ReadOnly = $true
$txtTotal.TextAlign = 'Right'
$txtTotal.Location = New-Object System.Drawing.Point(104, 173)
$txtTotal.Size = New-Object System.Drawing.Size(160, 24)
$form.Controls.Add($txtTotal)

# --- 状態表示 -----------------------------------------------------------
$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Name = 'lblStatus'
$lblStatus.Text = '準備完了'
$lblStatus.Location = New-Object System.Drawing.Point(20, 216)
$lblStatus.Size = New-Object System.Drawing.Size(420, 26)
$lblStatus.BorderStyle = 'FixedSingle'
$lblStatus.TextAlign = 'MiddleLeft'
$form.Controls.Add($lblStatus)

# --- 集計（時間のかかる処理を模す）--------------------------------------
$script:rowCount = 0
$script:total = 0

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(1, $Delay) * 1000

$timer.Add_Tick({
    $timer.Stop()
    $script:rowCount = Get-Random -Minimum 80 -Maximum 400
    $script:total = $script:rowCount * (Get-Random -Minimum 1000 -Maximum 9000)
    $txtTotal.Text = '{0:N0}' -f $script:total
    $lblStatus.Text = '集計完了（' + $script:rowCount + ' 件）'
    $btnAggregate.Enabled = $true
    $btnExport.Enabled = $true
})

$btnAggregate.Add_Click({
    $txtTotal.Text = '0'
    $lblStatus.Text = '集計中...'
    $btnAggregate.Enabled = $false
    $btnExport.Enabled = $false
    $timer.Start()
})

# --- CSV 出力 -----------------------------------------------------------
$btnExport.Add_Click({
    # WinForms のイベントハンドラは例外を握り潰す。原因が分からなくなるので
    # 画面に出す（このアプリ自体の不具合と、自動操作側の不具合を切り分けるため）
    $lblStatus.Text = '保存先を選んでください...'
    try {
        $dialog = New-Object System.Windows.Forms.SaveFileDialog
        $dialog.Title = '名前を付けて保存'
        $dialog.Filter = 'CSV ファイル (*.csv)|*.csv'
        $dialog.FileName = '売上_' + ($txtMonth.Text -replace '-', '') + '.csv'

        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            $lblStatus.Text = '出力を中止しました。'
            return
        }

        $kind = $cmbKind.SelectedItem
        if ($chkTax.Checked) { $taxLabel = '税抜' } else { $taxLabel = '税込' }

        $lines = New-Object 'System.Collections.Generic.List[string]'
        [void]$lines.Add('年月,区分,税区分,項目,数量,金額')

        for ($i = 1; $i -le $script:rowCount; $i++) {
            $qty = Get-Random -Minimum 1 -Maximum 50
            $amount = $qty * (Get-Random -Minimum 100 -Maximum 5000)
            $item = '項目' + ('{0:D3}' -f $i)
            [void]$lines.Add($txtMonth.Text + ',' + $kind + ',' + $taxLabel + ',' +
                             $item + ',' + $qty + ',' + $amount)
        }

        [System.IO.File]::WriteAllLines(
            $dialog.FileName, $lines, (New-Object System.Text.UTF8Encoding $false))

        $lblStatus.Text = '出力しました: ' + $dialog.FileName
    }
    catch {
        $lblStatus.Text = 'ダミー側でエラー: ' + $_.Exception.Message
    }
})

[void]$form.ShowDialog()
$form.Dispose()
