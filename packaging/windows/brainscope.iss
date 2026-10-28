; Inno Setup script. Built in CI: iscc /DAppVersion=x.y.z /DSourceDir=... /DOutputDir=... brainscope.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\brainscope"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
AppId={{A3C2E3B4-7F21-4B0E-9C55-5B7E2D9F1C40}
AppName=brainscope
AppVersion={#AppVersion}
AppVerName=brainscope {#AppVersion}
AppPublisher=Filip Mivalt
AppPublisherURL=https://github.com/xmival00/brainscope
DefaultDirName={autopf}\brainscope
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=brainscope-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\brainscope.exe
LicenseFile=..\..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\brainscope"; Filename: "{app}\brainscope.exe"
Name: "{autodesktop}\brainscope"; Filename: "{app}\brainscope.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\brainscope.exe"; Description: "{cm:LaunchProgram,brainscope}"; Flags: nowait postinstall skipifsilent
