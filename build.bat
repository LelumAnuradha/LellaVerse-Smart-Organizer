@echo off
title Build LellaVerse Smart Organizer
echo Installing tools...
pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail
echo Building...
pyinstaller --noconfirm --clean --onedir --windowed --name LellaVerseOrganizer --hidden-import pystray._win32 lellaverse_organizer.py
if errorlevel 1 goto fail
powershell -NoProfile -Command "Compress-Archive -Path dist\LellaVerseOrganizer -DestinationPath dist\LellaVerseOrganizer-v1.0.0.zip -Force"
echo.
echo DONE!  App folder: dist\LellaVerseOrganizer
echo        Zip to share: dist\LellaVerseOrganizer-v1.0.0.zip
pause
exit /b 0
:fail
echo.
echo Something went wrong - read the messages above.
pause
exit /b 1
