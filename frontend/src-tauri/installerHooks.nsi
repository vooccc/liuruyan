; ============================================================
;   柳如烟 NSIS 安装/卸载自定义扩展钩子 (installerHooks.nsi)
; ============================================================

!macro NSIS_HOOK_PREUNINSTALL
  ; 卸载前确保后台 Python 进程已关闭，防止文件占用
  !insertmacro CheckIfAppIsRunning "liuruyan-backend.exe" "柳如烟后端服务"
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; 用户卸载时若勾选了“删除用户数据”（DeleteAppDataCheckboxState = 1）
  ; 并且不是平滑升级覆盖安装模式（UpdateMode <> 1）
  ; 彻底清理后端用户数据目录（包括角色、会话、记忆等）
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    SetShellVarContext current
    RMDir /r "$PROFILE\.liuruyan"
  ${EndIf}
!macroend
