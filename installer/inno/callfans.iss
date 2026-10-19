; callfans Windows 安装脚本（Inno Setup 6+）
; 用法: ISCC.exe installer\inno\callfans.iss（需先跑完三个 PyInstaller spec）
; 布局: {app}\service\callfans-service.exe + {app}\ui\callfans-ui.exe + {app}\cli + {app}\.env
; 自启: HKCU Run 注册 callfans-ui（用户级，tech-design 模式 A），UI 再自拉起服务
; 部署: 安装时询问平台部署根目录，把 docker-compose.yml 复制过去，
;       并把 APP_ROOT / DEPLOY_ROOT / COMPOSE_FILE / FRONTEND_OUTPUT_DIR 写入 .env；
;       时区改为下拉选择（升级时预选部署 .env 已有值），与 HOST_IP 一并写入部署 .env；
;       nginx / xray 启动数据（证书、站点、geo 库等）缺失才补到部署目录，不覆盖现场
; 注意：Inno Pascal 自上而下编译、无前向声明——辅助例程必须定义在引用它的
;       事件处理器（InitializeWizard / CurPageChanged / CurStepChanged）之前

#define MyAppName "callfans"
#define MyAppVersion "0.9.1"
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
Source: "..\assets\.env"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "..\assets\get-ip.ps1"; DestDir: "{app}\assets"; Flags: ignoreversion
; nginx / xray 启动数据：整树打包（含空目录，如 nginx\logs），安装时按缺失补齐
Source: "..\assets\nginx\*"; DestDir: "{app}\assets\nginx"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\assets\xray\*"; DestDir: "{app}\assets\xray"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\.env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion

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
  TimezonePage: TWizardPage;
  TimezoneCombo: TNewComboBox;

{ ---------- 辅助例程（须先于事件处理器定义） ---------- }

