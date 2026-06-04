"""fis-monitor — мониторинг свободных гектаров."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("fis-monitor")
except importlib.metadata.PackageNotFoundError:  # пакет не установлен (dev-окружение)
    __version__ = "dev"
