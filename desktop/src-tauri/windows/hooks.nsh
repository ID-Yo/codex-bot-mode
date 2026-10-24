; Codex Office installer hooks (#244).
; Before files are replaced or removed, ask a running Office service to stop through the same authenticated request the
; controller's Stop button uses. Nothing is force-killed: unless the helper reports success (exit code 0), the installer
; stops and says what to do.

; Resolved here, in the hook file, because __FILEDIR__ inside a macro body expands where the macro is inserted.
!define CODEX_OFFICE_STOP_SCRIPT "${__FILEDIR__}\..\..\installer\stop-previous-office.ps1"
!if /FileExists "${CODEX_OFFICE_STOP_SCRIPT}"
!else
  !error "Installer hook resource not found: ${CODEX_OFFICE_STOP_SCRIPT}"
!endif

!macro CodexOfficeStopService SCRIPT ID
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "${SCRIPT}"'
  Pop $0
  Pop $1
  StrCmp $0 "0" CodexOfficeStopDone_${ID}
  StrCmp $0 "2" 0 CodexOfficeStopUnknown_${ID}
    MessageBox MB_OK|MB_ICONEXCLAMATION "Codex Office could not be stopped safely.$\r$\n$\r$\n$1$\r$\n$\r$\nUse Quit Office in the Codex Office window, then run this again." /SD IDOK
    Abort
  CodexOfficeStopUnknown_${ID}:
    MessageBox MB_OK|MB_ICONEXCLAMATION "Codex Office could not confirm that its service is stopped (result: $0).$\r$\n$\r$\nUse Quit Office in the Codex Office window, then run this again." /SD IDOK
    Abort
  CodexOfficeStopDone_${ID}:
!macroend

!macro NSIS_HOOK_PREINSTALL
  ; The upgrade runs this installer's copy: the installed version may be older than the script.
  InitPluginsDir
  File "/oname=$PLUGINSDIR\stop-previous-office.ps1" "${CODEX_OFFICE_STOP_SCRIPT}"
  !insertmacro CodexOfficeStopService "$PLUGINSDIR\stop-previous-office.ps1" install
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; Keep a copy beside the app so the uninstaller can do the same before removing files.
  File "/oname=$INSTDIR\stop-previous-office.ps1" "${CODEX_OFFICE_STOP_SCRIPT}"
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  IfFileExists "$INSTDIR\stop-previous-office.ps1" 0 CodexOfficeNoStopScript
    !insertmacro CodexOfficeStopService "$INSTDIR\stop-previous-office.ps1" uninstall
  CodexOfficeNoStopScript:
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  Delete "$INSTDIR\stop-previous-office.ps1"
!macroend