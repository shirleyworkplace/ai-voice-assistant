#define MyAppName "Ava"
#define MyAppVersion "1.0.9"
#define MyAppPublisher "Ava"
#define MyAppExeName "Ava.exe"
#define MyAppId "{{B2802CFA-65AF-4FC2-9866-CBB768CC2362}"

#ifndef SourceDir
  #define SourceDir "..\dist\Ava"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Ava Setup
VersionInfoProductName=Ava
VersionInfoProductVersion={#MyAppVersion}
DefaultDirName={autopf}\Ava
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=AvaSetup-{#MyAppVersion}
SetupIconFile=..\assets\ava.ico
UninstallDisplayName=Ava
UninstallDisplayIcon={app}\ava.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\config.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "{#SourceDir}\*"; Excludes: "config.yaml"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\ava.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\ava.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent; WorkingDir: "{app}"
