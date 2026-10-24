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

; Reply used when the installer runs silently (/S) and Office is still busy: cancel, so the exit code stays 2.
; A test harness can define CODEX_OFFICE_BUSY_SD as IDRETRY to exercise the Retry loop without a dialog.
!ifndef CODEX_OFFICE_BUSY_SD
  !define CODEX_OFFICE_BUSY_SD IDCANCEL
!endif

!macro CodexOfficeStopService SCRIPT ID
  CodexOfficeStopTry_${ID}:
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "${SCRIPT}" -InstallDir "$INSTDIR"'
  Pop $0
  Pop $1
  StrCmp $0 "0" CodexOfficeStopDone_${ID}
  StrCmp $0 "2" 0 CodexOfficeStopUnknown_${ID}
    ; Office is alive and busy (or its state is unreadable). Nothing is killed: the owner closes Office, then retries.
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Codex Office is still running and could not be stopped safely.$\r$\nClose the Office tabs in your browser or choose Quit from the Codex Office tray icon, then click Retry. If Office is idle and stays busy, end office-service.exe in Task Manager, then click Retry.$\r$\n$\r$\nCodex Office все още работи и не може да бъде спрян безопасно.$\r$\nЗатворете табовете на Office в браузъра или изберете Quit от иконата в трея, после натиснете Retry. Ако Office е неактивен, но остава зает, прекратете office-service.exe от Task Manager и натиснете Retry.$\r$\n$\r$\nDetails: $1" /SD ${CODEX_OFFICE_BUSY_SD} IDRETRY CodexOfficeStopTry_${ID}
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
