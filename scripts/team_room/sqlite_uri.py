"""Read-only SQLite URI for a local file, safe for Windows extended (\\\\?\\D:\\...) and UNC paths.

Path.resolve().as_uri() turns an extended path into file://%3F/..., which SQLite rejects with 'invalid uri authority'.
Stored settings and bindings keep their extended paths; only the URI is normalised here.
"""
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

_EXTENDED_UNC = '\\\\?\\UNC\\'
_EXTENDED = '\\\\?\\'


def strip_extended(text):
    """A path string without a Windows extended-length prefix (UNC form becomes \\\\server\\share)."""
    if text.upper().startswith(_EXTENDED_UNC):
        return '\\\\' + text[len(_EXTENDED_UNC):]
    if text.startswith(_EXTENDED) or text.startswith('\\\\.\\'):
        return text[4:]
    return text


def uri_for(text):
    text = strip_extended(text)
    if re.match(r'^[A-Za-z]:[\\/]', text) or text.startswith('\\\\'):
        return PureWindowsPath(text).as_uri() + '?mode=ro'
    return PurePosixPath(text).as_uri() + '?mode=ro'


def readonly_uri(path):
    return uri_for(str(Path(path).resolve()))
