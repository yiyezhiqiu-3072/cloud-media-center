@echo off
setlocal enabledelayedexpansion
chcp 936 >nul
title 云媒中心 · nginx 自检
color 0F

rem ======================================================================
rem  登录不了 / 页面打不开时，先双击这个脚本
rem  它会把最关键的几项一次性查出来：配置是否合法、账号文件是否存在、
rem  统一登录服务与两个后端有没有在监听、nginx 最近的错误日志
rem  不需要安装 python，只用系统自带功能
rem ======================================================================

rem nginx 不在 C:\nginx 时，去掉下面这行的 rem 并改成实际路径
rem set "NGINX_HOME=D:\nginx"

call :find_nginx
if not defined NGINX_HOME (
    echo [失败] 没找到 nginx.exe
    echo        请把脚本放到 nginx 目录下，或在本文件开头设置 NGINX_HOME
    goto :end
)
cd /d "%NGINX_HOME%"

echo =========================================================
echo   nginx 目录：%NGINX_HOME%
echo =========================================================
echo.

rem ---------- 1. 版本 ----------
echo [1/6] nginx 版本
"%NGINX_HOME%\nginx.exe" -v 2>&1
echo.

rem ---------- 2. 配置检查 ----------
echo [2/6] 配置语法
"%NGINX_HOME%\nginx.exe" -t 2>&1
echo.

rem ---------- 3. 账号文件（现在由 CloudAuth 读取，不再是 nginx auth_basic） ----------
echo [3/6] 登录账号文件
set "HPFILE=C:\nginx\conf\htpasswd"
echo   默认路径：!HPFILE!
echo   实际以 CloudAuth 目录下 config.json 里的 htpasswd 为准
if not exist "!HPFILE!" (
    echo   [问题] 这个文件不存在，登录会一直失败
    echo          解决：双击 make-htpasswd.bat 生成，或改 config.json 里的路径
    goto :after_hp
)
for %%F in ("!HPFILE!") do set "HPSIZE=%%~zF"
echo   大小：!HPSIZE! 字节
if "!HPSIZE!"=="0" (
    echo   [问题] 文件是空的，登录会一直失败
    goto :after_hp
)
set "HPN=0"
for /f "usebackq delims=" %%L in ("!HPFILE!") do set /a HPN+=1
echo   账号：!HPN! 行
set "FIRST="
for /f "usebackq delims=" %%L in ("!HPFILE!") do if not defined FIRST set "FIRST=%%L"
if defined FIRST (
    for /f "tokens=1 delims=:" %%U in ("!FIRST!") do echo   首个用户：%%U
    echo !FIRST! | findstr /c:"{SHA}" >nul
    if errorlevel 1 echo   [提示] 首行不是 {SHA} 格式，登录会一直失败，建议重新生成
)
:after_hp
echo.

rem ---------- 3.5 stub_status 模块 ----------
echo [3.5] 状态监控所需的 stub_status 模块
"%NGINX_HOME%\nginx.exe" -V 2>&1 | findstr /c:"stub_status" >nul
if errorlevel 1 (
    echo   [警告] 编译参数里没有 stub_status，/status 页面拿不到 nginx 指标
    echo          官网 Windows 版默认带这个模块；如果确实没有，把 nginx.conf 里
    echo          location = /nginx_status 那几行去掉即可，页面仍会显示后端健康状态
) else (
    echo   [正常] 已包含 stub_status，/status 页面可用
)
echo.

rem ---------- 4. 后端端口 ----------
echo [4/6] 后端服务是否在监听
call :port 8090 云收音机
call :port 8091 云电视
call :port 8092 统一登录 CloudAuth
call :port 8093 云音乐
call :port 8094 工作日志
echo   8092 没起会登录不了；8090/8091 没起访问 /radio/ 或 /tv/ 会报 502
echo.

rem ---------- 5. 错误日志 ----------
echo [5/6] nginx 最近 12 条错误日志
set "LOG=%NGINX_HOME%\logs\error.log"
if not exist "%LOG%" (
    echo   没有找到 %LOG%
    goto :tips
)
echo   --------------------------------------------
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 12 -Encoding UTF8" 2>nul
echo   --------------------------------------------
echo.
echo   重点看这几类字样：
echo     connect() failed ... 8090/8091        =  后端没启动，会 502
echo     connect() failed ... 8092             =  统一登录服务没启动，登录页报错
echo     Permission denied / Access denied     =  权限问题，用管理员身份重跑
echo     rewrite or internal redirection       =  配置里有循环跳转

:tips
echo.
echo =========================================================
echo   对照结论：
echo     登录页报错  ^-^> 看第 4 项，8092 统一登录服务
echo     密码总不对  ^-^> 看第 3 项，账号文件与 {SHA} 格式
echo     /radio/ 502 ^-^> 收音机没启动或端口不是 8090
echo     /tv/    502 ^-^> 电视没启动或端口不是 8091
echo =========================================================
goto :end

rem ==================== 子过程 ====================

:port
set "PN=%~2"
netstat -ano | findstr ":%1 " | findstr "LISTENING" >nul
if errorlevel 1 (
    echo   [未监听] %PN%  %1
) else (
    echo   [已监听] %PN%  %1
)
goto :eof

:find_nginx
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
