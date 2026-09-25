@echo off
setlocal EnableExtensions

rem Builds a single GUI executable for 64-bit Windows PE.
rem Requires Python 3.12.x (not 3.13+) and PyInstaller on the technician workstation.

set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "DIST_DIR=%PROJECT_ROOT%\dist"
set "BUILD_DIR=%PROJECT_ROOT%\build\pyinstaller"

where py >nul 2>nul || (
    echo ERROR: Python Launcher py.exe was not found.
    exit /b 1
)

echo Building WinPEImageDeployer.exe with Python 3.12...
py -3.12 -m PyInstaller --noconfirm --clean --onefile --console ^
    --name WinPEImageDeployer ^
    --paths "%PROJECT_ROOT%\src" ^
    --distpath "%DIST_DIR%" ^
    --workpath "%BUILD_DIR%" ^
    --specpath "%BUILD_DIR%" ^
    "%PROJECT_ROOT%\scripts\launcher.py"
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    exit /b 1
)

if not exist "%DIST_DIR%\WinPEImageDeployer.exe" (
    echo ERROR: Build completed but the executable was not created.
    exit /b 1
)

echo.
echo SUCCESS: %DIST_DIR%\WinPEImageDeployer.exe
echo You can now run create-winpe-media.bat as Administrator.
exit /b 0