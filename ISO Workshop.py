#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ISO Workshop - dark Qt GUI to create and extract ISO images.

Requirements: Python 3.10+, PySide6, pycdlib >= 1.20.
Boot mode adds an El Torito boot entry; an actually bootable boot image
must be provided by the user. The app does not invent boot code.
"""

# Support #
# Update Python # py -m pip install --upgrade pip
# Install Components # py -m pip install --upgrade PySide6 pycdlib pyinstaller
# Optional: export Windows ISO icon ONCE for PyInstaller's executable icon:
# py "ISO Workshop.py" --export-icon "ISOWorkshop-build.ico"
# Compile: py -m PyInstaller --noconfirm --clean --onefile --windowed --name "ISO Workshop" --icon "ISOWorkshop.ico" --collect-all pycdlib "ISO Workshop.py"
# The build-only ICO can be deleted after compiling; the script and EXE do not need it at runtime.

from __future__ import annotations

import gzip
import ctypes
import os
import re
import struct
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt, QThread, QUrl, Signal, QSize, QTimer
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget, QStyle
)

try:
    import pycdlib
except ImportError:
    pycdlib = None


# Hide the Windows console when launched as .py by double-clicking.
# Prefer the companion iso_workshop.pyw for console-free startup.
if os.name == "nt":
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ISOWorkshop.CustomIcon")
    except (AttributeError, OSError):
        pass


# -------------------------- ISO ENGINE -------------------------------


def _clean_iso_name(name: str, is_dir: bool, level: int, used: set[str]) -> str:
    """Create a unique, portable ISO9660 8.3 / 31-character name."""
    normalized = unicodedata.normalize("NFKD", name.replace("ı", "i").replace("İ", "I"))
    normalized = normalized.encode("ascii", errors="ignore").decode("ascii").upper()
    if is_dir:
        stem, ext = normalized, ""
    elif "." in normalized and not normalized.startswith("."):
        stem, ext = normalized.rsplit(".", 1)
    else:
        stem, ext = normalized, ""
    stem = re.sub(r"[^A-Z0-9_]", "_", stem).strip("_") or ("DIR" if is_dir else "FILE")
    ext = re.sub(r"[^A-Z0-9_]", "_", ext)
    # The ISO9660 namespace is a compatibility view; Joliet/UDF keep original names.
    max_stem = 8 if level == 1 else (31 if is_dir else 25)
    max_ext = 3 if level == 1 else 5
    ext = ext[:max_ext]
    for count in range(1_000_000):
        suffix = "" if not count else "_" + str(count)
        base = stem[:max(1, max_stem - len(suffix))] + suffix
        candidate = base if is_dir else base + ("." + ext if ext else ".")
        key = candidate.upper()
        if key not in used:
            used.add(key)
            return candidate
    raise ValueError("Could not generate a unique ISO 9660 name.")


def _virtual_join(parent: str, name: str) -> str:
    return (parent.rstrip("/") + "/" + name) if parent != "/" else "/" + name


def _safe_destination(folder: Path, virtual_path: str) -> Path:
    """Prevent path traversal and unsafe extraction through existing symlinks."""
    if isinstance(virtual_path, bytes):
        virtual_path = virtual_path.decode("utf-8", "replace")
    pieces = virtual_path.replace("\\", "/").split("/")
    parts = []
    for piece in pieces:
        if piece in ("", "."):
            continue
        if piece == "..":
            raise ValueError("Unsafe parent-directory path found inside the ISO.")
        piece = re.sub(r'[<>:"|?*\x00-\x1f]', "_", piece).rstrip(". ")
        if not piece:
            raise ValueError("Invalid filename found inside the ISO.")
        if os.name == "nt" and piece.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            piece = "_" + piece
        parts.append(piece)
    target = folder.joinpath(*parts)
    root = folder.resolve()
    # A directory might exist already because of the explicit overlay rule.
    probe = folder
    if probe.is_symlink():
        raise ValueError("Destination folder cannot be a symbolic link.")
    for name in parts:
        probe = probe / name
        if probe.is_symlink():
            raise ValueError(f"Writing through a symbolic link is blocked: {probe}")
    if not target.resolve(strict=False).is_relative_to(root):
        raise ValueError("The ISO entry attempts to write outside the destination folder.")
    return target


ISO9660_MAX_SUBDIRECTORY_LEVELS = 7
RECOMMENDED_FILE_SYSTEM = "hybrid"


def _source_reaches_iso_depth_limit(source: Path) -> bool:
    """Safely scan source tree; ignore symlinks, stop as soon as seven nested levels are found.

    ISO9660 stores at most seven nested subdirectories under its root.
    Switch proactively at level seven to leave a safety margin.
    The scan runs both on folder selection / filesystem changes and before packing.
    """
    if not source.is_dir():
        return False

    def fail_on_scan_error(error):
        raise error

    for folder, dirnames, _ in os.walk(source, followlinks=False,
                                        onerror=fail_on_scan_error):
        current = Path(folder)
        dirnames[:] = [
            d for d in dirnames if not (current / d).is_symlink()
        ]
        if len(current.relative_to(source).parts) >= ISO9660_MAX_SUBDIRECTORY_LEVELS:
            return True
    return False


def _uses_iso9660_only_or_joliet(system: str) -> bool:
    """Selections that cannot represent directories deeper than ISO9660's limit."""
    return system in ("iso", "joliet")


def _iso_file_system_kwargs(system: str, level: int, label: str) -> dict:
    settings = {
        "vol_ident": label,
        "interchange_level": level,
        "app_ident_str": "ISO WORKSHOP",
    }
    if system in ("joliet", "hybrid"):
        settings["joliet"] = 3
    if system in ("udf", "hybrid"):
        settings["udf"] = "2.60"
    return settings


