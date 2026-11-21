# ISO Workshop

A compact, dark-themed Windows ISO utility built with Python and PySide6.

Repository: [https://github.com/EgeGurkan/ISOWorkshop](https://github.com/EgeGurkan/ISOWorkshop)

## Requirements

- Windows OS with Python and dependencies

`py -m pip install --upgrade PySide6 pycdlib pyinstaller`

- Optional: full 7-Zip command-line tools (`7z.exe` **and** `7z.dll` in the same folder). These are used as a fallback for ISO/UDF images that `pycdlib` cannot open. No extra 7-Zip downloads are needed if both files are present.

Install the Python dependencies in a PowerShell window opened in the project directory:

```
py iso_workshop.py
```

## Interface

The window opens on a small home screen with **Pack ISO** and **Unpack ISO**. It resizes when a page is selected, with an additional height adjustment when **Boot ISO image** is chosen. There is no scroll area. The same top-right button displays **?** on the home page and **←** on Pack / Unpack pages.

Click **?** for the Byte Wrapper-style information popup. It contains a link to this GitHub repository and presence indicators for `7z.exe` and its accompanying `7z.dll`. **Green** indicates a file was found; **red** indicates it was not. The DLL check requires a matching executable in the *same* folder. Click a component name to open the official 7-Zip download page. The popup checks file presence quickly; when fallback extraction is actually needed, the app separately runs `7z i` to validate ISO/UDF support.

The popup does not download or install 7-Zip automatically.

## Pack ISO

Select a source folder and an output `.iso` file. The **Label** field controls the volume label.

| Setting | Available options |
| --- | --- |
| Type | Normal ISO image; Boot ISO image |
| File System | UDF 2.60 + Joliet (default); UDF 2.60; ISO 9660 + Joliet; ISO 9660 |
| ISO Level | Level 3 (default); Level 2; Level 1 (8.3) |
| Source | Directory to include in the ISO |
| Target | Destination `.iso` file |
| Label | Up to 32 characters: A-Z, 0-9, spaces, underscores |
| Create GZip copy | Optional and **off by default** |

When ISO 9660 or ISO 9660 + Joliet is selected, the application checks the source tree. If a path reaches seven nested directory levels, the selection changes to the recommended **UDF 2.60 + Joliet**. This check happens when choosing or editing Source, when changing File System, and immediately before packing.

ISO 9660 compatibility has a path-component limit in the underlying ISO writer. For UDF and hybrid modes, the original deeper paths are retained in their extended directory namespaces while paths beyond the ISO 9660 compatibility limit are not added to the legacy namespace. Older ISO 9660-only readers may not see those deeper entries.

The **Boot ISO image** option requires a suitable boot-image file and BIOS/UEFI selection. An arbitrary boot image does not guarantee that the final ISO will boot on every computer.

If **Create GZip copy (.iso.gz)** is selected, the app produces a second, compressed file alongside the ordinary `.iso`. It does **not** compress the ISO file itself.

The writer creates a temporary ISO and reopens it for validation before replacing an existing output ISO. After success, **Open Folder** becomes available next to **Pack ISO** and opens the directory containing the finished ISO.

## Unpack ISO

Select an ISO image. The destination is determined automatically in the ISO's containing directory:

- `C:\Images\Example.iso` → `C:\Images\Example\`
- If `Example` already exists → `C:\Images\Example_ISO Workshop\`
- If `Example_ISO Workshop` already exists, files with the same names are overwritten there, while unrelated existing files are preserved.

The app attempts extraction using `pycdlib` first. If the ISO cannot be opened, it searches common 7-Zip installation directories, folders next to the application, and the system PATH. A valid `7z.exe` + `7z.dll` pair with ISO/UDF support can serve as a fallback. The limited `7za.exe` and `7zr.exe` editions are not used for ISO/UDF extraction.

Fallback extraction is performed in a staging directory, with checks against unsafe paths, symlinks, and special files before files are copied into the destination. ISO images from untrusted sources should still be treated cautiously.

After a successful extraction, **Open Folder** opens the extracted directory.

## Status and error behavior

Operations run in a background worker, leaving the interface responsive. Progress and concise English status messages are displayed inside the form. **Successful operations no longer open a redundant completion popup.** Errors and overwrite confirmations remain visible so failed operations are not silently ignored.

## Build a Windows executable (optional)

```
py -m pip install pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name "ISO Workshop" iso_workshop.py
```

The running window uses the file icon registered for `.iso` in Windows. A packaged `.exe` may require an additional `.ico` file at build time if you want to customize the executable's icon on disk.

## Project files

- `iso_workshop.py` — application, ISO engine, GUI and background tasks

The application processes files locally. It does not upload your ISO files to a service.
