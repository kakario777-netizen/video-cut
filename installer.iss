; 비디오 컷 설치 파일 (Inno Setup)
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{4ECD5E5E-C20B-4A35-BAF6-42877989CF50}
AppName=비디오 컷
AppVersion={#MyAppVersion}
AppVerName=비디오 컷 {#MyAppVersion}
AppPublisher=kakario777
DefaultDirName={autopf}\VideoCut
DisableProgramGroupPage=yes
DisableDirPage=yes
UninstallDisplayIcon={app}\VideoCut.exe
UninstallDisplayName=비디오 컷
OutputDir=installer_out
OutputBaseFilename=VideoCut_Setup
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
WizardStyle=modern
SetupIconFile=icon.ico
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany=kakario777
VersionInfoDescription=비디오 컷 설치
VersionInfoProductName=VideoCut

[Languages]
#if FileExists(CompilerPath + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Files]
Source: "dist\VideoCut\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\비디오 컷"; Filename: "{app}\VideoCut.exe"
Name: "{autodesktop}\비디오 컷"; Filename: "{app}\VideoCut.exe"; Check: IsFirstInstall

[Run]
Filename: "{app}\VideoCut.exe"; Description: "비디오 컷 실행"; Flags: nowait postinstall skipifsilent
Filename: "{app}\VideoCut.exe"; Parameters: "--updated"; Flags: nowait runasoriginaluser; Check: ShouldRestart

[Code]
const
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{4ECD5E5E-C20B-4A35-BAF6-42877989CF50}_is1';
var
  FirstInstall: Boolean;

{ 처음 설치일 때만 바탕화면 바로가기를 만듦 (업데이트 때는 건드리지 않음) }
function InitializeSetup(): Boolean;
begin
  FirstInstall := not (RegKeyExists(HKLM, UninstKey) or RegKeyExists(HKCU, UninstKey));
  Result := True;
end;

function IsFirstInstall(): Boolean;
begin
  Result := FirstInstall;
end;

function ShouldRestart(): Boolean;
begin
  Result := ExpandConstant('{param:restart|0}') = '1';
end;
