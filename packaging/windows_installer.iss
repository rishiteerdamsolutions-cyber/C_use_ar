[Setup]
AppName=cusear ar
AppVersion=1.0.0
DefaultDirName={autopf}\cusear-ar
DefaultGroupName=cusear ar
OutputDir=..\dist
OutputBaseFilename=cusear-ar-setup
Compression=lzma
SolidCompression=yes

[Files]
Source: "..\dist\AutonomousWebAgencyDesktop\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\cusear ar"; Filename: "{app}\AutonomousWebAgencyDesktop.exe"
Name: "{commondesktop}\cusear ar"; Filename: "{app}\AutonomousWebAgencyDesktop.exe"

[Run]
Filename: "{app}\AutonomousWebAgencyDesktop.exe"; Description: "Launch cusear ar"; Flags: nowait postinstall skipifsilent