def _make_iso(config: dict, progress) -> list[Path]:
    if pycdlib is None:
        raise RuntimeError("pycdlib eksik. Kurulum: python -m pip install pycdlib PySide6")

    source = Path(config["source"]).expanduser().resolve()
    target = Path(config["target"]).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("Select an existing folder in Source.")
    if not target.name.lower().endswith(".iso"):
        raise ValueError("Target must end with .iso.")
    if not target.parent.is_dir():
        raise ValueError("The destination directory does not exist.")
    if target.is_relative_to(source):
        raise ValueError("Target ISO cannot be inside Source: that would cause recursive inclusion.")
    if not config["label"].strip():
        raise ValueError("ISO label cannot be empty.")
    if len(config["label"]) > 32 or not re.fullmatch(r"[A-Za-z0-9_ ]+", config["label"]):
        raise ValueError("Label must be at most 32 characters: A-Z, 0-9, spaces and underscores.")

    # Defensive check for direct engine calls: the UI automatically switches
    # incompatible ISO-only/Joliet selections to the recommended UDF bridge.
    if _uses_iso9660_only_or_joliet(config["filesystem"]) and _source_reaches_iso_depth_limit(source):
        raise ValueError(
            "Source contains seven or more nested directory levels. "
            "Select UDF 2.60 + Joliet as the file system."
        )

    is_boot = config["type"] == "boot"
    boot_path = Path(config.get("boot_path", "")).expanduser()
    if is_boot and not boot_path.is_file():
        raise ValueError("Boot ISO requires a valid boot image (.img/.bin).")

    settings = _iso_file_system_kwargs(config["filesystem"], config["level"], config["label"].strip())
    with_udf = config["filesystem"] in ("udf", "hybrid")
    with_joliet = config["filesystem"] in ("joliet", "hybrid")

    progress(2, "Scanning source directory...")
    directories = []
    files = []
    def fail_on_scan_error(error):
        raise error

    for root, dirnames, filenames in os.walk(source, followlinks=False,
                                             onerror=fail_on_scan_error):
        dirnames[:] = sorted(d for d in dirnames if not (Path(root) / d).is_symlink())
        for dirname in dirnames:
            directories.append(Path(root) / dirname)
        for filename in sorted(filenames):
            local = Path(root) / filename
            if local.is_file() and not local.is_symlink():
                files.append(local)
    total_items = max(1, len(directories) + len(files))

    # Each ISO9660 directory gets a unique set of generated compatibility names.
    used_names = defaultdict(set)
    iso_dirs = {source: "/"}
    iso = pycdlib.PyCdlib()
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.partial.iso")
    gzip_target = Path(str(target) + ".gz")
    gzip_temp = Path(str(gzip_target) + "." + uuid.uuid4().hex + ".part")
    created = []
    try:
        progress(6, "Preparing ISO file system...")
        iso.new(**settings)
        processed = 0
        # os.walk may return paths whose parent was added first; sorting by depth makes this explicit.
        for local in sorted(directories, key=lambda p: (len(p.relative_to(source).parts), str(p).casefold())):
            relative = local.relative_to(source).parts
            kwargs = {}
            # ISO9660's maximum depth applies even inside a UDF/Joliet bridge.
            # Omit the over-depth record from the *ISO9660 namespace only*.
            # UDF/Joliet retain the exact original full directory structure.
            if len(relative) <= ISO9660_MAX_SUBDIRECTORY_LEVELS:
                parent_iso = iso_dirs[local.parent]
                iso_name = _clean_iso_name(local.name, True, config["level"], used_names[local.parent])
                iso_path = _virtual_join(parent_iso, iso_name)
                kwargs["iso_path"] = iso_path
                iso_dirs[local] = iso_path
            else:
                iso_dirs[local] = None
            if with_joliet:
                kwargs["joliet_path"] = "/" + "/".join(relative)
            if with_udf:
                kwargs["udf_path"] = "/" + "/".join(relative)
            iso.add_directory(**kwargs)
            processed += 1
            if processed % 30 == 0:
                progress(6 + int(24 * processed / total_items), "Adding directories to ISO...")

        for local in files:
            relative = local.relative_to(source).parts
            kwargs = {}
            parent_iso = iso_dirs[local.parent]
            # pycdlib also counts the filename when enforcing the ISO9660
            # seven-component limit.  A file below seven nested folders has
            # eight components, even if its parent directory was added legally.
            # In UDF/Joliet bridge mode preserve the complete original path in
            # those namespaces, but never add that file to ISO9660's tree.
            if parent_iso is not None and len(relative) <= ISO9660_MAX_SUBDIRECTORY_LEVELS:
                iso_name = _clean_iso_name(local.name, False, config["level"], used_names[local.parent])
                kwargs["iso_path"] = _virtual_join(parent_iso, iso_name) + ";1"
            if with_joliet:
                kwargs["joliet_path"] = "/" + "/".join(relative)
            if with_udf:
                kwargs["udf_path"] = "/" + "/".join(relative)
            iso.add_file(str(local), **kwargs)
            processed += 1
            if processed % 30 == 0:
                progress(6 + int(24 * processed / total_items), f"Adding file: {local.name}")

        if is_boot:
            progress(33, "Preparing El Torito boot entry...")
            boot_filename = _clean_iso_name("ISOWBOOT.BIN", False, config["level"], used_names[source])
            boot_iso_path = "/" + boot_filename + ";1"
            boot_kwargs = {"iso_path": boot_iso_path}
            if with_joliet:
                boot_kwargs["joliet_path"] = "/_isoworkshop_boot.img"
            if with_udf:
                boot_kwargs["udf_path"] = "/_isoworkshop_boot.img"
            iso.add_file(str(boot_path), **boot_kwargs)
            boot_args = {
                "efi": config["boot_platform"] == "uefi",
                "bootcatfile": "/BOOT.CAT;1",
            }
            if with_joliet:
                boot_args["joliet_bootcatfile"] = "/_isoworkshop_boot.cat"
            if with_udf:
                boot_args["udf_bootcatfile"] = "/_isoworkshop_boot.cat"
            iso.add_eltorito(boot_iso_path, **boot_args)

        progress(38, "Writing ISO image...")

        def master_progress(completed, total, _opaque):
            if total:
                fraction = max(0.0, min(1.0, completed / total))
                upper = 82 if config["compress"] else 98
                progress(38 + int((upper - 38) * fraction), "Writing ISO image...")

        iso.write(str(temporary), progress_cb=master_progress)
        iso.close()

        # Check that this result can be opened before replacing the original.
        # A failed write must never destroy the user's last working ISO.
        progress(82 if config["compress"] else 99, "Verifying the created ISO...")
        verifier = pycdlib.PyCdlib()
        try:
            verifier.open(str(temporary))
            if with_udf and not verifier.has_udf():
                raise RuntimeError("UDF is missing from the created ISO.")
            if with_joliet and not verifier.has_joliet():
                raise RuntimeError("Joliet is missing from the created ISO.")
        finally:
            try:
                verifier.close()
            except Exception:
                pass
        # Replace only after a successfully written and reopenable image.
        os.replace(temporary, target)
        created.append(target)

        if config["compress"]:
            progress(84, "Compressing GZip copy...")
            src_size = max(1, target.stat().st_size)
            copied = 0
            with target.open("rb") as src, gzip.open(gzip_temp, "wb", compresslevel=6) as dst:
                while True:
                    chunk = src.read(2 * 1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    progress(84 + int(15 * copied / src_size), "Compressing ISO.GZ copy...")
            os.replace(gzip_temp, gzip_target)
            created.append(gzip_target)
        progress(100, "Completed.")
        return created
    finally:
        try:
            iso.close()
        except Exception:
            pass
        temporary.unlink(missing_ok=True)
        gzip_temp.unlink(missing_ok=True)


def _resolve_unpack_folder(iso_path: Path) -> Path:
    """Prefer <name>/; if occupied use <name>_ISO Workshop/ and overlay it."""
    basic = iso_path.parent / iso_path.stem
    if not basic.exists():
        return basic
    return iso_path.parent / (iso_path.stem + "_ISO Workshop")


def _sevenzip_candidates() -> list[Path]:
    """Use Byte Wrapper's installed, portable and PATH resolution strategy.

    7za/7zr are deliberately excluded: their reduced decoders do not support
    ISO/UDF. For ISO extraction require the full 7z+7z.dll pair or 7zz.
    """
    app_dir = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
               else Path(__file__).resolve().parent)
    folders = []
    if os.name == "nt":
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if os.environ.get(key):
                folders.append(Path(os.environ[key]) / "7-Zip")
        folders.extend([Path(r"C:\Program Files\7-Zip"),
                        Path(r"C:\Program Files (x86)\7-Zip")])
    folders.extend([app_dir, app_dir / "7-Zip", app_dir / "7-Zip" / "x64"])
    folders.extend([path / "x64" for path in list(folders) if path.name.lower() == "7-zip"])
    result = []
    seen = set()
    for folder in folders:
        for name in ("7z.exe", "7zz.exe", "7z", "7zz"):
            path = folder / name
            key = os.path.normcase(str(path.resolve(strict=False)))
            if key not in seen and path.is_file():
                result.append(path)
                seen.add(key)
    for name in ("7z", "7zz", "7z.exe", "7zz.exe"):
        resolved = shutil.which(name)
        if resolved:
            path = Path(resolved)
            key = os.path.normcase(str(path.resolve(strict=False)))
            if key not in seen:
                result.append(path)
                seen.add(key)
    return result


