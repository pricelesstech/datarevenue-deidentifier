; Inno Setup script: wraps dist\Data Revenue De-Identifier\ into one Setup .exe.
; Installs per user (no administrator rights needed), adds Start menu + optional desktop
; shortcut and an uninstaller. Build:  iscc /DAppVersion=1.1.0 installer\windows.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Data Revenue De-Identifier"

[Setup]
AppId={{6E0C6F7B-3D52-4B7A-9E0B-DA7A0DE1D001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Data Revenue LLC
AppPublisherURL=https://datarevenue.io
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=DataRevenue-DeIdentifier-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppName}.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent
