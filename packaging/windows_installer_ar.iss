[Setup]
; Generic AR-only installer. Pass defines via ISCC:
;   ISCC.exe packaging\windows_installer_ar.iss /DAppName="Presence" /DAppVersion="1.0.0" /DSourceDir="C:\path\to\dist\Presence_cusear" /DAppExeName="Presence_cusear.exe"

#ifndef AppName
  #define AppName "cusear ar"
#endif

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\AutonomousWebAgencyDesktop"
#endif

#ifndef AppExeName
  #define AppExeName "AutonomousWebAgencyDesktop.exe"
#endif

AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename={#AppName}-setup
Compression=lzma
SolidCompression=yes

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
