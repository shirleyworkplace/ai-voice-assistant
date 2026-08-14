#define MyAppName "Ava"
#define MyAppVersion "1.0.9"
#define MyAppPublisher "Ava"
#define MyAppExeName "Ava.exe"
#define MyAppId "{{8F3C2A91-4D6E-4B17-9C55-2E8A7D1F0B44}"

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
OutputBaseFilename=AvaSetup-1.0.9
SetupIconFile=..\assets\AvaIcon.ico
UninstallDisplayName=Ava
; Use a unique icon filename to bust Windows desktop/icon caches.
UninstallDisplayIcon={app}\AvaIcon.ico
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
Source: "{#SourceDir}\AvaIcon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\AvaIcon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\AvaIcon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent; WorkingDir: "{app}"
