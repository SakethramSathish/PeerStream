[Setup]
; NOTE: The value of AppId uniquely identifies this application. Do not use the same AppId value in installers for other applications.
; (To generate a new GUID, click Tools | Generate GUID inside the IDE.)
AppId={{D1A3960D-AB6D-44DB-9E09-8472986FE356}
AppName=PeerStream
AppVersion=1.0.0
AppPublisher=PeerStream
AppPublisherURL=https://github.com/
AppSupportURL=https://github.com/
AppUpdatesURL=https://github.com/
; Default to %LocalAppData%\Programs\PeerStream for frictionless user-level install
DefaultDirName={userappdata}\Programs\PeerStream
DisableProgramGroupPage=yes
; We do not need admin privileges for a user-level installation
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=PeerStream-Setup-1.0.0
SetupIconFile=PeerStream.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\PeerStream.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\PeerStream\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; NOTE: Don't use "Flags: ignoreversion" on any shared system files

[Icons]
Name: "{autoprograms}\PeerStream"; Filename: "{app}\PeerStream.exe"; IconFilename: "{app}\PeerStream.exe"
Name: "{autodesktop}\PeerStream"; Filename: "{app}\PeerStream.exe"; Tasks: desktopicon; IconFilename: "{app}\PeerStream.exe"

[Run]
Filename: "{app}\PeerStream.exe"; Description: "{cm:LaunchProgram,PeerStream}"; Flags: nowait postinstall skipifsilent
