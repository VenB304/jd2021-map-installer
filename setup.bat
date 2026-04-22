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

:: Ensure Portable Git is available
git --version >nul 2>&1
if errorlevel 1 (
	echo [INFO] Git is missing. Setting up Portable Git...
	powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $api='https://api.github.com/repos/git-for-windows/git/releases/latest'; $headers=@{'User-Agent'='jd2021-map-installer-setup'}; $release=Invoke-RestMethod -Headers $headers -Uri $api; $asset=$release.assets | Where-Object { $_.name -match '^MinGit-.*-64-bit\.zip$' } | Select-Object -First 1; if (-not $asset) { throw 'Could not find MinGit release.' }; $tmpZip='tools\mingit.zip'; Invoke-WebRequest -Headers $headers -Uri $asset.browser_download_url -OutFile $tmpZip; Expand-Archive -Path $tmpZip -DestinationPath 'tools\git' -Force; Remove-Item -Force $tmpZip"
	if errorlevel 1 (
		echo [ERROR] Failed to install Portable Git.
		popd
		exit /b 1
	)
)

:: Ensure Portable Python is available
python -c "import sys" >nul 2>&1
if errorlevel 1 (
	echo [INFO] Python is missing. Setting up Portable Python...
	powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $tmpZip='tools\python.zip'; Invoke-WebRequest -Uri 'https://www.nuget.org/api/v2/package/python/3.11.9' -OutFile $tmpZip; Expand-Archive -Path $tmpZip -DestinationPath 'tools\python' -Force; Remove-Item -Force $tmpZip"
	if errorlevel 1 (
		echo [ERROR] Failed to install Portable Python.
		popd
		exit /b 1
	)
)

echo [1/5] Installing Python dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
	echo.
	echo [ERROR] Python dependency installation failed.
	echo Re-run setup after fixing the issue above.
	popd
	exit /b 1
)

echo.
echo [2/5] Installing Chromium for Playwright...
python -m playwright install chromium
if errorlevel 1 (
	echo.
	echo [ERROR] Playwright Chromium installation failed.
	echo Re-run setup after fixing the issue above.
	popd
	exit /b 1
)

echo.
echo [3/5] Staging AssetStudioModCLI runtime...
call :install_assetstudio_cli

echo.
echo [4/5] Installing vgmstream toolchain...
if not exist tools\vgmstream mkdir tools\vgmstream

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $zip='tools/vgmstream/vgmstream-win64.zip'; $extract='tools/vgmstream/_extract'; if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }; Invoke-WebRequest -Uri 'https://github.com/vgmstream/vgmstream-releases/releases/download/nightly/vgmstream-win64.zip' -OutFile $zip; Expand-Archive -Path $zip -DestinationPath $extract -Force; $bin = Get-ChildItem -Path $extract -Recurse -File | Where-Object { $_.Name -in @('vgmstream-cli.exe','vgmstream.exe') } | Select-Object -First 1; if (-not $bin) { throw 'vgmstream executable not found in archive' }; $runtimeRoot = $bin.Directory.FullName; Get-ChildItem -Path $runtimeRoot -Force | ForEach-Object { Copy-Item -Path $_.FullName -Destination 'tools/vgmstream' -Recurse -Force }; Remove-Item -Recurse -Force $extract; Remove-Item -Force $zip"
if errorlevel 1 (
	echo.
	echo [WARNING] vgmstream auto-install failed.
	echo IPK X360 audio decode may fail until vgmstream is installed in tools\vgmstream.
) else (
	echo [OK] vgmstream installed in tools\vgmstream
)

