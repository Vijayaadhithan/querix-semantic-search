from __future__ import annotations

import uvicorn

from .api import create_app
from .config import AnalyticsSettings


def main() -> None:
    settings = AnalyticsSettings.from_env()
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
