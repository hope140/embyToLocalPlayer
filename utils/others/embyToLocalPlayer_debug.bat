@echo OFF
chcp 65001
:BEGIN
cls

set "pythonPath=python"
set "pythonEmbed=%~dp0python_embed\python.exe"
if exist "%pythonEmbed%" (
    echo use python embed. %pythonEmbed%
    set "pythonPath=%pythonEmbed%"
)

for /F "usebackq tokens=*" %%A in (`"%pythonPath%" --version 2^>^&1`) do set "PYTHON_VERSION=%%A"

if "%PYTHON_VERSION:~0,6%" == "Python" (
    echo %PYTHON_VERSION%
    "%pythonPath%" -c "import sys; print(sys.executable)"
    echo press a number
    echo 1: run in console
    echo 2: run in background and add to startup folder
    echo 3: open startup folder
    echo 4: path translate helper
    echo 5: copy script path to clipboard
    echo 6: update current release channel
    choice /N /C:123456 /M "press a number" %1
    if errorlevel 6 goto SIX
    if errorlevel 5 goto FIVE
    if errorlevel 4 goto FOUR
    if errorlevel 3 goto THREE
    if errorlevel 2 goto TWO
    if errorlevel 1 goto ONE
    GOTO END
) else (
    echo ERROR: python not found, reinstall it and add to path!
    GOTO END
)


:SIX
echo you have pressed six
"%pythonPath%" "%~dp0utils\update.py"
GOTO END


:FIVE
echo you have pressed five
echo "%pythonPath%" "%~dp0embyToLocalPlayer.py"
echo already copied, run in cmd, not powershell. paste command is "Ctrl + V"
echo "%pythonPath%" "%~dp0embyToLocalPlayer.py"|clip
GOTO END


:FOUR
echo you have pressed four
"%pythonPath%" "%~dp0utils\conf_helper.py"
GOTO END


:THREE
echo you have pressed three
explorer shell:startup
GOTO END


:TWO
echo you have pressed two
set "startupVbs=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\embyToLocalPlayer.vbs"
set "startupCmd=CreateObject("Wscript.Shell").Run """%pythonPath%"" ""%~dp0embyToLocalPlayer.py""", 0, True"
echo startupCmd=%startupCmd%
echo startupVbs=%startupVbs%
> "%startupVbs%" echo %startupCmd%
echo writing startupCmd to startupVbs, save in startup folder.
timeout /nobreak /t 1 >nul
echo close this window manually
cscript.exe //nologo "%startupVbs%"
GOTO END


:ONE
echo you have pressed one
"%pythonPath%" "%~dp0embyToLocalPlayer.py"
GOTO END


:END
echo all tasks are finished.
pause
