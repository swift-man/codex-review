import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from codex_review.application.review_pr_use_case import ReviewPullRequestUseCase
from codex_review.application.webhook_handler import WebhookHandler
from codex_review.config import Settings
from codex_review.infrastructure.codex_cli_engine import CodexAuthError, CodexCliEngine
from codex_review.infrastructure.file_dump_collector import FileDumpCollector
from codex_review.infrastructure.git_repo_fetcher import GitRepoFetcher
from codex_review.infrastructure.github_app_client import (
    GitHubAppClient,
    _default_tls_context,
)
from codex_review.logging_utils import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or Settings()  # type: ignore[call-arg]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # httpx.AsyncClient 는 lifespan 전 범위에서 살아 있다 — 연결 풀 재사용이 핵심.
        async with httpx.AsyncClient(
            base_url=settings.github_api_base,
            timeout=30.0,
            verify=_default_tls_context(),
        ) as http_client:
            github = GitHubAppClient(
                app_id=settings.github_app_id,
                private_key_pem=settings.load_private_key(),
                http_client=http_client,
                dry_run=settings.dry_run,
                review_model_label=settings.codex_model,
            )
            repo_fetcher = GitRepoFetcher(cache_dir=settings.repo_cache_dir)
            collector = FileDumpCollector(
                file_max_bytes=settings.file_max_bytes,
                data_file_max_bytes=settings.data_file_max_bytes,
            )
            engine = CodexCliEngine(
                binary=settings.codex_bin,
                model=settings.codex_model,
                reasoning_effort=settings.codex_reasoning_effort,
                timeout_sec=settings.codex_timeout_sec,
            )

            # 기동 시 Codex CLI 인증 상태를 선점검. 토큰 살아 있으면 로그만 남기고 통과,
            # 없으면 서버 기동 자체를 중단해 운영자가 `codex login` 을 먼저 돌리도록 유도.
            try:
                status_line = await engine.verify_auth()
                logger.info("codex auth OK — %s", status_line)
            except CodexAuthError as exc:
                logger.error("codex auth preflight failed:\n%s", exc)
                raise

            use_case = ReviewPullRequestUseCase(
                github=github,
                repo_fetcher=repo_fetcher,
                file_collector=collector,
                engine=engine,
                max_input_tokens=settings.codex_max_input_tokens,
            )
            handler = WebhookHandler(
                secret=settings.github_webhook_secret,
                github=github,
                use_case=use_case,
                concurrency=settings.review_concurrency,
                queue_maxsize=settings.review_queue_maxsize,
            )
            # 요청 핸들러가 handler 를 찾을 수 있게 app state 에 보관.
            app.state.handler = handler
            await handler.start()
            try:
                yield
            finally:
                await handler.stop()

    app = FastAPI(title="codex-review", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        handler: WebhookHandler = request.app.state.handler
        # 1) raw body 를 먼저 읽는다. 서명 검증은 원문 바이트에 대해서만 유효.
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not handler.verify_signature(signature, body):
            logger.warning("invalid webhook signature")
            return Response(status_code=401, content="invalid signature")

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return Response(status_code=400, content="invalid json")

        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "-")

        # 2) accept() 는 필터링 후 큐에 넣고 즉시 반환. GitHub 는 10초 내 응답이 없으면
        #    webhook 을 실패 처리하므로, 무거운 리뷰 작업은 워커 코루틴에서 진행.
        status, reason = await handler.accept(event, delivery, payload)
        return Response(status_code=status, content=reason)

    return app


def app_factory() -> FastAPI:
    """Uvicorn factory entry point: `uvicorn codex_review.main:app_factory --factory`."""
    return create_app()
