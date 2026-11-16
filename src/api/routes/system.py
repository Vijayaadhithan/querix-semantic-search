"""System search HTTP routes."""

from fastapi import FastAPI, Header, HTTPException

from api.context import SearchRequestContext
from api.contracts import (
    HealthResponse,
    SearchRequest,
    SearchResponse,
)
from api.readiness import readiness_response


def register_system_routes(application: FastAPI, context: SearchRequestContext) -> None:
    @application.get("/api/v1/ready", tags=["system"])
    def ready():
        return readiness_response(application)

    @application.get("/api/v1/live", tags=["system"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get(
        "/api/v1/health",
        response_model=HealthResponse,
        tags=["system"],
    )
    def health(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> HealthResponse:
        try:
            return context.resolve_service(
                x_api_key,
                apply_rate_limit=False,
            ).health()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post(
        "/api/v1/search",
        response_model=SearchResponse,
        tags=["search"],
    )
    def search(
        request: SearchRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> SearchResponse:
        return context.execute_search(request, x_api_key)

    @application.get(
        "/api/v1/{company_endpoint}/health",
        response_model=HealthResponse,
        tags=["company"],
    )
    def company_health(
        company_endpoint: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> HealthResponse:
        try:
            return context.resolve_service(
                x_api_key,
                apply_rate_limit=False,
                company_endpoint=company_endpoint,
            ).health()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
