#define AppName "Parsezen"
#define AppVersion "1.2.0"

[Setup]
AppId={{B11F2D89-386D-42EF-9468-A8F69C329627}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Parsezen contributors
AppPublisherURL=https://github.com/jaimeacena/parsezen
AppSupportURL=https://github.com/jaimeacena/parsezen/issues
AppUpdatesURL=https://github.com/jaimeacena/parsezen/releases/latest
LicenseFile=..\..\LICENSE
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\..\outputs
OutputBaseFilename=Parsezen-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\..\assets\branding\generated\parsezen-app-icon.ico
UninstallDisplayIcon={app}\{#AppName}.exe
UninstallDisplayName={#AppName}
VersionInfoCompany=Parsezen contributors
VersionInfoDescription=Instalador de Parsezen
VersionInfoProductName={#AppName}
VersionInfoVersion={#AppVersion}

[Files]
Source: "..\..\outputs\package\Parsezen\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\Parsezen.exe"
Type: files; Name: "{group}\Parsezen.lnk"
Type: files; Name: "{autodesktop}\Parsezen.lnk"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Abrir {#AppName}"; Flags: nowait postinstall skipifsilent
