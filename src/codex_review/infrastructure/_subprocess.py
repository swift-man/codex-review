"""공유 서브프로세스 정리 헬퍼.

`CodexCliEngine` / `GitRepoFetcher` 둘 다 `proc.kill()` 후 `await proc.wait()` 로
좀비/오펀 프로세스를 수거하는 패턴을 반복한다. 그런데 `wait()` 자체엔 상한이 없어,
드물게 커널 쪽 프로세스 수거가 지연되면:
  - preflight auth 가 서버 기동을 무한히 붙잡는다.
  - 리뷰 워커가 `CODEX_TIMEOUT_SEC` 을 훌쩍 넘겨 점유돼 큐 동시성 상한이 깨진다.

이 모듈은 한 곳에서 '확실한 종료 + 시간 제한' 을 통일된 방식으로 제공한다 (codex 리뷰 지적).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

# kill 이후 커널이 프로세스를 수거할 때까지 기다리는 기본 상한. SIGKILL 은 보통 즉시이지만
# 좀비·파이프 잔재 등 드문 경우를 대비해 넉넉한 여유를 둔다.
_DEFAULT_REAP_TIMEOUT = 5.0


async def safe_reap(
    proc: asyncio.subprocess.Process, *, timeout: float = _DEFAULT_REAP_TIMEOUT
) -> None:
    """이미 `proc.kill()` 이 호출된 하위 프로세스를 상한 있는 `wait()` 로 수거한다.

    - 타임아웃·취소·수거 중 예외는 모두 삼키고 로그만 남긴다 (호출자는 더 급한 일이 있음).
    - 이미 종료된 프로세스에 대해 호출돼도 안전 (`wait()` 가 즉시 반환).
    """
    try:
        async with asyncio.timeout(timeout):
            await proc.wait()
    except TimeoutError:
        logger.warning(
            "subprocess pid=%s did not reap within %.1fs after kill", proc.pid, timeout
        )
    except asyncio.CancelledError:
        # cleanup 경로까지 취소되면 더 기다리지 않고 전파한다.
        raise
    except Exception:  # pragma: no cover - defensive
        logger.exception("unexpected error while reaping subprocess pid=%s", proc.pid)


async def kill_and_reap(
    proc: asyncio.subprocess.Process, *, timeout: float = _DEFAULT_REAP_TIMEOUT
) -> None:
    """`kill + 상한 있는 wait` 을 한 번에. 취소·타임아웃 핸들러에서 쓰기 위한 숏컷."""
    with contextlib.suppress(ProcessLookupError):
        # race: 이미 정상 종료됐을 수 있음 — 무시하고 reap 만 수행.
        proc.kill()
    await safe_reap(proc, timeout=timeout)
