@echo off
setlocal

pushd "%~dp0"
if errorlevel 1 (
	echo [ERROR] Failed to switch to repo root.
	exit /b 1
)

if not exist tools mkdir tools

:: Set local context for portable tools
set "PATH=%~dp0tools\python\tools;%~dp0tools\python\tools\Scripts;%~dp0tools\git\cmd;%PATH%"

call :ensure_supported_python
if errorlevel 1 (
	echo.
	echo [ERROR] Python 3.12 or newer is required to install JD2021 Map Installer.
	popd
	exit /b 1
)

:: Ensure Portable Git is available
git --version >nul 2>&1
if errorlevel 1 (
	echo [INFO] Git is missing. Setting up Portable Git...
	powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $api='https://api.github.com/repos/git-for-windows/git/releases/latest'; $headers=@{'User-Agent'='jd2021-map-installer-setup'}; $release=Invoke-RestMethod -Headers $headers -Uri $api; $asset=$release.assets | Where-Object { $_.name -match '^MinGit-.*-64-bit\.zip$' } | Select-Object -First 1; if (-not $asset) { throw 'Could not find MinGit release.' }; $tmpZip='tools\mingit.zip'; Invoke-WebRequest -Headers $headers -Uri $asset.browser_download_url -OutFile $tmpZip; Expand-Archive -Path $tmpZip -DestinationPath 'tools\git' -Force; Remove-Item -Force $tmpZip"
	if errorlevel 1 (
		echo [ERROR] Failed to install Portable Git.
		popd
		exit /b 1
	)
)

:: Ensure Portable Python is available for supported versions
call :ensure_supported_python
if errorlevel 1 (
	echo [ERROR] Failed to provision a supported Python runtime.
	popd
	exit /b 1
)

echo [1/4] Installing Python dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
	echo.
	echo [ERROR] Python dependency installation failed.
	echo Re-run setup after fixing the issue above.
	popd
	exit /b 1
)

echo.
echo [2/4] Installing Chromium for Playwright...
python -m playwright install chromium
if errorlevel 1 (
	echo.
	echo [ERROR] Playwright Chromium installation failed.
	echo Re-run setup after fixing the issue above.
	popd
	exit /b 1
)

echo.
echo [3/4] Staging AssetStudioModCLI runtime...
call :install_assetstudio_cli

echo.
echo [4/4] Installing vgmstream toolchain...
if not exist tools\vgmstream mkdir tools\vgmstream

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $zip='tools/vgmstream/vgmstream-win64.zip'; $extract='tools/vgmstream/_extract'; if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }; Invoke-WebRequest -Uri 'https://github.com/vgmstream/vgmstream-releases/releases/download/nightly/vgmstream-win64.zip' -OutFile $zip; Expand-Archive -Path $zip -DestinationPath $extract -Force; $bin = Get-ChildItem -Path $extract -Recurse -File | Where-Object { $_.Name -in @('vgmstream-cli.exe','vgmstream.exe') } | Select-Object -First 1; if (-not $bin) { throw 'vgmstream executable not found in archive' }; $runtimeRoot = $bin.Directory.FullName; Get-ChildItem -Path $runtimeRoot -Force | ForEach-Object { Copy-Item -Path $_.FullName -Destination 'tools/vgmstream' -Recurse -Force }; Remove-Item -Recurse -Force $extract; Remove-Item -Force $zip"
if errorlevel 1 (
	echo.
	echo [WARNING] vgmstream auto-install failed.
	echo IPK X360 audio decode may fail until vgmstream is installed in tools\vgmstream.
) else (
	echo [OK] vgmstream installed in tools\vgmstream
)

echo.
echo Setup complete!
timeout /t 5

popd
endlocal

goto :eof

:ensure_git_clone
set "REPO_URL=%~1"
set "DEST_DIR=%~2"
set "REPO_BRANCH=%~3"

if exist "%DEST_DIR%\.git" (
	echo [OK] %DEST_DIR% already present
	exit /b 0
)

if exist "%DEST_DIR%" (
	echo [WARNING] %DEST_DIR% exists but is not a git checkout. Skipping clone.
	exit /b 0
)

