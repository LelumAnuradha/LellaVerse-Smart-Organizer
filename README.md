# LellaVerse Smart Organizer

A small Windows app that keeps your **Downloads** folder (and any other folder) clean automatically.
New files are sorted into folders like *Images*, *Documents* and *Media* - after a virus check, and without ever overwriting anything.

> Windows only. Free and open source (MIT).

## Features

- **Automatic sorting** by file type. You can add your own folders and extensions.
- **Several folders**, each with its **own rules** (for example Downloads and Desktop).
- **Date folders** (optional): by week or by month, e.g. `Images\2026-10-Oct-Week3`.
- **Skips unfinished downloads** (`.crdownload`, `.part`, ...) and waits until a file is complete.
- **Virus scan** of new files with the Windows Defender that is already on your PC. Infected files go to `Infected_Malware`.
- **Duplicate finder**: identical files go to `Duplicates`. Same name but different content? The file gets a unique name like `report_20261017_a3f9c1.pdf`.
- **Undo** any move from the *History / Undo* tab.
- **System tray + start with Windows**: runs quietly in the background with low priority.
- Existing files and sub-folders are sorted too when you press START (or *Sort now*).

## Download

Go to the **[Releases](../../releases)** page and download the latest `.zip`.
Unzip it and run `LellaVerseOrganizer.exe`.

> Windows may show *"Windows protected your PC"* because the app is not code-signed yet.
> Click **More info -> Run anyway**. The full source code is in this repository, so you can check it (or build it yourself).

## Quick start

1. Open the app and press **＋ Add folder**. Choose your Downloads folder.
2. Pick the rules for it (the default set is fine) and whether you want week/month folders.
3. Press **START**. That's it. Close the window and it keeps running in the tray.

## Run from source

```
pip install -r requirements.txt
python lellaverse_organizer.py
```

## Build the .exe yourself

Double-click `build.bat`. The app appears in `dist\LellaVerseOrganizer` and a zip file is created next to it.

## Privacy

Everything stays on your PC. No internet, no tracking. See [PRIVACY.md](PRIVACY.md).

## Good to know

- The app only touches the folders **you** add. It never goes into sub-folders of them (they are moved as a whole into `Folders`).
- Virus scanning is done by Windows Defender, this app is not an antivirus.
- Settings are saved in your user folder: `LellaVerse_Organizer_config.json` and `LellaVerse_Organizer_history.json`.

## Problems or ideas?

Open an **Issue** on this page. Please include what you did and the red error line from the *Activity* tab.

## Author

Made by **Lelum Anuradha Fernando** - [LellaVerse](https://github.com/YOUR-USERNAME).

## License

MIT - see [LICENSE](LICENSE).
