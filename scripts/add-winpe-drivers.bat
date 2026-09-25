@echo off
setlocal EnableExtensions

rem Adds storage and NIC drivers to a customized WinPE working directory.
rem Usage: add-winpe-drivers.bat C:\WinPE-Image-Deployer-amd64 C:\Drivers

set "WORK_DIR=%~1"
set "DRIVER_DIR=%~2"
if "%WORK_DIR%"=="" goto :usage
if "%DRIVER_DIR%"=="" goto :usage

net session >nul 2>nul
if errorlevel 1 (
    echo ERROR: Run this script as Administrator.
    exit /b 1
)

if not exist "%WORK_DIR%\media\sources\boot.wim" (
    echo ERROR: boot.wim was not found in %WORK_DIR%\media\sources
    exit /b 1
)
if not exist "%DRIVER_DIR%" (
    echo ERROR: Driver directory was not found: %DRIVER_DIR%
    exit /b 1
)

set "MOUNT_DIR=%WORK_DIR%\mount-drivers"
if exist "%MOUNT_DIR%" (
    echo ERROR: Mount directory already exists: %MOUNT_DIR%
    echo Verify no other DISM operation is using it, then remove it.
    exit /b 1
)
md "%MOUNT_DIR%" || exit /b 1

Dism /Mount-Image /ImageFile:"%WORK_DIR%\media\sources\boot.wim" /Index:1 /MountDir:"%MOUNT_DIR%"
if errorlevel 1 goto :cleanup_discard

echo Adding all INF drivers from %DRIVER_DIR%...
Dism /Image:"%MOUNT_DIR%" /Add-Driver /Driver:"%DRIVER_DIR%" /Recurse
if errorlevel 1 goto :cleanup_discard

Dism /Unmount-Image /MountDir:"%MOUNT_DIR%" /Commit
if errorlevel 1 exit /b 1

rd "%MOUNT_DIR%" 2>nul
echo SUCCESS: Drivers were added to boot.wim.
exit /b 0

:cleanup_discard
echo ERROR: Driver injection failed. Discarding the mounted image...
Dism /Unmount-Image /MountDir:"%MOUNT_DIR%" /Discard >nul 2>nul
exit /b 1

:usage
echo Usage: %~nx0 ^<WinPE-working-directory^> ^<driver-directory^>
echo Example: %~nx0 C:\WinPE-Image-Deployer-amd64 C:\Drivers
exit /b 1