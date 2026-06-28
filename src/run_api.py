import uvicorn

from settings import API_HOST, API_LOG_LEVEL, API_PORT


if __name__ == "__main__":
    uvicorn.run(
        "api:app",
        host=API_HOST,
        port=API_PORT,
        app_dir="src",
        log_level=API_LOG_LEVEL,
        workers=1,
    )
