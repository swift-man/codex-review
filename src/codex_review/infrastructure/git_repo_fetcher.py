import asyncio
import logging
import re
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from codex_review.domain import PullRequest

from ._subprocess import kill_and_reap

logger = logging.getLogger(__name__)

# `https://user:token@host/...` 형태의 자격 증명을 포함한 URL 을 찾아 userinfo 만 마스킹.
# 예외 메시지·stderr 텍스트 안에서도 토큰을 노출하지 않도록 전역 마스킹에 쓴다.
_URL_WITH_USERINFO = re.compile(r"(?P<scheme>https?)://[^/@\s]+:[^/@\s]+@")


class _RepoLockRegistry:
    """owner/repo → `asyncio.Lock` — WeakValueDictionary 기반.

    `popitem` LRU 방식은 잠긴 락까지 evict 될 위험이 있다. WeakValueDictionary 는 누군가
    강한 참조(예: `async with lock`)를 쥔 동안은 GC 되지 않고, 사용자가 없어지면 자동 수거.
    → 메모리 누적 방지 + 활성 락의 배타성 보존 두 목표를 모두 달성.
    """

    def __init__(self) -> None:
        # WeakValueDictionary 의 value 참조가 모두 사라지면 자동 삭제.
        self._locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )

    def get(self, key: str) -> asyncio.Lock:
        # asyncio 는 싱글스레드라 get ↔ setdefault 사이 선점이 없다 — atomic.
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


class GitRepoFetcher:
    """Async git wrapper. session() 컨텍스트 안에서만 작업 트리가 기대 SHA 로 고정된다.

    같은 저장소에 대한 다른 session 은 이전 session 의 블록이 끝날 때까지 대기한다 —
    `git fetch/checkout/clean` 뿐 아니라 블록 내의 파일 읽기까지 완전히 커버.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._repo_locks = _RepoLockRegistry()

    @asynccontextmanager
    async def session(
        self, pr: PullRequest, installation_token: str
    ) -> AsyncIterator[Path]:
        async with self._repo_locks.get(pr.repo.full_name):
            repo_path = await self._checkout_locked(pr, installation_token)
            yield repo_path

    async def _checkout_locked(self, pr: PullRequest, installation_token: str) -> Path:
        repo_path = self._cache_dir / pr.repo.owner / pr.repo.name
        repo_path.parent.mkdir(parents=True, exist_ok=True)

        authed_url = _inject_token(pr.clone_url, installation_token)

        # 토큰 주입 시점 → cleanup 까지 전체를 try/finally 로 묶는다. clone 직후나 set-url
        # 직후 `CancelledError` 가 들어와도 finally 가 실행되어 `.git/config` 에 토큰이 남지
        # 않는다. clone 이 실패하면 `.git` 자체가 없으니 cleanup 은 그 경우를 체크한다.
        try:
            if not (repo_path / ".git").exists():
                logger.info("cloning %s into %s", pr.repo.full_name, repo_path)
                # --filter=blob:none 은 partial clone — 블롭을 지연 로드해 초기 clone 속도·디스크 절약.
                await _run(["git", "clone", "--filter=blob:none", authed_url, str(repo_path)])
            else:
                # 설치 토큰은 1시간마다 바뀌므로 기존 remote URL 의 토큰을 교체해야 fetch 성공.
                await _run(
                    ["git", "-C", str(repo_path), "remote", "set-url", "origin", authed_url]
                )

            # depth=1 로 head SHA 만 얕게 받아 네트워크/디스크 비용 최소화.
            await _run(
                ["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", pr.head_sha]
            )
            # --force: 이전 리뷰에서 남은 local modification 이 있어도 무시하고 대상 SHA 로 전환.
            await _run(["git", "-C", str(repo_path), "checkout", "--force", pr.head_sha])
            # -fdx: 추적 안되는 파일/디렉터리/ignore 대상까지 전부 제거.
            await _run(["git", "-C", str(repo_path), "clean", "-fdx"])
        finally:
            # clone 이 실패하면 .git 자체가 없을 수 있음 — 존재할 때만 복구.
            if (repo_path / ".git").exists():
                await _run(
                    ["git", "-C", str(repo_path), "remote", "set-url", "origin", pr.clone_url],
                    check=False,
                )
        return repo_path


def _inject_token(clone_url: str, token: str) -> str:
    # GitHub 권장: username=x-access-token, password=installation token.
    parts = urlsplit(clone_url)
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _mask_token_in_url(value: str) -> str:
    """`https://x-access-token:<tok>@host/path` → `https://***@host/path` (단일 URL)."""
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and parts.username:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))
    return value


def _mask_tokens_in_text(text: str) -> str:
    """텍스트에 섞여 있는 모든 `scheme://user:token@` 패턴을 `scheme://***@` 로 치환.

    git 이 stderr 에 `fatal: unable to access 'https://x-access-token:TOKEN@github...'`
    처럼 URL 을 그대로 출력하는 경우가 있어, 예외 메시지·로그에 붙이기 전 반드시 마스킹.
    """
    return _URL_WITH_USERINFO.sub(r"\g<scheme>://***@", text)


async def _run(cmd: list[str], *, check: bool = True) -> None:
    # 기록 직전 토큰이 포함된 URL 을 마스킹한다 (URL 형태가 아니면 원본 그대로).
    masked_args = [_mask_token_in_url(arg) for arg in cmd[1:]]
    logger.debug("git %s", " ".join(masked_args))
    # stdout 은 소비하지 않으므로 DEVNULL 로 — 파이프 버퍼링/메모리 오버헤드 제거.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # `communicate()` 가 취소되면 생성된 git 하위 프로세스가 orphan 으로 남아,
        # 토큰이 포함된 remote URL 로 백그라운드 통신을 계속할 수 있다.
        # 공용 `kill_and_reap` 헬퍼로 수거 대기에도 상한을 두고 취소를 전파.
        await kill_and_reap(proc)
        raise
    if check and proc.returncode != 0:
        # git 이 stderr 에 토큰을 포함한 URL 을 실어 보낼 수 있다 (fatal: unable to access ...).
        # 예외 메시지·예외 추적 시스템에 토큰이 남지 않도록 stderr 도 통째로 마스킹.
        safe_stderr = _mask_tokens_in_text(stderr.decode(errors="replace").strip())
        raise RuntimeError(
            f"git command failed ({proc.returncode}): "
            f"{' '.join(masked_args[:2])}...\n{safe_stderr}"
        )
