@echo off
:: Change working directory to the project root
cd /d "%~dp0\.."
echo ==============================================
echo PeerStream Windows Build System
echo ==============================================

echo [1/4] Cleaning previous builds...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "release" rmdir /s /q "release"
mkdir release

echo [2/4] Building Windows executable with PyInstaller...
pyinstaller PeerStream.spec --clean
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller build failed!
    pause
    exit /b %errorlevel%
)

echo [3/4] Building Windows Installer with Inno Setup...
set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
)

if defined ISCC (
    "%ISCC%" packaging\PeerStream.iss
    if %errorlevel% neq 0 (
        echo [ERROR] Inno Setup build failed!
        pause
        exit /b %errorlevel%
    )
) else (
    echo [WARNING] Inno Setup ISCC.exe not found. Skipping installer generation.
    echo Make sure Inno Setup 6 is installed.
)

echo [4/4] Generating SHA-256 Checksum...
cd release
for %%F in (*.exe) do (
    certutil -hashfile "%%F" SHA256 > "SHA256SUMS.txt"
)
cd ..

echo ==============================================
echo Build completed successfully!
echo Artifacts are in the 'release' directory.
echo ==============================================
pause
