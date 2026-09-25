@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Creates a customized WinPE image containing WinPEImageDeployer.exe.
rem Run from an elevated "Deployment and Imaging Tools Environment" command prompt.
rem Usage: create-winpe-media.bat [amd64^|arm64] [working-directory] [/UFD drive-letter^|/ISO iso-path]

set "ARCH=%~1"
if "%ARCH%"=="" set "ARCH=amd64"
set "WORK_DIR=%~2"
if "%WORK_DIR%"=="" set "WORK_DIR=C:\WinPE-Image-Deployer-%ARCH%"
set "MEDIA_MODE=%~3"
set "MEDIA_TARGET=%~4"

if /I not "%ARCH%"=="amd64" if /I not "%ARCH%"=="arm64" (
    echo ERROR: Architecture must be amd64 or arm64.
    exit /b 1
)

net session >nul 2>nul
if errorlevel 1 (
    echo ERROR: Run this script as Administrator.
    exit /b 1
)

where copype.cmd >nul 2>nul
if errorlevel 1 (
    echo ERROR: copype.cmd is not on PATH.
    echo Start "Deployment and Imaging Tools Environment" as Administrator.
    exit /b 1
)

where MakeWinPEMedia.cmd >nul 2>nul
if errorlevel 1 (
    echo ERROR: MakeWinPEMedia.cmd is not on PATH.
    echo Install Windows ADK Deployment Tools and the matching Windows PE add-on.
    exit /b 1
)

set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "APP_EXE=%PROJECT_ROOT%\dist\WinPEImageDeployer.exe"
rem Keep this folder name aligned with MEDIA_CONTENT_DIR in media_content.py.
set "MEDIA_CONTENT_DIR=winpe-media-content"
set "RUNTIME_CONTENT_SOURCE=%PROJECT_ROOT%\%MEDIA_CONTENT_DIR%"
if not exist "%APP_EXE%" (
    echo ERROR: Application executable was not found:
    echo %APP_EXE%
    echo Run "%PROJECT_ROOT%\scripts\build-exe.bat" first.
    exit /b 1
)
if not exist "%RUNTIME_CONTENT_SOURCE%\" (
    echo ERROR: Runtime content folder was not found:
    echo %RUNTIME_CONTENT_SOURCE%
    exit /b 1
)

for %%I in ("%APP_EXE%") do echo Embedding application built: %%~tI  (%%~zI bytes)
for /f "tokens=*" %%H in ('certutil -hashfile "%APP_EXE%" SHA256 ^| findstr /R /V "hash CertUtil"') do (
    set "APP_SHA256=%%H"
    goto :app_hash_ready
)
:app_hash_ready
if defined APP_SHA256 echo EXE SHA256: %APP_SHA256%

if exist "%WORK_DIR%" (
    echo ERROR: Working directory already exists:
    echo %WORK_DIR%
    echo Delete it only after confirming it contains no required files.
    exit /b 1
)

echo.
echo Creating base WinPE files in "%WORK_DIR%"...
call copype.cmd %ARCH% "%WORK_DIR%"
if errorlevel 1 exit /b 1

set "MOUNT_DIR=%WORK_DIR%\mount"
if not exist "%MOUNT_DIR%" (
    md "%MOUNT_DIR%"
    if errorlevel 1 (
        echo ERROR: Could not create mount directory: %MOUNT_DIR%
        exit /b 1
    )
)

for /f %%I in ('dir /a /b "%MOUNT_DIR%" 2^>nul ^| find /c /v ""') do set "MOUNT_ITEM_COUNT=%%I"
if not "!MOUNT_ITEM_COUNT!"=="0" (
    echo ERROR: Mount directory is not empty: %MOUNT_DIR%
    echo Run Dism /Get-MountedImageInfo and resolve any stale DISM mount before retrying.
    exit /b 1
)

echo Mounting boot.wim...
Dism /Mount-Image /ImageFile:"%WORK_DIR%\media\sources\boot.wim" /Index:1 /MountDir:"%MOUNT_DIR%"
if errorlevel 1 goto :cleanup_discard

echo Copying WinPEImageDeployer.exe into boot.wim...
copy /Y "%APP_EXE%" "%MOUNT_DIR%\Windows\System32\WinPEImageDeployer.exe" >nul
if errorlevel 1 goto :cleanup_discard
for %%I in ("%MOUNT_DIR%\Windows\System32\WinPEImageDeployer.exe") do echo Embedded EXE: %%~tI  (%%~zI bytes)

echo Configuring automatic application startup...
>"%MOUNT_DIR%\Windows\System32\startnet.cmd" (
    echo @echo off
    echo wpeinit
    echo X:\Windows\System32\WinPEImageDeployer.exe --startup-preflight
    echo cmd.exe
)
if errorlevel 1 goto :cleanup_discard

echo Committing boot.wim changes...
Dism /Unmount-Image /MountDir:"%MOUNT_DIR%" /Commit
if errorlevel 1 exit /b 1

echo Copying WinPEImageDeployer runtime content into WinPE media...
robocopy "%RUNTIME_CONTENT_SOURCE%" "%WORK_DIR%\media\%MEDIA_CONTENT_DIR%" /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /XF startup-config.ini.example /XD .git
set "ROBOCOPY_EXIT=%ERRORLEVEL%"
if %ROBOCOPY_EXIT% GEQ 8 (
    echo ERROR: Could not copy runtime content into WinPE media. Robocopy exit code: %ROBOCOPY_EXIT%
    exit /b 1
)
echo Runtime content copied from: %RUNTIME_CONTENT_SOURCE%

echo.
echo SUCCESS: Customized WinPE files were created in:
echo %WORK_DIR%\media

if "%MEDIA_MODE%"=="" (
    echo.
    echo To create a USB drive, run:
    echo MakeWinPEMedia /UFD "%WORK_DIR%" E:
    echo.
    echo To create an ISO, run:
    echo MakeWinPEMedia /ISO "%WORK_DIR%" C:\WinPEImageDeployer.iso
    exit /b 0
)

if /I "%MEDIA_MODE%"=="/UFD" goto :make_media
if /I "%MEDIA_MODE%"=="/ISO" goto :make_media
echo ERROR: Media mode must be /UFD or /ISO.
exit /b 1

:make_media
if "%MEDIA_TARGET%"=="" (
    echo ERROR: Provide a USB drive letter for /UFD or an ISO output path for /ISO.
    exit /b 1
)
echo.
echo WARNING: MakeWinPEMedia /UFD formats the selected USB drive.
call MakeWinPEMedia %MEDIA_MODE% "%WORK_DIR%" "%MEDIA_TARGET%"
if errorlevel 1 exit /b %errorlevel%
if /I "%MEDIA_MODE%"=="/UFD" (
    echo.
    echo Copied runtime content from the repository folder to boot media:
    echo %MEDIA_TARGET%\%MEDIA_CONTENT_DIR%\Drivers
    echo %MEDIA_TARGET%\%MEDIA_CONTENT_DIR%\startup-config.ini
    echo This works from /UFD media and from an ISO written to USB by Rufus.
    echo Add drivers and create or edit startup-config.ini in %RUNTIME_CONTENT_SOURCE% before creating media.
)
exit /b 0

:cleanup_discard
echo.
echo ERROR: Customization failed. Discarding mounted boot.wim changes...
Dism /Unmount-Image /MountDir:"%MOUNT_DIR%" /Discard >nul 2>nul
exit /b 1