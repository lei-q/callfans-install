# 探测宿主机 IPv4（排除回环/APIPA/Hyper-V 虚拟网卡），供安装器与 .env 写入
$ip = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -notlike '127.*' -and
    $_.IPAddress -notlike '169.254.*' -and
    $_.InterfaceAlias -notlike '*Loopback*' -and
    $_.InterfaceAlias -notlike '*vEthernet*'
} | Sort-Object InterfaceIndex | Select-Object -First 1
if ($ip) { Write-Output $ip.IPAddress } else { Write-Output '' }