def _sevenzip_dependency_status() -> dict[str, tuple[bool, str]]:
    """Report components in the same installation (Byte Wrapper-style indicator).

    A 7z.dll found in an unrelated directory does not satisfy 7z.exe.
    Checking on disk is fast; the real ISO/UDF capability test runs in the worker.
    """
    candidates = _sevenzip_candidates()
    full_candidates = [p for p in candidates if p.name.lower() in ("7z.exe", "7z")]
    full = next((p for p in full_candidates if (p.parent / "7z.dll").is_file()), None)
    if full is None:
        full = full_candidates[0] if full_candidates else None
    dll = (full.parent / "7z.dll") if full else None
    return {
        "7z.exe": (full is not None, str(full) if full else "7z.exe was not found"),
        "7z.dll": (bool(dll and dll.is_file()), str(dll) if dll else "Requires 7z.dll next to 7z.exe"),
    }


def _locate_iso_extractor() -> tuple[str, str]:
    """Return a working 7-Zip ISO/UDF decoder and informative missing-file diagnostics.

    Files existing on disk alone are insufficient: inspect 7z's actual format
    support via its command-line 'i' command. Called only in the worker thread.
    """
    problems = []
    candidates = _sevenzip_candidates()
    for binary in candidates:
        # 7z.exe needs its matching 7z.dll. 7zz.exe is standalone.
        if os.name == "nt" and binary.name.lower() == "7z.exe" and not (binary.parent / "7z.dll").is_file():
            problems.append(f"{binary}: missing 7z.dll in the same folder")
            continue
        try:
            options = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                       "check": False, "timeout": 8}
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            tested = subprocess.run([str(binary), "i"], **options)
            output = tested.stdout.decode("utf-8", errors="replace")
            # The ASCII format names in '7z i' are independent of output encoding.
            # Restrict the search to the Formats section to avoid false matches
            # from path names, license texts and command explanations.
            match = re.search(r"(?is)\bFormats\s*:\s*(.*?)(?:\bCodecs\s*:|\bHashers\s*:|\Z)", output)
            formats = match.group(1) if match else ""
            if tested.returncode == 0 and re.search(r"(?i)\bIso\b", formats) and re.search(r"(?i)\bUdf\b", formats):
                return str(binary), f"ISO/UDF ready: {binary}"
            problems.append(f"{binary}: ISO/UDF support could not be verified (exit code {tested.returncode})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{binary}: could not start ({exc})")
    if not candidates:
        return "", ("7-Zip was not found. For ISO/UDF extraction install the full 7-Zip version "
                    "(7z.exe and 7z.dll in the same folder). "
                    "Install it normally or place both files beside this application.")
    return "", ("No compatible ISO/UDF 7-Zip extractor was found. "
                "Check that 7z.exe and 7z.dll are in the same folder. "
                "Diagnostics: " + " | ".join(problems[:4]))


def _find_7zip() -> str:
    """Compatibility wrapper; returns only verified ISO/UDF-capable 7-Zip."""
    return _locate_iso_extractor()[0]


