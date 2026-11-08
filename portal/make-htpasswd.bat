@echo off
setlocal
chcp 936 >nul
title 云媒中心 · 生成登录账号（htpasswd）
color 0F

rem ======================================================================
rem  作用：生成 / 修改 nginx 的登录账号文件 conf\htpasswd
rem        不需要安装 Python，只用 Windows 自带的 PowerShell
rem
rem  用法：双击运行，按提示输入用户名和密码
rem        密码直接回车 = 自动生成 12 位随机密码
rem
rem  同一个用户名重复运行 = 改密码；换个用户名 = 加账号
rem  改完执行 restart-nginx.bat，让 nginx 重新读取账号
rem ======================================================================

rem nginx 不在 C:\nginx 时，去掉下面这行的 rem 并改成实际路径
rem set "NGINX_HOME=D:\nginx"

call :find_nginx
if not defined NGINX_HOME goto :ask_path

:have_home
set "HP_FILE=%NGINX_HOME%\conf\htpasswd"
if not exist "%NGINX_HOME%\conf" (
    echo.
    echo [失败] 没找到目录：%NGINX_HOME%\conf
    echo        请确认 %NGINX_HOME% 是 nginx 安装目录（里面有 nginx.exe 和 conf 文件夹）
    goto :end
)
if not exist "%~dp0make-htpasswd.ps1" (
    echo.
    echo [失败] 缺少 make-htpasswd.ps1
    echo        这个文件必须和本文件放在同一个目录里
    goto :end
)

echo ==========================================
echo   账号文件：%HP_FILE%
echo ==========================================
echo.

set "HP_USER="
set /p "HP_USER=用户名（直接回车 = admin）: "
if not defined HP_USER set "HP_USER=admin"

set "HP_PWD="
set /p "HP_PWD=密  码（直接回车 = 自动生成）: "
if not defined HP_PWD echo.& echo   未输入密码，将生成随机密码 ...

set "HP_RESULT=%TEMP%\cloudmedia_htpasswd.txt"
if exist "%HP_RESULT%" del /q "%HP_RESULT%" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make-htpasswd.ps1" >nul 2>&1

set "RES_LINE="
if exist "%HP_RESULT%" for /f "usebackq delims=" %%L in ("%HP_RESULT%") do if not defined RES_LINE set "RES_LINE=%%L"
if exist "%HP_RESULT%" del /q "%HP_RESULT%" >nul 2>&1

if not defined RES_LINE (
    echo.
    echo [失败] 生成失败，PowerShell 没有返回结果。
    echo        请用管理员身份打开命令提示符，进入本目录后手动执行：
    echo            powershell -ExecutionPolicy Bypass -File make-htpasswd.ps1
    echo        看它报什么错。
    goto :end
)

for /f "tokens=1-4 delims=|" %%a in ("%RES_LINE%") do (
    set "R_KIND=%%a"
    set "R_USER=%%b"
    set "R_PWD=%%c"
    set "R_COUNT=%%d"
)

if /i not "%R_KIND%"=="OK" (
    echo.
    echo [失败] %R_USER%
    echo.
    echo        常见原因：conf 目录没有写权限，用管理员身份重新运行本文件
    goto :end
)

echo.
echo ------------------------------------------
echo   已写入 %HP_FILE%
echo.
echo   用户名：%R_USER%
echo   密  码：%R_PWD%
echo   账号数：%R_COUNT% 个
echo ------------------------------------------
echo.
echo   下一步：运行 restart-nginx.bat 让 nginx 读取新账号
echo   然后打开 http://127.0.0.1/ ，浏览器会弹登录框
echo   密码自己记一份，这个窗口关掉就看不到了
echo.
goto :end

rem ==================== 子过程 ====================

:ask_path
echo.
echo [提示] 没自动找到 nginx.exe
echo.
set "NGINX_HOME="
set /p "NGINX_HOME=请输入 nginx 安装目录（例如 C:\nginx）: "
if not defined NGINX_HOME goto :end
if not exist "%NGINX_HOME%\nginx.exe" (
    echo.
    echo [失败] %NGINX_HOME% 下没有 nginx.exe，请检查路径
    goto :end
)
goto :have_home

:find_nginx
rem 已经手工指定且有效，直接用
if defined NGINX_HOME if exist "%NGINX_HOME%\nginx.exe" goto :trim
set "NGINX_HOME="
if exist "%~dp0nginx.exe" (
    set "NGINX_HOME=%~dp0"
    goto :trim
)
if exist "%~dp0..\nginx.exe" (
    for %%P in ("%~dp0..") do set "NGINX_HOME=%%~fP"
    goto :trim
)
for %%D in (C D E F G) do (
    if exist "%%D:\nginx\nginx.exe" (
        set "NGINX_HOME=%%D:\nginx"
        goto :trim
    )
)
goto :eof

:trim
if "%NGINX_HOME:~-1%"=="\" set "NGINX_HOME=%NGINX_HOME:~0,-1%"
goto :eof

:end
echo.
pause
endlocal
