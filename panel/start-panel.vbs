' start-panel.vbs —— 无窗口启动控制面板（幂等）
'
' 逻辑：先探测面板是否已在监听。
'   已在运行 → 只打开浏览器，不再派生第二个实例；
'   未运行   → 以 GUI 子系统的 pythonw 拉起面板（无任何窗口），
'               面板自己会在 0.8 秒后打开默认浏览器。
' 双击多少次都只会有一个面板实例。
Option Explicit
Dim fso, sh, here, root, py, script, url

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(here)
url = "http://127.0.0.1:8790/"   ' 与 config.json 的 panel_port 保持一致

' ---- 已在运行？直接开浏览器 ----
If PanelUp(url & "api/status") Then
  sh.Run """" & url & """", 1, False
  WScript.Quit 0
End If

' ---- 未运行：后台拉起 ----
py = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then py = root & "\python\pythonw.exe"
If Not fso.FileExists(py) Then py = root & "\.venv\Scripts\python.exe"

If Not fso.FileExists(py) Then
  MsgBox "找不到 Python 解释器：" & py & vbCrLf & _
         "请先在项目根目录执行：" & vbCrLf & _
         "  python -m venv .venv", 16, "WorkBuddy2API 控制面板"
  WScript.Quit 1
End If

script = here & "\wb_control_panel.py"
sh.CurrentDirectory = here
' 0 = 隐藏窗口，False = 不等待
sh.Run """" & py & """ """ & script & """", 0, False

' ---- 探测：200 表示面板已在服务 ----
Function PanelUp(target)
  Dim http
  PanelUp = False
  On Error Resume Next
  Set http = CreateObject("WinHTTP.WinHTTPRequest.5.1")
  If Err.Number <> 0 Then Exit Function
  http.SetTimeouts 1000, 1000, 1500, 1500
  http.Open "GET", target, False
  If Err.Number <> 0 Then Exit Function
  http.Send
  If Err.Number <> 0 Then Exit Function
  PanelUp = (http.Status = 200)
  On Error GoTo 0
End Function