{ 读取 .env 中某个键的值（KEY=VALUE 行；不存在或未设置返回 ''） }
function GetEnvValue(const FileName, Key: String): String;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if not FileExists(FileName) then
    Exit;
  if not LoadStringsFromFile(FileName, Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos(Key + '=', Lines[I]) = 1 then
    begin
      Result := Copy(Lines[I], Length(Key) + 2, Length(Lines[I]) - Length(Key) - 1);
      Exit;
    end;
end;

{ 探测宿主机 IPv4（powershell 脚本过滤回环/APIPA/Hyper-V 网卡，输出重定向到临时文件） }
function DetectHostIP(var IP: String): Boolean;
var
  ResultCode: Integer;
  TmpFile: String;
  Lines: TArrayOfString;
begin
  Result := False;
  IP := '';
  TmpFile := ExpandConstant('{tmp}\cf_hostip.txt');
  Exec(ExpandConstant('{cmd}'),
       '/C powershell -NoProfile -ExecutionPolicy Bypass -File "' +
       ExpandConstant('{app}\assets\get-ip.ps1') + '" > "' + TmpFile + '" 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if FileExists(TmpFile) and LoadStringsFromFile(TmpFile, Lines) then
    if GetArrayLength(Lines) > 0 then
    begin
      IP := Trim(Lines[0]);
      if Pos('.', IP) > 0 then
        Result := True;
    end;
  DeleteFile(TmpFile);
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

{ 时区下拉填充：'城市 - IANA 时区名'，写入 .env 取 ' - ' 右侧部分 }
procedure AddTimezoneItems;
begin
  with TimezoneCombo.Items do
  begin
    Add('上海 - Asia/Shanghai');
    Add('香港 - Asia/Hong_Kong');
    Add('台北 - Asia/Taipei');
    Add('东京 - Asia/Tokyo');
    Add('首尔 - Asia/Seoul');
    Add('胡志明 - Asia/Ho_Chi_Minh');
    Add('吉隆坡 - Asia/Kuala_Lumpur');
    Add('新加坡 - Asia/Singapore');
    Add('曼谷 - Asia/Bangkok');
    Add('雅加达 - Asia/Jakarta');
    Add('加尔各答/新德里 - Asia/Kolkata');
    Add('卡拉奇 - Asia/Karachi');
    Add('达卡 - Asia/Dhaka');
    Add('迪拜 - Asia/Dubai');
    Add('德黑兰 - Asia/Tehran');
    Add('巴格达 - Asia/Baghdad');
    Add('利雅得 - Asia/Riyadh');
    Add('伊斯坦布尔 - Europe/Istanbul');
    Add('加德满都 - Asia/Kathmandu');
    Add('仰光 - Asia/Yangon');
    Add('符拉迪沃斯托克 - Asia/Vladivostok');
    Add('伦敦 - Europe/London');
    Add('都柏林 - Europe/Dublin');
    Add('巴黎 - Europe/Paris');
    Add('柏林 - Europe/Berlin');
    Add('罗马 - Europe/Rome');
    Add('马德里 - Europe/Madrid');
    Add('阿姆斯特丹 - Europe/Amsterdam');
    Add('布鲁塞尔 - Europe/Brussels');
    Add('维也纳 - Europe/Vienna');
    Add('苏黎世 - Europe/Zurich');
    Add('斯德哥尔摩 - Europe/Stockholm');
    Add('奥斯陆 - Europe/Oslo');
    Add('哥本哈根 - Europe/Copenhagen');
    Add('赫尔辛基 - Europe/Helsinki');
    Add('华沙 - Europe/Warsaw');
    Add('布拉格 - Europe/Prague');
    Add('布达佩斯 - Europe/Budapest');
    Add('雅典 - Europe/Athens');
    Add('莫斯科 - Europe/Moscow');
    Add('基辅 - Europe/Kiev');
    Add('开罗 - Africa/Cairo');
    Add('阿比让 - Africa/Abidjan');
    Add('阿克拉 - Africa/Accra');
    Add('亚的斯亚贝巴 - Africa/Addis_Ababa');
    Add('阿尔及尔 - Africa/Algiers');
    Add('卡萨布兰卡 - Africa/Casablanca');
    Add('约翰内斯堡 - Africa/Johannesburg');
    Add('拉各斯 - Africa/Lagos');
    Add('内罗毕 - Africa/Nairobi');
    Add('纽约 - America/New_York');
    Add('芝加哥 - America/Chicago');
    Add('丹佛 - America/Denver');
    Add('洛杉矶 - America/Los_Angeles');
    Add('凤凰城 - America/Phoenix');
    Add('安克雷奇 - America/Anchorage');
    Add('檀香山 - Pacific/Honolulu');
    Add('多伦多 - America/Toronto');
    Add('温哥华 - America/Vancouver');
    Add('墨西哥城 - America/Mexico_City');
    Add('圣保罗/巴西利亚 - America/Sao_Paulo');
    Add('布宜诺斯艾利斯 - America/Argentina/Buenos_Aires');
    Add('圣地亚哥 - America/Santiago');
    Add('利马 - America/Lima');
    Add('波哥大 - America/Bogota');
    Add('加拉加斯 - America/Caracas');
    Add('费尔南多·迪诺罗尼亚 - America/Noronha');
    Add('亚马逊(马瑙斯) - America/Manaus');
    Add('阿克里 - America/Rio_Branco');
    Add('凯西站 - Antarctica/Casey');
    Add('戴维斯站 - Antarctica/Davis');
    Add('麦克默多站 - Antarctica/McMurdo');
    Add('帕尔默站 - Antarctica/Palmer');
    Add('悉尼 - Australia/Sydney');
    Add('墨尔本 - Australia/Melbourne');
    Add('布里斯班 - Australia/Brisbane');
    Add('珀斯 - Australia/Perth');
    Add('阿德莱德 - Australia/Adelaide');
    Add('奥克兰 - Pacific/Auckland');
    Add('斐济 - Pacific/Fiji');
    Add('关岛 - Pacific/Guam');
    Add('塔希提 - Pacific/Tahiti');
    Add('萨摩亚 - Pacific/Apia');
  end;
end;

{ 按 IANA 名预选（菜单文字 '城市 - IANA'）；找到返回 True }
function SelectTimezone(const TZ: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 0 to TimezoneCombo.Items.Count - 1 do
    if Pos(' - ' + TZ, TimezoneCombo.Items[I]) > 0 then
    begin
      TimezoneCombo.ItemIndex := I;
      Result := True;
      Exit;
    end;
end;

{ 当前选中的 IANA 时区名（下拉 '城市 - IANA' 取右侧部分） }
function SelectedTimezone: String;
var
  S: String;
begin
  Result := '';
  if TimezoneCombo.ItemIndex < 0 then
    Exit;
  S := TimezoneCombo.Items[TimezoneCombo.ItemIndex];
  if Pos(' - ', S) > 0 then
    Result := Copy(S, Pos(' - ', S) + 3, Length(S) - Pos(' - ', S) - 2);
end;

{ 递归复制目录：缺什么补什么，已存在的文件一律不覆盖。

  nginx / xray 启动数据用（证书、站点、geo 库）：首装整树落地；升级只补
  新增文件，现场改过的证书 / 配置 / 前端不被安装包回退。 }
procedure CopyTreeSkipExisting(const SrcDir, DstDir: String);
var
  Find: TFindRec;
  SrcPath, DstPath: String;
begin
  ForceDirectories(DstDir);
  if FindFirst(AddBackslash(SrcDir) + '*', Find) then
  begin
    try
      repeat
        if (Find.Name = '.') or (Find.Name = '..') then
          Continue;
        SrcPath := AddBackslash(SrcDir) + Find.Name;
        DstPath := AddBackslash(DstDir) + Find.Name;
        if Find.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
          CopyTreeSkipExisting(SrcPath, DstPath)
        else if not FileExists(DstPath) then
          FileCopy(SrcPath, DstPath, False);
      until not FindNext(Find);
    finally
      FindClose(Find);
    end;
  end;
end;

{ ---------- 事件处理器 ---------- }

procedure InitializeWizard;
var
  TZLabel: TNewStaticText;
begin
  // 平台部署根目录（docker-compose.yml 落地处，前端/数据也在其下）
  DeployPage := CreateInputDirPage(wpSelectDir,
    '平台部署目录', '平台文件将部署到哪个目录？',
    '安装程序会把 docker-compose.yml 复制到该目录，并写入 .env 的 ' +
    'COMPOSE_FILE / FRONTEND_OUTPUT_DIR 配置。',
    False, '');
  DeployPage.Add('平台部署根目录(&R):');
  DeployPage.Values[0] := 'C:\callfans_standard';

  // 平台时区（写入部署 .env 的 TIMEZONE）：下拉选择，覆盖常用时区
  TimezonePage := CreateCustomPage(DeployPage.ID,
    '时区设置', '平台容器使用的时区');
  TZLabel := TNewStaticText.Create(TimezonePage);
  TZLabel.Caption := '将写入部署目录 .env 的 TIMEZONE，全部平台容器按此时区运行。'  // 页顶说明
    + #13#10#13#10 + '时区(&T):';
  TZLabel.WordWrap := True;
  TZLabel.Width := TimezonePage.SurfaceWidth;
  TZLabel.Parent := TimezonePage.Surface;
  TZLabel.AdjustHeight;
  TimezoneCombo := TNewComboBox.Create(TimezonePage);
  TimezoneCombo.Style := csDropDownList;  // 只可选，不可自由输入
  TimezoneCombo.Top := TZLabel.Top + TZLabel.Height + ScaleY(8);
  TimezoneCombo.Width := TimezonePage.SurfaceWidth;
  TimezoneCombo.Parent := TimezonePage.Surface;
  AddTimezoneItems;
  SelectTimezone('Asia/Shanghai');  // 兜底默认；进入该页时按部署 .env 再预选
end;

{ 进入时区页时按部署目录 .env 已有 TIMEZONE 预选（升级不丢现场选择） }
procedure CurPageChanged(PageID: Integer);
begin
  if PageID = TimezonePage.ID then
    if not SelectTimezone(
           GetEnvValue(AddBackslash(DeployPage.Values[0]) + '.env', 'TIMEZONE')) then
      SelectTimezone('Asia/Shanghai');
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
  DeployEnv, HostIP: String;
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

  // nginx 启动数据 → 平台根目录\nginx；xray 启动数据 → 平台根目录\server\xray
  // （缺失才补：现场证书 / 站点 / 配置不被覆盖）
  CopyTreeSkipExisting(ExpandConstant('{app}\assets\nginx'),
                       AddBackslash(DeployDir) + 'nginx');
  CopyTreeSkipExisting(ExpandConstant('{app}\assets\xray'),
                       AddBackslash(DeployDir) + 'server\xray');

  // 部署 .env：不存在则从模板生成（存在则保留现场改过的密码等）
  DeployEnv := AddBackslash(DeployDir) + '.env';
  if not FileExists(DeployEnv) then
  begin
    if FileExists(AddBackslash(DeployDir) + '.env.bak') then
      FileCopy(AddBackslash(DeployDir) + '.env.bak', DeployEnv, False)
    else
      FileCopy(ExpandConstant('{app}\assets\.env'), DeployEnv, False);
  end;
  // TIMEZONE（下拉选择）与 HOST_IP（自动探测）每次安装都写入部署 .env
  if SelectedTimezone <> '' then
    SetEnvValue(DeployEnv, 'TIMEZONE', SelectedTimezone);
  if DetectHostIP(HostIP) then
    SetEnvValue(DeployEnv, 'HOST_IP', HostIP)
  else
    SetEnvValue(DeployEnv, 'HOST_IP', '127.0.0.1');

  // 应用自身 .env：不存在则从示例生成，然后写入两个根目录与派生配置
  EnvPath := ExpandConstant('{app}\.env');
  if not FileExists(EnvPath) then
    FileCopy(ExpandConstant('{app}\.env.example'), EnvPath, False);
  SetEnvValue(EnvPath, 'APP_ROOT', ExpandConstant('{app}'));
  SetEnvValue(EnvPath, 'DEPLOY_ROOT', DeployDir);
  SetEnvValue(EnvPath, 'COMPOSE_FILE', ComposeDst);
  SetEnvValue(EnvPath, 'FRONTEND_OUTPUT_DIR', DeployDir);
end;
