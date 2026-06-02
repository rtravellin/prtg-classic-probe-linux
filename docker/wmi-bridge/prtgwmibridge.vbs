' PRTG WMI Bridge — probe-side relay (classic EXE/Script Advanced custom sensor).
'
' Runs under Wine inside the PRTG probe. Fetches the Impacket sidecar over localhost
' HTTP and writes the returned <prtg> XML to stdout, which PRTG parses into channels.
' Wine cannot do remote WMI itself (see docker/WMI-ANALYSIS.md); the sidecar does.
'
' PRTG invokes:  cscript //nologo prtgwmibridge.vbs "<target>" "<type>"
'   %1 = target host (pass the device address, e.g. %host, via the sensor's parameters)
'   %2 = builtin type: mem | disk | cpu | uptime   (default mem)
'
' Sidecar base URL can be overridden with env WMI_BRIDGE_URL (default http://127.0.0.1:8910).

Option Explicit
Dim target, stype, baseUrl, url, http, body
If WScript.Arguments.Count >= 1 Then target = WScript.Arguments(0) Else target = ""
If WScript.Arguments.Count >= 2 Then stype  = WScript.Arguments(1) Else stype  = "mem"

baseUrl = CreateObject("WScript.Shell").ExpandEnvironmentStrings("%WMI_BRIDGE_URL%")
If baseUrl = "" Or InStr(baseUrl, "%") > 0 Then baseUrl = "http://127.0.0.1:8910"

If target = "" Then
  WScript.Echo "<prtg><error>1</error><text>no target passed to bridge sensor</text></prtg>"
  WScript.Quit 0
End If

url = baseUrl & "/wmi?target=" & target & "&type=" & stype

On Error Resume Next
Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
If Err.Number <> 0 Then
  Err.Clear
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
End If
If http Is Nothing Then
  WScript.Echo "<prtg><error>1</error><text>no HTTP client available under Wine</text></prtg>"
  WScript.Quit 0
End If

http.Open "GET", url, False
http.Send
If Err.Number <> 0 Then
  WScript.Echo "<prtg><error>1</error><text>bridge HTTP error: " & Err.Description & " (" & url & ")</text></prtg>"
  WScript.Quit 0
End If

body = http.ResponseText
If body = "" Then
  WScript.Echo "<prtg><error>1</error><text>empty response from sidecar " & url & "</text></prtg>"
  WScript.Quit 0
End If

WScript.Echo body
