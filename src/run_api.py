import uvicorn

from settings import API_HOST, API_PORT


if __name__ == "__main__":
    uvicorn.run(
        "api:app",
        host=API_HOST,
        port=API_PORT,
        app_dir="src",
        workers=1,
    )
