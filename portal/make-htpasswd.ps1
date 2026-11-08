# 生成 nginx 的 htpasswd 账号文件（{SHA} 方案，nginx 原生支持）
# 由 make-htpasswd.bat 调用，一般不直接运行。
# 输入：环境变量 HP_USER / HP_PWD / HP_FILE / HP_RESULT
# 输出：HP_RESULT 指向的文件，一行：OK|用户名|密码|账号数  或  ERR|错误信息
$ErrorActionPreference = 'Stop'

$userName = "$env:HP_USER"
$password = "$env:HP_PWD"
$target = "$env:HP_FILE"
$resultFile = "$env:HP_RESULT"

function Write-Result([string]$text) {
    [System.IO.File]::WriteAllText($resultFile, $text, (New-Object System.Text.UTF8Encoding($false)))
}

try {
    if ([string]::IsNullOrEmpty($userName)) { throw '用户名为空' }

    # 没给密码就生成 12 位随机密码（去掉了容易看错的 I O l 0 1）
    if ([string]::IsNullOrEmpty($password)) {
        $chars = 'ACDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'
        $password = -join (1..12 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
    }

    $sha = [System.Security.Cryptography.SHA1]::Create()
    $hash = [Convert]::ToBase64String($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($password)))
    $line = $userName + ':{SHA}' + $hash

    # 读入已有账号，替换同名用户，其余保留
    $lines = @()
    if (Test-Path -LiteralPath $target) {
        $lines = @(Get-Content -LiteralPath $target -Encoding UTF8 |
            Where-Object { $_.Trim().Length -gt 0 -and -not $_.StartsWith($userName + ':') })
    }
    $lines += $line

    # 写成 UTF-8 无 BOM：带 BOM 会让 nginx 把第一行用户名读成乱码
    [System.IO.File]::WriteAllLines($target, $lines, (New-Object System.Text.UTF8Encoding($false)))
    Write-Result ('OK|' + $userName + '|' + $password + '|' + $lines.Count)
}
catch {
    Write-Result ('ERR|' + $_.Exception.Message)
}
