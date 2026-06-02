# Example PRTG PowerShell Script sensor body, run by NATIVE Linux PowerShell Core (pwsh).
$v = $PSVersionTable.PSVersion.ToString()
Write-Output "<prtg>"
Write-Output "  <result>"
Write-Output "    <channel>PS Value</channel>"
Write-Output "    <value>777</value>"
Write-Output "  </result>"
Write-Output "  <result>"
Write-Output "    <channel>PS Random</channel>"
Write-Output ("    <value>" + (Get-Random -Minimum 1 -Maximum 100) + "</value>")
Write-Output "  </result>"
Write-Output "  <text>OK from PowerShell Core $v on Linux</text>"
Write-Output "</prtg>"
