from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SUPPORTED_DOCUMENT_TYPES = ("docx", "pptx", "txt", "md", "csv", "xlsx")


class FolderPickerError(RuntimeError):
    """The native folder picker could not be opened."""


def pick_project_directory() -> Path | None:
    """Open a native folder picker without accepting a path from page content."""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Choose a local Blot project folder")'
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FolderPickerError("The macOS folder picker could not be opened.") from exc
        if result.returncode != 0:
            if "User canceled" in result.stderr or "-128" in result.stderr:
                return None
            raise FolderPickerError("The macOS folder picker failed.")
        selected = result.stdout.rstrip("\r\n")
    elif sys.platform == "win32":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dialog.Description = 'Choose a local Blot project folder'; "
            "$dialog.ShowNewFolderButton = $true; "
            "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [Console]::WriteLine($dialog.SelectedPath) } else { exit 2 }"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FolderPickerError("The Windows folder picker could not be opened.") from exc
        if result.returncode == 2:
            return None
        if result.returncode != 0:
            raise FolderPickerError("The Windows folder picker failed.")
        selected = result.stdout.rstrip("\r\n")
    else:
        raise FolderPickerError("Native project folder selection is supported on macOS and Windows only.")

    if not selected:
        raise FolderPickerError("The folder picker returned an empty selection.")
    return Path(selected).expanduser()


def pick_document_files() -> list[Path] | None:
    """Open a native multi-file picker restricted to planned input formats."""
    extensions = ";".join(f"*.{extension}" for extension in SUPPORTED_DOCUMENT_TYPES)
    if sys.platform == "darwin":
        type_list = ", ".join(f'"{extension}"' for extension in SUPPORTED_DOCUMENT_TYPES)
        script = (
            f"set selectedFiles to choose file with prompt \"Choose documents to add to Blot\" "
            f"of type {{{type_list}}} with multiple selections allowed\n"
            "set selectedPaths to {}\n"
            "repeat with selectedFile in selectedFiles\n"
            "set end of selectedPaths to POSIX path of selectedFile\n"
            "end repeat\n"
            "set AppleScript's text item delimiters to linefeed\n"
            "return selectedPaths as text"
        )
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FolderPickerError("The macOS document picker could not be opened.") from exc
        if result.returncode != 0:
            if "User canceled" in result.stderr or "-128" in result.stderr:
                return None
            raise FolderPickerError("The macOS document picker failed.")
        selected = result.stdout.splitlines()
    elif sys.platform == "win32":
        filter_value = f"Supported documents ({extensions})|{extensions}"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$dialog.Filter = '{filter_value}'; "
            "$dialog.Multiselect = $true; "
            "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ foreach ($file in $dialog.FileNames) { [Console]::WriteLine($file) } } else { exit 2 }"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FolderPickerError("The Windows document picker could not be opened.") from exc
        if result.returncode == 2:
            return None
        if result.returncode != 0:
            raise FolderPickerError("The Windows document picker failed.")
        selected = result.stdout.splitlines()
    else:
        raise FolderPickerError("Native document selection is supported on macOS and Windows only.")

    paths = [Path(item).expanduser() for item in selected if item]
    if not paths:
        raise FolderPickerError("The document picker returned an empty selection.")
    return paths
