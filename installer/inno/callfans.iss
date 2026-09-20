; callfans Windows 安装脚本（Inno Setup 6+）
; 用法: ISCC.exe installer\inno\callfans.iss（需先跑完两个 PyInstaller spec）
; 布局: {app}\service\callfans-service.exe + {app}\ui\callfans-ui.exe + {app}\.env
; 自启: HKCU Run 注册 callfans-ui（用户级，tech-design 模式 A），UI 再自拉起服务

#define MyAppName "callfans"
#define MyAppVersion "0.3.8"
#define MyAppExeName "callfans-ui.exe"

[Setup]
AppId={{8C7B6F5A-1C2E-4D9B-9A30-01C411F4A5C7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=callfans-setup-x64
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Files]
Source: "..\..\dist\callfans-service\*"; DestDir: "{app}\service"; Flags: recursesubdirs ignoreversion
Source: "..\..\dist\callfans-ui\*"; DestDir: "{app}\ui"; Flags: recursesubdirs ignoreversion
Source: "..\..\dist\callfans-cli\*"; DestDir: "{app}\cli"; Flags: recursesubdirs ignoreversion
Source: "..\..\.env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion onlyifdoesntexist

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "{#MyAppName}"; ValueData: """{app}\ui\{#MyAppExeName}"""; Flags: uninsdeletevalue

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // 升级安装先停旧进程，否则安装后跑的仍是旧代码
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM callfans-service.exe /F 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM callfans-ui.exe /F 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  // {app}\cli 加入用户 PATH（新开的终端生效）
  if CurStep = ssPostInstall then
    Exec(ExpandConstant('{cmd}'),
         '/C setx PATH "%PATH%;{app}\cli" >nul 2>nul',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\ui\{#MyAppExeName}"

[Run]
Filename: "{app}\ui\{#MyAppExeName}"; Description: "启动 callfans"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前停掉服务与托盘
Filename: "{cmd}"; Parameters: "/C taskkill /IM callfans-service.exe /F"; Flags: runhidden; RunOnceId: "KillSvc"
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F"; Flags: runhidden; RunOnceId: "KillUi"
