import logging
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from codex_review.domain import PullRequest

logger = logging.getLogger(__name__)


class GitRepoFetcher:
    """Clones or updates a cached repo and checks out the PR head SHA."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        repo_path = self._cache_dir / pr.repo.owner / pr.repo.name
        repo_path.parent.mkdir(parents=True, exist_ok=True)

        authed_url = _inject_token(pr.clone_url, installation_token)

        if not (repo_path / ".git").exists():
            logger.info("cloning %s into %s", pr.repo.full_name, repo_path)
            # --filter=blob:none 은 partial clone 으로, 블롭을 지연 로드해 초기 clone 시간과
            # 디스크 사용량을 크게 줄인다. 리뷰엔 checkout 한 SHA 의 blob 만 실제로 받으면 된다.
            _run(["git", "clone", "--filter=blob:none", authed_url, str(repo_path)])
        else:
            # 설치 토큰은 1시간마다 바뀌므로 기존 remote URL 의 토큰을 교체해야 fetch 가 성공한다.
            _run(["git", "-C", str(repo_path), "remote", "set-url", "origin", authed_url])

        # depth=1 로 head SHA 만 얕게 받아 네트워크/디스크 비용을 최소화. 전체 히스토리가 필요
        # 없는 리뷰 용도에 충분하다.
        _run(["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", pr.head_sha])
        # --force: 이전 리뷰에서 남은 local modification 이 있어도 무시하고 대상 SHA 로 전환.
        _run(["git", "-C", str(repo_path), "checkout", "--force", pr.head_sha])
        # -fdx: 추적되지 않는 파일/디렉터리/ignore 대상까지 전부 제거해 이전 체크아웃의 잔여물이
        # 리뷰 입력에 섞이지 않도록 한다.
        _run(["git", "-C", str(repo_path), "clean", "-fdx"])

        # 디스크에 저장된 .git/config 에 토큰이 남지 않도록 remote URL 을 원래 값으로 복구.
        # 다음 리뷰에서 새 토큰으로 다시 덮어쓰므로 기능에는 영향 없음.
        _run(
            ["git", "-C", str(repo_path), "remote", "set-url", "origin", pr.clone_url],
            check=False,
        )
        return repo_path


def _inject_token(clone_url: str, token: str) -> str:
    # GitHub 공식 권장 방식: username 자리에 `x-access-token`, password 자리에 installation token.
    # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/...
    parts = urlsplit(clone_url)
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run(cmd: list[str], *, check: bool = True) -> None:
    logger.debug("git %s", " ".join(cmd[1:]))
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git command failed ({result.returncode}): {' '.join(cmd[:2])}...\n"
            f"{result.stderr.strip()}"
        )
