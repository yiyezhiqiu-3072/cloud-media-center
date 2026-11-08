@echo off
setlocal enabledelayedexpansion
chcp 936 >nul
title 云媒中心 · 重启服务

rem ======================================================================
rem  用法：
rem    双击                    检查配置并热重载 nginx（不断开现有访问）
rem    restart-nginx.bat all   再重启统一登录 / 云收音机 / 云电视
rem    restart-nginx.bat stop  停止 nginx 和三个后端
rem
rem  下面四行按你的实际情况改；留空表示自动查找 / 跳过：
rem ======================================================================
set "NGINX_HOME="
set "RADIO_DIR="
set "TV_DIR="
set "AUTH_DIR="
rem ======================================================================

set "MODE=%~1"
if /i "%MODE%"=="stop" goto :stop_all

call :find_nginx
if not defined NGINX_EXE (
    echo.
    echo [失败] 没找到 nginx.exe
    echo.
    echo 请把 nginx 安装路径填到本文件开头的 NGINX_HOME，例如：
    echo     set "NGINX_HOME=D:\nginx"
    goto :end
)

cd /d "%NGINX_HOME%"

echo =========================================
echo    nginx 目录：%NGINX_HOME%
echo =========================================
echo.
echo [1/3] 检查配置 ...
"%NGINX_EXE%" -t
if errorlevel 1 (
    echo.
    echo [失败] 配置有语法错误，未执行重载。
    echo        按上面提示的行号改完，再运行一次本脚本。
    call :show_error_log
    goto :end
)

echo.
echo [2/3] 重载 nginx ...
tasklist /fi "imagename eq nginx.exe" 2>nul | find /i "nginx.exe" >nul
if errorlevel 1 (
    echo    nginx 当前没在运行，正在启动 ...
    start "" /min "%NGINX_EXE%"
    timeout /t 2 >nul
) else (
    echo    正在热重载，不会断开现有连接 ...
    "%NGINX_EXE%" -s reload
    if errorlevel 1 (
        echo.
        echo [失败] 重载失败。常见原因：nginx.pid 丢失，或旧进程卡住。
        echo        可以先执行 restart-nginx.bat stop，再重新运行本脚本。
        call :show_error_log
        goto :end
    )
)

echo.
echo [3/3] 后端服务 ...
if /i "%MODE%"=="all" (
    call :restart_app "%AUTH_DIR%" "CloudAuth.exe" "统一登录"
    call :restart_app "%RADIO_DIR%" "CloudRadio.exe" "云收音机"
    call :restart_app "%TV_DIR%" "CloudTV.exe" "云电视"
) else (
    echo    跳过。只改了 nginx 的话不需要重启后端。
    echo    要用例：restart-nginx.bat all
)

echo.
echo -----------------------------------------
echo   完成。浏览器打开 http://127.0.0.1/ 验证
echo   首页应直接显示，点卡片才会弹出登录页
echo   页面没变化？Ctrl+F5 强刷一下
echo -----------------------------------------
goto :end


rem ==================== 子过程 ====================

:find_nginx
set "NGINX_EXE="
if defined NGINX_HOME (
    if exist "%NGINX_HOME%\nginx.exe" set "NGINX_EXE=%NGINX_HOME%\nginx.exe"
    goto :eof
)
if exist "%~dp0nginx.exe" (
    set "NGINX_HOME=%~dp0"
    set "NGINX_EXE=%~dp0nginx.exe"
    goto :eof
)
for %%D in (C D E F) do (
    if exist "%%D:\nginx\nginx.exe" (
        set "NGINX_HOME=%%D:\nginx"
        set "NGINX_EXE=%%D:\nginx\nginx.exe"
        goto :eof
    )
)
goto :eof

:restart_app
set "APPDIR=%~1"
set "APPEXE=%~2"
set "APPNAME=%~3"
if not defined APPDIR (
    echo    - %APPNAME%：未配置路径，跳过
    goto :eof
)
if not exist "%APPDIR%\%APPEXE%" (
    echo    - %APPNAME%：没找到 %APPDIR%\%APPEXE%，跳过
    goto :eof
)
echo    - %APPNAME%：正在重启 ...
taskkill /f /im "%APPEXE%" >nul 2>&1
timeout /t 1 >nul
start "" /d "%APPDIR%" "%APPDIR%\%APPEXE%"
timeout /t 2 >nul
goto :eof

:show_error_log
set "LOG=%NGINX_HOME%\logs\error.log"
if not exist "%LOG%" goto :eof
echo.
echo 最近的错误日志（最后 6 行）：
set "SKIP=0"
for /f %%C in ('find /c /v "" ^< "%LOG%"') do set /a SKIP=%%C-6
if %SKIP% lss 0 set "SKIP=0"
more +%SKIP% "%LOG%"
goto :eof

:stop_all
call :find_nginx
echo 正在停止服务 ...
if defined NGINX_EXE (
    cd /d "%NGINX_HOME%"
    "%NGINX_EXE%" -s quit >nul 2>&1
    timeout /t 1 >nul
    taskkill /f /im nginx.exe >nul 2>&1
    echo    - nginx 已停止
) else (
    echo    - nginx：没找到，跳过
)
taskkill /f /im "CloudAuth.exe" >nul 2>&1 && echo    - 统一登录 已停止
taskkill /f /im "CloudRadio.exe" >nul 2>&1 && echo    - 云收音机 已停止
taskkill /f /im "CloudTV.exe" >nul 2>&1 && echo    - 云电视 已停止
echo 全部停止。
goto :end

:end
echo.
pause
endlocal