echo [INFO] Cloning %REPO_URL% into %DEST_DIR%
git clone --depth 1 --branch %REPO_BRANCH% %REPO_URL% "%DEST_DIR%"
if errorlevel 1 (
	echo [ERROR] Failed to clone %REPO_URL%
	exit /b 1
)

exit /b 0

:install_assetstudio_cli
set "CLI_DIR=tools\AssetStudioModCLI"
set "CLI_EXE=%CLI_DIR%\AssetStudioModCLI.exe"

if exist "%CLI_EXE%" (
	echo [OK] AssetStudioModCLI already present at %CLI_EXE%
	exit /b 0
)

if not exist "%CLI_DIR%" mkdir "%CLI_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $api='https://api.github.com/repos/aelurum/AssetStudio/releases/latest'; $headers=@{'User-Agent'='jd2021-map-installer-setup'}; $release=Invoke-RestMethod -Headers $headers -Uri $api; $asset=$release.assets | Where-Object { $_.name -match 'AssetStudio.*(CLI|cmd|console).*win.*\.(zip|7z)$' } | Select-Object -First 1; if (-not $asset) { $asset=$release.assets | Where-Object { $_.name -match 'AssetStudio.*CLI.*\.(zip|7z)$' } | Select-Object -First 1 }; if (-not $asset) { throw 'Could not find a Windows AssetStudio CLI release asset.' }; $tmpRoot='tools/_assetstudio_tmp'; if (Test-Path $tmpRoot) { Remove-Item -Recurse -Force $tmpRoot }; New-Item -ItemType Directory -Path $tmpRoot | Out-Null; $archive=Join-Path $tmpRoot $asset.name; Invoke-WebRequest -Headers $headers -Uri $asset.browser_download_url -OutFile $archive; if ($archive -like '*.zip') { Expand-Archive -Path $archive -DestinationPath $tmpRoot -Force } elseif ($archive -like '*.7z') { $sevenZip=(Get-Command 7z -ErrorAction SilentlyContinue); if (-not $sevenZip) { throw 'AssetStudio release is .7z, but 7z is not installed.' }; & $sevenZip.Source x $archive ('-o' + $tmpRoot) -y | Out-Null } else { throw 'Unsupported AssetStudio archive format.' }; $cli=(Get-ChildItem -Path $tmpRoot -Recurse -Filter AssetStudioModCLI.exe -File | Select-Object -First 1); if (-not $cli) { throw 'AssetStudioModCLI.exe not found in downloaded archive.' }; $root=$cli.Directory.FullName; $dest='tools/AssetStudioModCLI'; if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }; New-Item -ItemType Directory -Path $dest | Out-Null; Get-ChildItem -Path $root -Force | ForEach-Object { Copy-Item -Path $_.FullName -Destination $dest -Recurse -Force }; Remove-Item -Recurse -Force $tmpRoot"

if errorlevel 1 (
	echo [WARNING] AssetStudioModCLI auto-install failed.
	echo JDNext mapPackage extraction may fail until AssetStudioModCLI is staged in %CLI_DIR%.
	exit /b 0
)

if exist "%CLI_EXE%" (
	echo [OK] AssetStudioModCLI staged at %CLI_EXE%
) else (
	echo [WARNING] AssetStudioModCLI setup completed but executable was not found.
)

exit /b 0

:ensure_supported_python
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 exit /b 0

echo [INFO] Python 3.12+ is required. Setting up Portable Python 3.14.0...
if exist "tools\python" (
	echo [INFO] Replacing unsupported Portable Python in tools\python...
	rmdir /s /q "tools\python"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $tmpZip='tools\python.zip'; Invoke-WebRequest -Uri 'https://www.nuget.org/api/v2/package/python/3.14.0' -OutFile $tmpZip; Expand-Archive -Path $tmpZip -DestinationPath 'tools\python' -Force; Remove-Item -Force $tmpZip"
if errorlevel 1 (
	echo [ERROR] Failed to install Portable Python.
	exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
	echo [ERROR] Python 3.12+ is still unavailable after portable setup.
	exit /b 1
)

exit /b 0