def _unpack_via_7zip(input_path: Path, output: Path, progress, original_error: Exception) -> list[Path]:
    """Fallback for valid UDF-only / otherwise unsupported ISO images.

    Extract into a fresh, isolated staging area; validate all extracted paths
    and reject symlinks/reparse points before overlaying the output folder.
    Existing unrelated files in the destination remain intact.
    """
    progress(5, "Checking 7-Zip dependencies...")
    executable, description = _locate_iso_extractor()
    if not executable:
        raise RuntimeError(
            "pycdlib could not open the ISO, and the 7-Zip fallback "
            f"is unavailable.\n{description}\nOriginal ISO error: {original_error}"
        ) from original_error

    progress(8, "Extracting ISO with 7-Zip...")
    with tempfile.TemporaryDirectory(prefix=".iso_workshop_extract_", dir=str(input_path.parent)) as tmp:
        staging = Path(tmp)
        # 7-Zip reads the untrusted ISO into a temporary tree only; it never
        # receives the user's existing target directory as its output.
        cmd = [executable, "x", "-y", "-bd", f"-o{staging}", "--", str(input_path)]
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                check=False, **kwargs)
        if result.returncode != 0:
            details = result.stdout[-2500:].decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"7-Zip could not extract the ISO (exit code {result.returncode}).\n{details}")

        progress(76, "Validating extracted files...")
        staged_dirs = []
        staged_files = []
        for folder, dirnames, filenames in os.walk(staging, topdown=True, followlinks=False):
            root = Path(folder)
            for name in dirnames:
                candidate = root / name
                info = candidate.lstat()
                if stat.S_ISLNK(info.st_mode) or (
                    os.name == "nt" and getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise ValueError(f"Unsafe link in ISO: {name}")
                relative = candidate.relative_to(staging).as_posix()
                staged_dirs.append((_safe_destination(output, relative), candidate))
            for name in filenames:
                candidate = root / name
                info = candidate.lstat()
                if not stat.S_ISREG(info.st_mode) or (
                    os.name == "nt" and getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise ValueError(f"Unsupported special file in ISO: {name}")
                relative = candidate.relative_to(staging).as_posix()
                staged_files.append((_safe_destination(output, relative), candidate))

        # Validate every destination before modifying existing contents.
        for dst, _ in staged_dirs:
            if dst.exists() and not dst.is_dir():
                raise ValueError(f"A file blocks a directory path: {dst}")
        for dst, _ in staged_files:
            if dst.exists() and not dst.is_file():
                raise ValueError(f"A folder blocks a file path: {dst}")
        output.mkdir(parents=True, exist_ok=True)
        for dst, _ in staged_dirs:
            dst.mkdir(parents=True, exist_ok=True)
        total = max(1, len(staged_files))
        for index, (dst, src) in enumerate(staged_files, 1):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            if index == total or index % 30 == 0:
                progress(77 + int(22 * index / total), f"Extracting: {src.name}")
    progress(100, "ISO extracted successfully.")
    return [output]


def _unpack_iso(iso_file: str, progress) -> list[Path]:
    if pycdlib is None:
        raise RuntimeError("pycdlib eksik. Kurulum: python -m pip install pycdlib PySide6")
    input_path = Path(iso_file).expanduser().resolve()
    if not input_path.is_file() or input_path.suffix.lower() != ".iso":
        raise ValueError("Select a valid .iso file.")
    output = _resolve_unpack_folder(input_path)
    if output.exists() and not output.is_dir():
        raise ValueError(f"Cannot create output folder; a file already exists with this name: {output}")

    iso = pycdlib.PyCdlib()
    try:
        progress(3, "ISO inceleniyor...")
        try:
            iso.open(str(input_path))
        except Exception as open_error:
            # A UDF-only image can be valid even though it lacks the ISO9660
            # Primary Volume Descriptor that pycdlib insists on reading.
            # An alternative extractor also handles some nonstandard ISO files.
            return _unpack_via_7zip(input_path, output, progress, open_error)
        if iso.has_udf():
            namespace = "udf_path"
        elif iso.has_joliet():
            namespace = "joliet_path"
        elif iso.has_rock_ridge():
            namespace = "rr_path"
        else:
            namespace = "iso_path"

        entries = list(iso.walk(**{namespace: "/"}))
        total = max(1, sum(len(filelist) for _, _, filelist in entries))
        output.mkdir(parents=True, exist_ok=True)
        count = 0
        for dirname, dirlist, filelist in entries:
            if isinstance(dirname, bytes):
                dirname = dirname.decode("utf-8", "replace")
            folder = _safe_destination(output, dirname)
            folder.mkdir(parents=True, exist_ok=True)
            for subdir in dirlist:
                if isinstance(subdir, bytes):
                    subdir = subdir.decode("utf-8", "replace")
                _safe_destination(output, _virtual_join(dirname, subdir)).mkdir(parents=True, exist_ok=True)
            for file_name in filelist:
                if isinstance(file_name, bytes):
                    file_name = file_name.decode("utf-8", "replace")
                full_path = _virtual_join(dirname, file_name)
                # ISO9660 includes a ;1 version suffix not used by Windows filesystems.
                local_name = re.sub(r";\d+$", "", file_name) if namespace == "iso_path" else file_name
                local_name = local_name.rstrip(".") if namespace == "iso_path" else local_name
                local_file = _safe_destination(output, _virtual_join(dirname, local_name))
                local_file.parent.mkdir(parents=True, exist_ok=True)
                if local_file.exists() and local_file.is_dir():
                    raise ValueError(f"Cannot overwrite a directory with a file: {local_file}")
                # Existing matching file is intentionally overwritten; other files stay intact.
                iso.get_file_from_iso(str(local_file), **{namespace: full_path})
                count += 1
                progress(8 + int(91 * count / total), f"Extracting: {local_name}")
        progress(100, "ISO extracted successfully.")
        return [output]
    finally:
        # If open() failed (invalid/unsupported ISO), close() raises
        # "This object is not initialized" and hides the actual cause.
        # Keep the original exception visible in the error dialog.
        try:
            iso.close()
        except Exception:
            pass


# ---------------------- WINDOWS ISO ICON -----------------------------


def _extract_windows_iso_icon() -> bytes | None:
    """Read Windows' native ISO-file icon (RT_GROUP_ICON 5205) into ICO bytes.

    No companion icon file, third-party Windows package or temporary file is
    needed. Windows 10/11 may keep icon resources in imageres.dll.mun instead
    of the lightweight imageres.dll stub, so both locations are checked.
    """
    if os.name != "nt":
        return None

    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    api.LoadLibraryExW.restype = wintypes.HMODULE
    api.FindResourceW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p)
    api.FindResourceW.restype = wintypes.HRSRC
    api.SizeofResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    api.SizeofResource.restype = wintypes.DWORD
    api.LoadResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    api.LoadResource.restype = wintypes.HGLOBAL
    api.LockResource.argtypes = (wintypes.HGLOBAL,)
    api.LockResource.restype = ctypes.c_void_p
    api.FreeLibrary.argtypes = (wintypes.HMODULE,)
    api.FreeLibrary.restype = wintypes.BOOL

    def read_resource(module, resource_type: int, resource_id: int) -> bytes | None:
        # Integer resource IDs are MAKEINTRESOURCE pointers, not Unicode strings.
        handle = api.FindResourceW(module, ctypes.c_void_p(resource_id), ctypes.c_void_p(resource_type))
        if not handle:
            return None
        length = api.SizeofResource(module, handle)
        resource = api.LoadResource(module, handle)
        address = api.LockResource(resource) if resource else None
        return ctypes.string_at(address, length) if address and length else None

    windows_dir = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
    paths = (windows_dir / "System32" / "imageres.dll",
             windows_dir / "SystemResources" / "imageres.dll.mun")
    for file in paths:
        if not file.is_file():
            continue
        # Do not execute the DLL. Load it as resource-only data.
        module = api.LoadLibraryExW(str(file), None, 0x00000002 | 0x00000020)
        if not module:
            continue
        try:
            group = read_resource(module, 14, 5205)  # RT_GROUP_ICON / ISO file icon
            if not group or len(group) < 6:
                continue
            reserved, icon_type, count = struct.unpack_from("<HHH", group)
            if reserved != 0 or icon_type != 1 or not 1 <= count <= 256 or len(group) < 6 + 14 * count:
                continue
            images = []
            for idx in range(count):
                width, height, colors, reserved_byte, planes, bit_depth, _, resource_id = \
                    struct.unpack_from("<BBBBHHIH", group, 6 + 14 * idx)
                image = read_resource(module, 3, resource_id)  # RT_ICON
                if image:
                    images.append((width, height, colors, reserved_byte, planes, bit_depth, image))
            if not images:
                continue

            # Convert the Windows icon-group directory to a standard .ico file
            # in memory, preserving all resolutions and PNG-compressed frames.
            ico = bytearray(struct.pack("<HHH", 0, 1, len(images)))
            offset = 6 + 16 * len(images)
            for width, height, colors, reserved_byte, planes, bit_depth, image in images:
                ico.extend(struct.pack("<BBBBHHII", width, height, colors, reserved_byte,
                                       planes, bit_depth, len(image), offset))
                offset += len(image)
            for *_, image in images:
                ico.extend(image)
            return bytes(ico)
        except (ValueError, struct.error, OSError):
            # A Windows release may lack or move the resource; try the .mun file.
            continue
        finally:
            api.FreeLibrary(module)
    return None


def _qicon_from_ico_bytes(data: bytes) -> QIcon:
    """Build a multi-resolution QIcon from bytes without writing any ICO file."""
    icon = QIcon()
    if len(data) < 6:
        return icon
    _, _, count = struct.unpack_from("<HHH", data)
    if not 1 <= count <= 256 or len(data) < 6 + count * 16:
        return icon
    for idx in range(count):
        entry = data[6 + idx * 16:6 + (idx + 1) * 16]
        length, offset = struct.unpack_from("<II", entry, 8)
        if offset + length > len(data) or not length:
            continue
        # A single-image ICO gives Qt an explicit pixmap for each icon size.
        single = struct.pack("<HHH", 0, 1, 1) + entry[:12] + struct.pack("<I", 22) + data[offset:offset + length]
        pixmap = QPixmap()
        if pixmap.loadFromData(single, "ICO"):
            icon.addPixmap(pixmap)
    return icon


def _draw_fallback_iso_icon() -> QIcon:
    """Draw a file-with-disc icon when Windows' imageres resource is unavailable.

    This fallback is generated in memory. Neither a sidecar ICO file nor any
    additional image package is required, and it works on any supported Qt OS.
    """
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.scale(size / 256, size / 256)
            # Folded document silhouette.
            sheet = QPainterPath()
            sheet.moveTo(45, 17)
            sheet.lineTo(160, 17)
            sheet.lineTo(211, 69)
            sheet.lineTo(211, 239)
            sheet.lineTo(45, 239)
            sheet.closeSubpath()
            painter.setPen(QPen(QColor("#8b9195"), 4))
            painter.setBrush(QColor("#fafbfc"))
            painter.drawPath(sheet)
            painter.setPen(QPen(QColor("#8b9195"), 3))
            painter.drawLine(QPointF(160, 18), QPointF(160, 69))
            painter.drawLine(QPointF(160, 69), QPointF(211, 69))

            # Optical disc, recognizable even at the smaller icon sizes.
            disc = QRadialGradient(111, 132, 83)
            disc.setColorAt(0, QColor("#ffffff"))
            disc.setColorAt(0.45, QColor("#d7e1e4"))
            disc.setColorAt(0.77, QColor("#aab6bb"))
            disc.setColorAt(1, QColor("#eff2f2"))
            painter.setBrush(disc)
            painter.setPen(QPen(QColor("#b6c3c5"), 1.5))
            painter.drawEllipse(QPointF(128, 147), 61, 61)
            painter.setBrush(QColor("#f4f6f6"))
            painter.setPen(QPen(QColor("#b5c1c6"), 2))
            painter.drawEllipse(QPointF(128, 147), 19, 19)
            painter.setBrush(QColor("#dfe6e8"))
            painter.setPen(QPen(QColor("#bcc5c8"), 1.5))
            painter.drawEllipse(QPointF(128, 147), 7, 7)
        finally:
            painter.end()
        result.addPixmap(canvas)
    return result


# --------------------------- GUI ------------------------------------

# Compact dark palette and field sizing match the Byte Wrapper conventions.
STYLE = """
QWidget {
    background-color: #0b1016;
    color: #e2e8f0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
}
QLabel { background-color: transparent; }
QLabel#HeaderTitle { color: #f1f5f9; font-size: 16px; font-weight: 600; }
QLabel#HeaderSubtitle { color: #94a3b8; font-size: 11px; }
QLabel#SectionLabel { color: #e2e8f0; font-size: 12px; font-weight: 600; }
QLabel#HintLabel { color: #64748b; font-size: 11px; }
QLabel#StatusLabel { color: #94a3b8; font-size: 11px; }
QPushButton#WinControlBtn, QPushButton#WinCloseBtn {
    background: transparent; border: 0; border-radius: 0;
    color: #94a3b8; font-size: 13px; padding: 0;
}
QPushButton#WinControlBtn:hover { background: #1e293b; color: white; }
QPushButton#WinCloseBtn:hover { background: #ef4444; color: white; }
QLineEdit, QComboBox {
    background-color: #131b23; border: 1px solid #1e2936;
    border-radius: 4px; color: #e2e8f0; font-size: 12px;
    padding: 0 8px; min-height: 32px; max-height: 32px;
    selection-background-color: #2563eb;
}
QLineEdit:focus, QComboBox:focus { border-color: #3b82f6; }
QLineEdit:read-only { color: #94a3b8; }
QComboBox::drop-down { border: 0; width: 26px; }
QComboBox QAbstractItemView {
    background: #131b24; border: 1px solid #263445; color: #e2e8f0;
    selection-background-color: #2563eb;
}
QPushButton#SmallBtn {
    background-color: #17202a; border: 1px solid #222f3d;
    border-radius: 3px; color: #94a3b8; font-size: 14px;
    padding: 0 4px;
}
QPushButton#SmallBtn:hover { color: #cbd5e1; border-color: #334155; }
QPushButton#ActionBtn {
    background-color: #131b23; border: 1px solid #1e2936;
    border-radius: 4px; color: #e2e8f0; font-weight: 500;
    font-size: 12px; text-align: left; padding: 0 9px;
}
QPushButton#ActionBtn:hover { background: #17212c; border-color: #3b82f6; }
QPushButton#StartBtn {
    background-color: #111820; border: 1px solid #1e2936;
    border-radius: 4px; color: #64748b; font-size: 12px;
    font-weight: 500; padding: 0 14px;
}
QPushButton#StartBtn:hover { background: #17212c; border-color: #3b82f6; color: #e2e8f0; }
QPushButton:disabled { color: #475569; }
QCheckBox { color: #64748b; spacing: 7px; font-size: 11px; }
QCheckBox:checked { color: #e2e8f0; }
QCheckBox::indicator {
    width: 16px; height: 16px; background: #131b23;
    border: 1px solid #1e2936; border-radius: 3px;
}
QCheckBox::indicator:checked { background: #2563eb; border-color: #3b82f6; }
QProgressBar { background: #26313f; border: 0; border-radius: 1px; }
QProgressBar::chunk { background: #3b82f6; }
QToolTip { background: #131b24; color: #e2e8f0; border: 1px solid #263445; }
QFrame#InfoPopup { background-color: #0b1016; border: 1px solid #263445; }
QPushButton#InfoLink, QPushButton#InfoDependency {
    border: 0; background: transparent; color: #a0adbc;
    padding: 0; font-size: 12px;
}
QPushButton#InfoLink:hover { color: #e2e8f0; }

"""


class TitleBar(QWidget):
    """Single draggable title bar, following the Byte Wrapper design."""

    def __init__(self, parent: "ISOApp", iso_icon: QIcon):
        super().__init__(parent)
        self.owner = parent
        self.setFixedHeight(58)
        self.drag_origin: QPoint | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 11, 18, 5)
        row.setSpacing(10)

        logo = QLabel()
        logo.setFixedSize(36, 36)
        logo.setPixmap(iso_icon.pixmap(36, 36))
        row.addWidget(logo)

        title_column = QVBoxLayout()
        title_column.setContentsMargins(0, 0, 0, 0)
        title_column.setSpacing(1)
        title = QLabel("ISO Workshop")
        title.setObjectName("HeaderTitle")
        title_column.addWidget(title)
        subtitle = QLabel("Create and unpack ISO images")
        subtitle.setObjectName("HeaderSubtitle")
        title_column.addWidget(subtitle)
        row.addLayout(title_column)
        row.addStretch(1)

        self.back = self._window_button("?", "About and 7-Zip diagnostics", "WinControlBtn", self.owner._back_or_info)
        row.addWidget(self.back, alignment=Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._window_button("—", "Minimize", "WinControlBtn", self.owner.showMinimized),
                      alignment=Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._window_button("✕", "Close", "WinCloseBtn", self.owner.close),
                      alignment=Qt.AlignmentFlag.AlignTop)

    def _window_button(self, title: str, tooltip: str, name: str, callback):
        button = QPushButton(title)
        button.setObjectName(name)
        button.setFixedSize(26, 26)
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                handle = self.owner.windowHandle()
                if handle is not None and handle.startSystemMove():
                    event.accept()
                    return
            except (AttributeError, RuntimeError):
                pass
            self.drag_origin = event.globalPosition().toPoint() - self.owner.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.owner.move(event.globalPosition().toPoint() - self.drag_origin)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.drag_origin = None
        super().mouseReleaseEvent(event)


