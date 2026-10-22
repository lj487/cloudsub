"""Local subtitles from user-controlled media sources."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cloudsub-local")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0+unknown"
