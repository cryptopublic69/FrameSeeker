$ErrorActionPreference = "Stop"

$rules = @(
    @{ Name = "FrameFinder API 8000 LAN"; Port = 8000 },
    @{ Name = "FrameFinder Web 3417 LAN"; Port = 3417 }
)

foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Set-NetFirewallRule `
            -Enabled True -Profile Any -Direction Inbound -Action Allow
        $existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter `
            -Protocol TCP -LocalPort $rule.Port
        $existing | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter `
            -RemoteAddress LocalSubnet
    } else {
        New-NetFirewallRule `
            -DisplayName $rule.Name `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort $rule.Port `
            -RemoteAddress LocalSubnet `
            -Profile Any | Out-Null
    }
}

Get-NetFirewallRule -DisplayName "FrameFinder*LAN" | ForEach-Object {
    $port = $_ | Get-NetFirewallPortFilter
    $address = $_ | Get-NetFirewallAddressFilter
    [pscustomobject]@{
        Name = $_.DisplayName
        Enabled = $_.Enabled
        Port = $port.LocalPort
        Remote = $address.RemoteAddress
        Action = $_.Action
    }
} | ConvertTo-Json | Set-Content -Encoding UTF8 "$PSScriptRoot\.lan-firewall-status.json"