class TaskThread(QThread):
    update = Signal(int, str)
    success = Signal(object)
    failure = Signal(str)

    def __init__(self, operation: str, settings):
        super().__init__()
        self.operation = operation
        self.settings = settings

    def run(self):
        try:
            if self.operation == "pack":
                result = _make_iso(self.settings, self.update.emit)
            else:
                result = _unpack_iso(self.settings, self.update.emit)
            self.success.emit(result)
        except Exception as error:
            self.failure.emit(f"{type(error).__name__}: {error}")


def _label(value: str, style: str = "SectionLabel") -> QLabel:
    label = QLabel(value)
    label.setObjectName(style)
    return label


class CompactStack(QStackedWidget):
    """Keep hidden Boot controls from forcing the home page to grow."""

    def sizeHint(self):
        return QSize(0, 0)

    def minimumSizeHint(self):
        return QSize(0, 0)


class ISOApp(QMainWindow):
    # Fixed pixel presets; dynamically expand if fonts or DPI demand it.
    # 380px width with 18px horizontal margins, no scroll areas.
    HOME_SIZE = (380, 130)
    PACK_SIZE = (380, 509)
    BOOT_SIZE = (380, 573)
    UNPACK_SIZE = (380, 336)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ISO Workshop")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.thread: TaskThread | None = None
        self.last_pack_directory: Path | None = None
        self.last_unpack_directory: Path | None = None
        self.info_popup: QFrame | None = None
        self.info_dependencies: dict[str, QPushButton] = {}
        self.app_icon = self._load_app_icon()
        # Application-level icon also covers dialogs and the Windows taskbar.
        QApplication.instance().setWindowIcon(self.app_icon)
        self.setWindowIcon(self.app_icon)

        root = QWidget()
        self.setCentralWidget(root)
        vertical = QVBoxLayout(root)
        vertical.setContentsMargins(0, 0, 0, 0)
        vertical.setSpacing(0)

        self.titlebar = TitleBar(self, self.app_icon)
        vertical.addWidget(self.titlebar)
        self.stack = CompactStack()
        # Do not let the hidden pages determine the active page's dimensions.
        self.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        vertical.addWidget(self.stack, 1)

        self.home_page = self._create_home()
        self.pack_page = self._create_pack()
        self.unpack_page = self._create_unpack()
        for page in (self.home_page, self.pack_page, self.unpack_page):
            self.stack.addWidget(page)
        self.show_home()

    @staticmethod
    def _load_app_icon() -> QIcon:
        """Use Windows' ISO document icon with a safe, file-like fallback."""
        try:
            image_bytes = _extract_windows_iso_icon()
            if image_bytes:
                icon = _qicon_from_ico_bytes(image_bytes)
                if not icon.isNull():
                    return icon
        except Exception:
            # Missing DLL, missing resource, denied access, or malformed image
            # must never prevent the main window from opening.
            pass
        try:
            icon = _draw_fallback_iso_icon()
            if not icon.isNull():
                return icon
        except Exception:
            pass
        # Final fallback uses Qt's stock document icon, not an optical drive.
        return QApplication.instance().style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 8, 18, 18)
        layout.setSpacing(6)
        return page, layout

    @staticmethod
    def _field(title: str, control: QWidget) -> QVBoxLayout:
        block = QVBoxLayout()
        block.setSpacing(3)
        block.addWidget(_label(title))
        block.addWidget(control)
        return block

    def _browse_field(self, title: str, action, hint: str) -> tuple[QVBoxLayout, QLineEdit]:
        edit = QLineEdit()
        edit.setPlaceholderText(hint)
        browse = QPushButton("…")
        browse.setObjectName("SmallBtn")
        browse.setFixedSize(32, 32)
        browse.setToolTip(f"Browse {title.lower()}")
        browse.clicked.connect(action)
        editor = QHBoxLayout()
        editor.setSpacing(6)
        editor.addWidget(edit, 1)
        editor.addWidget(browse)
        block = QVBoxLayout()
        block.setSpacing(3)
        block.addWidget(_label(title))
        block.addLayout(editor)
        return block, edit

    def _progress_widgets(self, parent: QVBoxLayout) -> tuple[QLabel, QProgressBar]:
        status = _label("Ready", "StatusLabel")
        status.setFixedHeight(17)
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setFixedHeight(10)
        progress.setValue(0)
        progress.setTextVisible(False)
        parent.addWidget(status)
        parent.addWidget(progress)
        return status, progress

    @staticmethod
    def _action_button(title: str, icon: QIcon) -> QPushButton:
        button = QPushButton(title)
        button.setObjectName("ActionBtn")
        button.setFixedHeight(42)
        button.setIcon(icon)
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _create_home(self) -> QWidget:
        page, layout = self._page()
        layout.setContentsMargins(18, 12, 18, 18)
        layout.setSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        # Icon, title and chevron share one compact line.
        pack = self._action_button("Pack ISO  ›", self.app_icon)
        unpack = self._action_button("Unpack ISO  ›", self.app_icon)
        pack.clicked.connect(self.show_pack)
        unpack.clicked.connect(self.show_unpack)
        buttons.addWidget(pack, 1)
        buttons.addWidget(unpack, 1)
        layout.addLayout(buttons)
        return page

    def _create_pack(self) -> QWidget:
        page, layout = self._page()

        self.iso_type = QComboBox()
        self.iso_type.addItem("Normal ISO image", "normal")
        self.iso_type.addItem("Boot ISO image", "boot")
        self.iso_type.currentIndexChanged.connect(self._update_boot_fields)
        layout.addLayout(self._field("Type", self.iso_type))

        fs_grid = QHBoxLayout()
        fs_grid.setSpacing(8)
        self.filesystem = QComboBox()
        self.filesystem.addItem("UDF 2.60 + Joliet (default)", "hybrid")
        self.filesystem.addItem("UDF 2.60", "udf")
        self.filesystem.addItem("ISO 9660 + Joliet", "joliet")
        self.filesystem.addItem("ISO 9660", "iso")
        self.filesystem.currentIndexChanged.connect(self._check_source_depth)
        self.iso_level = QComboBox()
        self.iso_level.addItem("Level 3", 3)
        self.iso_level.addItem("Level 2", 2)
        self.iso_level.addItem("Level 1 (8.3)", 1)
        fs_grid.addLayout(self._field("File System", self.filesystem), 3)
        fs_grid.addLayout(self._field("ISO Level", self.iso_level), 2)
        layout.addLayout(fs_grid)

        self.boot_block = QWidget()
        # Boot controls share a row so the page remains scroll-free.
        boot_box = QHBoxLayout(self.boot_block)
        boot_box.setContentsMargins(0, 0, 0, 0)
        boot_box.setSpacing(8)
        boot_line, self.boot_file = self._browse_field("Boot Image", self._browse_boot, "Select boot image…")
        boot_box.addLayout(boot_line, 3)
        self.platform = QComboBox()
        self.platform.addItem("BIOS (x86)", "bios")
        self.platform.addItem("UEFI (EFI FAT)", "uefi")
        boot_box.addLayout(self._field("Platform", self.platform), 2)
        layout.addWidget(self.boot_block)
        self.boot_block.hide()

        source_line, self.source = self._browse_field("Source", self._browse_source, "Select source folder…")
        layout.addLayout(source_line)
        # Typing/pasting a folder path is also checked, not only Browse.
        self.depth_scan_timer = QTimer(self)
        self.depth_scan_timer.setSingleShot(True)
        self.depth_scan_timer.setInterval(300)
        self.depth_scan_timer.timeout.connect(self._check_source_depth)
        self.source.textChanged.connect(lambda _text: self.depth_scan_timer.start())
        target_line, self.target = self._browse_field("Target", self._browse_target, "Choose output .iso file…")
        layout.addLayout(target_line)
        self.volume_label = QLineEdit("MY_ISO")
        self.volume_label.setMaxLength(32)
        self.volume_label.setToolTip("Up to 32 characters: A-Z, 0-9, spaces, underscores")
        layout.addLayout(self._field("Label", self.volume_label))

        self.compression = QCheckBox("Create GZip copy (.iso.gz)")
        self.compression.setToolTip("Optional: creates a compressed .iso.gz copy alongside the original ISO.")
        self.compression.setChecked(False)
        self.compression.setFixedHeight(20)
        layout.addWidget(self.compression)

        layout.addStretch(1)
        self.pack_status, self.pack_progress = self._progress_widgets(layout)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.open_pack_folder = QPushButton("Open Folder")
        self.open_pack_folder.setObjectName("SmallBtn")
        self.open_pack_folder.setFixedHeight(38)
        self.open_pack_folder.setEnabled(False)
        self.open_pack_folder.clicked.connect(self._open_pack_folder)
        bottom.addWidget(self.open_pack_folder, 1)
        self.pack_button = QPushButton("Pack ISO  →")
        self.pack_button.setObjectName("StartBtn")
        self.pack_button.setFixedHeight(38)
        self.pack_button.clicked.connect(self._start_pack)
        bottom.addWidget(self.pack_button, 2)
        layout.addLayout(bottom)
        return page

    def _create_unpack(self) -> QWidget:
        page, layout = self._page()
        source_line, self.unpack_source = self._browse_field("Source", self._browse_unpack, "Select an .iso file…")
        layout.addLayout(source_line)
        self.unpack_source.textChanged.connect(self._update_unpack_destination)
        self.unpack_target = QLineEdit()
        self.unpack_target.setReadOnly(True)
        self.unpack_target.setPlaceholderText("Destination is selected automatically")
        layout.addLayout(self._field("Target (automatic)", self.unpack_target))
        hint = _label("If the folder already exists, _ISO Workshop is used; matching files are replaced.", "HintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        self.unpack_status, self.unpack_progress = self._progress_widgets(layout)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.open_unpack_folder = QPushButton("Open Folder")
        self.open_unpack_folder.setObjectName("SmallBtn")
        self.open_unpack_folder.setFixedHeight(38)
        self.open_unpack_folder.setEnabled(False)
        self.open_unpack_folder.clicked.connect(self._open_unpack_folder)
        bottom.addWidget(self.open_unpack_folder, 1)
        self.unpack_button = QPushButton("Unpack ISO  →")
        self.unpack_button.setObjectName("StartBtn")
        self.unpack_button.setFixedHeight(38)
        self.unpack_button.clicked.connect(self._start_unpack)
        bottom.addWidget(self.unpack_button, 2)
        layout.addLayout(bottom)
        return page

    def _resize_for_page(self, size: tuple[int, int]):
        """Fit the active form without scrollbars or clipping (including Boot ISO)."""
        active_page = self.stack.currentWidget()
        width, height = size
        if active_page is not None and active_page.layout() is not None:
            form = active_page.layout()
            form.invalidate()
            # Layout size hints take account of visible fields and Segoe UI/DPI.
            required = form.sizeHint()
            width = max(width, required.width() + 2)
            height = max(height, self.titlebar.height() + required.height() + 2)
        self.setFixedSize(width, height)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None and self.isVisible():
            bounds = screen.availableGeometry()
            x = min(max(self.x(), bounds.left()), max(bounds.left(), bounds.right() - self.width() + 1))
            y = min(max(self.y(), bounds.top()), max(bounds.top(), bounds.bottom() - self.height() + 1))
            self.move(x, y)

    def _update_boot_fields(self):
        is_boot = self.iso_type.currentData() == "boot"
        self.boot_block.setVisible(is_boot)
        if self.stack.currentWidget() == self.pack_page:
            self._resize_for_page(self.BOOT_SIZE if is_boot else self.PACK_SIZE)

    def _back_or_info(self):
        if self.stack.currentWidget() == self.home_page:
            self._show_info_popup()
        else:
            self.show_home()

    def _show_info_popup(self):
        """A small Byte Wrapper-inspired GitHub and dependency status panel."""
        if self.info_popup is None:
            popup = QFrame(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
            popup.setObjectName("InfoPopup")
            popup.setFixedWidth(236)
            layout = QVBoxLayout(popup)
            layout.setContentsMargins(11, 12, 11, 12)
            layout.setSpacing(8)
            github = QPushButton("github")
            github.setObjectName("InfoLink")
            github.setCursor(Qt.CursorShape.PointingHandCursor)
            github.clicked.connect(lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/EgeGurkan/ISOWorkshop")))
            layout.addWidget(github, alignment=Qt.AlignmentFlag.AlignCenter)
            indicators = QHBoxLayout()
            indicators.setSpacing(10)
            indicators.addStretch(1)
            for name in ("7z.exe", "7z.dll"):
                button = QPushButton(name)
                button.setObjectName("InfoDependency")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda: QDesktopServices.openUrl(
                    QUrl("https://www.7-zip.org/download.html")))
                indicators.addWidget(button)
                self.info_dependencies[name] = button
            indicators.addStretch(1)
            layout.addLayout(indicators)
            self.info_popup = popup
        for name, (found, detail) in _sevenzip_dependency_status().items():
            color = "#80d49a" if found else "#e37a80"
            hover = "#a8edbc" if found else "#ffabb1"
            self.info_dependencies[name].setStyleSheet(
                "QPushButton {background: transparent; border: none; padding: 0; "
                f"color: {color}; font-size: 12px; }} "
                f"QPushButton:hover {{color: {hover};}}"
            )
            self.info_dependencies[name].setToolTip(
                ("Found: " if found else "Missing: ") + detail + "\nClick to open the 7-Zip website."
            )
        popup = self.info_popup
        popup.adjustSize()
        corner = self.titlebar.back.mapToGlobal(QPoint(self.titlebar.back.width(), self.titlebar.back.height()))
        popup.move(corner.x() - popup.width(), corner.y())
        popup.show()
        popup.raise_()

    def show_home(self):
        if self.thread and self.thread.isRunning():
            return
        self.stack.setCurrentWidget(self.home_page)
        self.titlebar.back.setText("?")
        self.titlebar.back.setToolTip("About and 7-Zip diagnostics")
        self._resize_for_page(self.HOME_SIZE)

    def show_pack(self):
        self.stack.setCurrentWidget(self.pack_page)
        self.titlebar.back.setText("←")
        self.titlebar.back.setToolTip("Back to home")
        self.boot_block.setVisible(self.iso_type.currentData() == "boot")
        self._resize_for_page(self.BOOT_SIZE if self.iso_type.currentData() == "boot" else self.PACK_SIZE)

    def show_unpack(self):
        self.stack.setCurrentWidget(self.unpack_page)
        self.titlebar.back.setText("←")
        self.titlebar.back.setToolTip("Back to home")
        self._resize_for_page(self.UNPACK_SIZE)

    def _browse_source(self):
        path = QFileDialog.getExistingDirectory(self, "Select source folder")
        if path:
            self.source.setText(path)
            if not self.target.text().strip():
                src = Path(path)
                self.target.setText(str(src.parent / (src.name + ".iso")))
            self.volume_label.setText(re.sub(r"[^a-zA-Z0-9_]", "_", Path(path).name).upper()[:32] or "MY_ISO")
            self._check_source_depth()

    def _check_source_depth(self):
        """Auto-select recommended UDF bridge when an ISO9660 choice is too shallow."""
        if not _uses_iso9660_only_or_joliet(self.filesystem.currentData()):
            return
        source_name = self.source.text().strip()
        if not source_name:
            return
        source = Path(source_name).expanduser()
        if not source.is_dir():
            return
        try:
            too_deep = _source_reaches_iso_depth_limit(source)
        except OSError as exc:
            self.pack_status.setText(f"Directory scan error: {exc}")
            return
        if too_deep:
            suggested_index = self.filesystem.findData(RECOMMENDED_FILE_SYSTEM)
            if suggested_index >= 0:
                self.filesystem.setCurrentIndex(suggested_index)
                message = "Seven or more directory levels: switched to UDF 2.60 + Joliet."
                self.pack_status.setText(message)
                self.filesystem.setToolTip(message)

    def _browse_target(self):
        suggested = self.target.text() or str(Path.home() / "Desktop" / "My ISO.iso")
        path, _ = QFileDialog.getSaveFileName(self, "Save ISO image", suggested, "ISO Files (*.iso)")
        if path:
            self.target.setText(path if path.lower().endswith(".iso") else path + ".iso")

    def _browse_boot(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select boot image", "", "Boot Images (*.img *.bin *.ima *.com);;All files (*)")
        if path:
            self.boot_file.setText(path)

    def _browse_unpack(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select ISO image", "", "ISO Files (*.iso)")
        if path:
            self.unpack_source.setText(path)

    def _update_unpack_destination(self):
        value = self.unpack_source.text().strip()
        if not value:
            self.unpack_target.setText("")
        else:
            self.unpack_target.setText(str(_resolve_unpack_folder(Path(value))))

    def _start_pack(self):
        self.depth_scan_timer.stop()
        # Recheck immediately in case the user pasted a path and clicked Pack.
        self._check_source_depth()
        data = {
            "source": self.source.text().strip(),
            "target": self.target.text().strip(),
            "label": self.volume_label.text().strip(),
            "filesystem": self.filesystem.currentData(),
            "level": self.iso_level.currentData(),
            "type": self.iso_type.currentData(),
            "boot_path": self.boot_file.text().strip(),
            "boot_platform": self.platform.currentData(),
            "compress": self.compression.isChecked(),
        }
        if not data["source"] or not data["target"]:
            QMessageBox.warning(self, "Missing information", "Select both Source and Target.")
            return
        target = Path(data["target"])
        existing = [str(p) for p in [target, Path(str(target) + ".gz") if data["compress"] else None] if p and p.exists()]
        if existing:
            reply = QMessageBox.question(self, "Confirm overwrite", "These files already exist:\n" + "\n".join(existing) + "\n\nOverwrite these files?")
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._begin_task("pack", data)

    def _start_unpack(self):
        file = self.unpack_source.text().strip()
        if not file:
            QMessageBox.warning(self, "Missing information", "Select an ISO to unpack.")
            return
        dest = _resolve_unpack_folder(Path(file))
        if dest.exists() and dest.name.endswith("_ISO Workshop"):
            reply = QMessageBox.question(self, "Confirm overwrite", f"{dest}\n\nMatching files will be overwritten; other files will remain. Continue?")
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._begin_task("unpack", file)

    def _begin_task(self, operation: str, settings):
        if self.thread and self.thread.isRunning():
            return
        self.pack_button.setEnabled(False)
        self.unpack_button.setEnabled(False)
        self.titlebar.back.setEnabled(False)
        self.open_unpack_folder.setEnabled(False)
        self.open_pack_folder.setEnabled(False)
        progress_bar = self.pack_progress if operation == "pack" else self.unpack_progress
        status_line = self.pack_status if operation == "pack" else self.unpack_status
        progress_bar.setValue(0)
        status_line.setText("Starting operation...")
        self.thread = TaskThread(operation, settings)
        self.thread.update.connect(lambda amount, msg: self._progress_ui(progress_bar, status_line, amount, msg))
        self.thread.success.connect(self._task_success)
        self.thread.failure.connect(self._task_failed)
        self.thread.finished.connect(self._task_finished)
        self.thread.start()

    def _progress_ui(self, bar, status, amount, message):
        bar.setValue(max(0, min(100, amount)))
        status.setText(message[:110])

    def _task_success(self, outputs):
        if not outputs or not self.thread:
            return
        if self.thread.operation == "pack":
            created_iso = Path(outputs[0])
            self.last_pack_directory = created_iso.parent
            self.open_pack_folder.setEnabled(created_iso.is_file())
            self.pack_status.setText("ISO created successfully.")
            self.pack_progress.setValue(100)
        else:
            destination = Path(outputs[0])
            self.last_unpack_directory = destination
            self.open_unpack_folder.setEnabled(destination.is_dir())
            self.unpack_status.setText("ISO extracted successfully.")
            self.unpack_progress.setValue(100)

    def _task_failed(self, error):
        if self.stack.currentWidget() == self.pack_page:
            self.pack_status.setText("Operation failed.")
        else:
            self.unpack_status.setText("Operation failed.")
        QMessageBox.critical(self, "ISO operation failed", error)

    def _task_finished(self):
        self.pack_button.setEnabled(True)
        self.unpack_button.setEnabled(True)
        self.titlebar.back.setEnabled(True)

    def _open_pack_folder(self):
        if self.last_pack_directory and self.last_pack_directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_pack_directory)))

    def _open_unpack_folder(self):
        if self.last_unpack_directory and self.last_unpack_directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_unpack_directory)))

    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(self, "Operation in progress", "The application cannot close while an ISO operation is running.")
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    window = ISOApp()
    window.show()
    # Center the compact home window in the available desktop area.
    screen = window.screen() or app.primaryScreen()
    if screen:
        bounds = screen.availableGeometry()
        window.move(bounds.center() - window.rect().center())
    sys.exit(app.exec())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--export-icon":
        # Optional build-time export for PyInstaller's Windows EXE icon.
        # Running the GUI never creates or requires an external ICO file.
        data = _extract_windows_iso_icon()
        if not data:
            sys.exit("Windows ISO icon (imageres.dll, resource 5205) could not be found.")
        Path(sys.argv[2]).write_bytes(data)
        print(f"Windows ISO icon exported to: {sys.argv[2]}")
    else:
        main()
