from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="agent_api")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
