"""Binary file extensions to skip for text-based operations.

These files can't be meaningfully compared as text and are often large.
Ported from free-code src/constants/files.ts.
"""

BINARY_EXTENSIONS = frozenset({
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    # Videos
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    # Audio
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
    # Executables/binaries
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib",
    ".app", ".msi", ".deb", ".rpm",
    # Documents (excluded from blacklist — read_file has dedicated handlers):
    # - .pdf: pypdf / pdfplumber / Anthropic native (P3 dual path, 2026-05-12)
    # - .docx: python-docx (Q3, 2026-05-12)
    # - .pptx: python-pptx (Q3, 2026-05-12)
    # - .xlsx: openpyxl (Q4, 2026-05-12)
    # Old binary formats still blocked (no lib support yet):
    ".doc", ".xls", ".ppt",
    ".odt", ".ods", ".odp",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Bytecode / VM artifacts
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
    # Database files
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
    # Design / 3D
    ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
    # Flash
    ".swf", ".fla",
    # Lock/profiling data
    ".lockb", ".dat", ".data",
})


IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
})


IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def has_binary_extension(path: str) -> bool:
    """Check if a file path has a binary extension. Pure string check, no I/O."""
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in BINARY_EXTENSIONS


def is_image(path: str) -> bool:
    """Check if a file path has an image extension supported for inline vision."""
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in IMAGE_EXTENSIONS


def image_media_type(path: str) -> str | None:
    """Return MIME media_type for recognized image extensions, else None."""
    dot = path.rfind(".")
    if dot == -1:
        return None
    return IMAGE_MEDIA_TYPES.get(path[dot:].lower())