echo.
echo [5/5] Installing FFmpeg toolchain...
if not exist tools\ffmpeg\bin mkdir tools\ffmpeg\bin

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; $zip='tools/ffmpeg/ffmpeg-release-essentials.zip'; $tmp='tools/ffmpeg/_tmp'; if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }; Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zip; Expand-Archive -Path $zip -DestinationPath $tmp -Force; $ff = Get-ChildItem -Path $tmp -Recurse -Filter 'ffmpeg.exe' -File | Select-Object -First 1; if (-not $ff) { throw 'ffmpeg.exe not found in archive' }; $fp = Get-ChildItem -Path $ff.Directory.FullName -Filter 'ffprobe.exe' -File | Select-Object -First 1; if (-not $fp) { throw 'ffprobe.exe not found in archive' }; Copy-Item -Path $ff.FullName -Destination 'tools/ffmpeg/bin/ffmpeg.exe' -Force; Copy-Item -Path $fp.FullName -Destination 'tools/ffmpeg/bin/ffprobe.exe' -Force; Remove-Item -Recurse -Force $tmp; Remove-Item -Force $zip"
if errorlevel 1 (
	echo.
	echo [WARNING] FFmpeg auto-install failed.
	echo Video encoding may fail until ffmpeg is available in PATH or tools\ffmpeg\bin.
) else (
	echo [OK] FFmpeg installed in tools\ffmpeg\bin
)

echo.
echo Generating run.bat...
(
echo @echo off
echo setlocal
echo pushd "%%~dp0"
echo set "PATH=%%~dp0tools\ffmpeg\bin;%%~dp0tools\python\tools;%%~dp0tools\python\tools\Scripts;%%~dp0tools\git\cmd;%%PATH%%"
echo python -m jd2021_installer.main
echo popd
echo endlocal
) > run.bat
echo [OK] run.bat generated.

echo.
echo Setup complete!
timeout /t 5

popd
endlocal

goto :eof

:install_assetstudio_cli
set "CLI_DIR=tools\AssetStudioModCLI"
set "CLI_EXE=%CLI_DIR%\AssetStudioModCLI.exe"

if exist "%CLI_EXE%" (
	echo [OK] AssetStudioModCLI already present at %CLI_EXE%
	exit /b 0
)

if not exist "%CLI_DIR%" mkdir "%CLI_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $api='https://api.github.com/repos/aelurum/AssetStudio/releases/latest'; $headers=@{'User-Agent'='jd2021-map-installer-setup'}; $release=Invoke-RestMethod -Headers $headers -Uri $api; $asset=$release.assets | Where-Object { $_.name -match 'AssetStudio.*(CLI|cmd|console).*win.*\.(zip|7z)$' } | Select-Object -First 1; if (-not $asset) { $asset=$release.assets | Where-Object { $_.name -match 'AssetStudio.*CLI.*\.(zip|7z)$' } | Select-Object -First 1 }; if (-not $asset) { throw 'Could not find a Windows AssetStudio CLI release asset.' }; $tmpRoot='tools/AssetStudioModCLI/_tmp'; if (Test-Path $tmpRoot) { Remove-Item -Recurse -Force $tmpRoot }; New-Item -ItemType Directory -Path $tmpRoot | Out-Null; $archive=Join-Path $tmpRoot $asset.name; Invoke-WebRequest -Headers $headers -Uri $asset.browser_download_url -OutFile $archive; if ($archive -like '*.zip') { Expand-Archive -Path $archive -DestinationPath $tmpRoot -Force } elseif ($archive -like '*.7z') { $sevenZip=(Get-Command 7z -ErrorAction SilentlyContinue); if (-not $sevenZip) { throw 'AssetStudio release is .7z, but 7z is not installed.' }; & $sevenZip.Source x $archive ('-o' + $tmpRoot) -y | Out-Null } else { throw 'Unsupported AssetStudio archive format.' }; $cli=(Get-ChildItem -Path $tmpRoot -Recurse -Filter AssetStudioModCLI.exe -File | Select-Object -First 1); if (-not $cli) { throw 'AssetStudioModCLI.exe not found in downloaded archive.' }; $root=$cli.Directory.FullName; Get-ChildItem -Path $root -Force | ForEach-Object { Copy-Item -Path $_.FullName -Destination 'tools/AssetStudioModCLI' -Recurse -Force }; Remove-Item -Recurse -Force $tmpRoot"

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