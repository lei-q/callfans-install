; callfans Windows 安装脚本（Inno Setup 6+）
; 用法: ISCC.exe installer\inno\callfans.iss（需先跑完三个 PyInstaller spec）
; 布局: {app}\service\callfans-service.exe + {app}\ui\callfans-ui.exe + {app}\cli + {app}\.env
; 自启: HKCU Run 注册 callfans-ui（用户级，tech-design 模式 A），UI 再自拉起服务
; 部署: 安装时询问平台部署根目录，把 docker-compose.yml 复制过去，
;       并把 APP_ROOT / DEPLOY_ROOT / COMPOSE_FILE / FRONTEND_OUTPUT_DIR 写入 .env

#define MyAppName "callfans"
#define MyAppVersion "0.7.0"
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
SetupIconFile=..\assets\logo.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Files]
Source: "..\..\dist\callfans-service\*"; DestDir: "{app}\service"; Flags: recursesubdirs ignoreversion
Source: "..\..\dist\callfans-ui\*"; DestDir: "{app}\ui"; Flags: recursesubdirs ignoreversion
Source: "..\..\dist\callfans-cli\*"; DestDir: "{app}\cli"; Flags: recursesubdirs ignoreversion
Source: "..\assets\docker-compose.yml"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "..\..\.env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion onlyifdoesntexist

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "{#MyAppName}"; ValueData: """{app}\ui\{#MyAppExeName}"""; Flags: uninsdeletevalue

[Tasks]
; 安装时默认勾选"创建桌面快捷方式"（可取消）；/SILENT 静默装同样创建
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; \
    GroupDescription: "附加任务:"; Flags: checkedonce

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\ui\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\ui\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\ui\{#MyAppExeName}"; Description: "启动 助手管家"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前停掉服务与托盘
Filename: "{cmd}"; Parameters: "/C taskkill /IM callfans-service.exe /F"; Flags: runhidden; RunOnceId: "KillSvc"
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F"; Flags: runhidden; RunOnceId: "KillUi"

[Code]
var
  DeployPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  // 平台部署根目录（docker-compose.yml 落地处，前端/数据也在其下）
  DeployPage := CreateInputDirPage(wpSelectDir,
    '平台部署目录', '平台文件将部署到哪个目录？',
    '安装程序会把 docker-compose.yml 复制到该目录，并写入 .env 的 ' +
    'COMPOSE_FILE / FRONTEND_OUTPUT_DIR 配置。',
    False, '');
  DeployPage.Add('平台部署根目录(&R):');
  DeployPage.Values[0] := 'C:\callfans_standard';
end;

function GetDeployDir(Param: String): String;
begin
  Result := DeployPage.Values[0];
end;

{ 设置 .env 中某个键：存在则替换，不存在则追加（保留其余内容与注释） }
procedure SetEnvValue(const FileName, Key, Value: String);
var
  Lines: TArrayOfString;
  I, N: Integer;
  Found: Boolean;
begin
  N := 0;
  if FileExists(FileName) then
  begin
    if not LoadStringsFromFile(FileName, Lines) then
      N := 0
    else
      N := GetArrayLength(Lines);
  end;
  Found := False;
  for I := 0 to N - 1 do
    if Pos(Key + '=', Lines[I]) = 1 then
    begin
      Lines[I] := Key + '=' + Value;
      Found := True;
    end;
  if not Found then
  begin
    SetArrayLength(Lines, N + 1);
    Lines[N] := Key + '=' + Value;
  end;
  SaveStringsToFile(FileName, Lines, False);
end;

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
  DeployDir, EnvPath, ComposeSrc, ComposeDst: String;
begin
  if CurStep <> ssPostInstall then
    Exit;

  // {app}\cli 加入用户 PATH（新开的终端生效）
  Exec(ExpandConstant('{cmd}'),
       '/C setx PATH "%PATH%;{app}\cli" >nul 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  DeployDir := DeployPage.Values[0];
  if DeployDir = '' then
    Exit;
  ForceDirectories(DeployDir);

  // docker-compose.yml 部署到平台根目录（已存在则先备份，避免覆盖现场改动）
  ComposeSrc := ExpandConstant('{app}\assets\docker-compose.yml');
  ComposeDst := AddBackslash(DeployDir) + 'docker-compose.yml';
  if FileExists(ComposeSrc) then
  begin
    if FileExists(ComposeDst) then
      FileCopy(ComposeDst, ComposeDst + '.bak', False);
    FileCopy(ComposeSrc, ComposeDst, False);
  end;

  // .env：不存在则从示例生成，然后写入两个根目录与派生配置
  EnvPath := ExpandConstant('{app}\.env');
  if not FileExists(EnvPath) then
    FileCopy(ExpandConstant('{app}\.env.example'), EnvPath, False);
  SetEnvValue(EnvPath, 'APP_ROOT', ExpandConstant('{app}'));
  SetEnvValue(EnvPath, 'DEPLOY_ROOT', DeployDir);
  SetEnvValue(EnvPath, 'COMPOSE_FILE', ComposeDst);
  SetEnvValue(EnvPath, 'FRONTEND_OUTPUT_DIR', DeployDir);
end;
