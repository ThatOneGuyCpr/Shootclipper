; Inno Setup script. The release workflow builds it with:
;   ISCC /DMyAppVersion=2026.11.12 packaging\installer.iss
; Installs per user (no admin prompt), so the app can update itself silently.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{6B3E2A1D-5C74-4E8B-9F2A-3D1C7B5E9A42}
AppName=Shoot Clipper
AppVersion={#MyAppVersion}
AppVerName=Shoot Clipper {#MyAppVersion}
AppPublisher=Shoot Clipper
DefaultDirName={localappdata}\Programs\Shoot Clipper
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=ShootClipper-Setup-{#MyAppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\ShootClipper.exe
UninstallDisplayName=Shoot Clipper
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Put a Shoot Clipper shortcut on the desktop"; Flags: checkedonce

[InstallDelete]
; Clear the previous build's libraries so nothing stale is left behind after an update.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\ShootClipper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Shoot Clipper"; Filename: "{app}\ShootClipper.exe"
Name: "{autodesktop}\Shoot Clipper"; Filename: "{app}\ShootClipper.exe"; Tasks: desktopicon

[Run]
; Runs after a normal install (as a ticked box) and after a silent in-app update.
Filename: "{app}\ShootClipper.exe"; Description: "Open Shoot Clipper"; Flags: nowait postinstall

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""ShootClipper Autopilot"" /F"; Flags: runhidden; RunOnceId: "RemoveAutopilot"

[Code]
function InitializeSetup(): Boolean;
begin
  { An in-app update starts this installer and then closes the app; give it a moment. }
  if WizardSilent then
    Sleep(3000);
  Result := True;
end;
