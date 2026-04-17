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
            _run(["git", "clone", "--filter=blob:none", authed_url, str(repo_path)])
        else:
            _run(["git", "-C", str(repo_path), "remote", "set-url", "origin", authed_url])

        _run(["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", pr.head_sha])
        _run(["git", "-C", str(repo_path), "checkout", "--force", pr.head_sha])
        _run(["git", "-C", str(repo_path), "clean", "-fdx"])

        # Scrub the token from the stored remote URL.
        _run(
            ["git", "-C", str(repo_path), "remote", "set-url", "origin", pr.clone_url],
            check=False,
        )
        return repo_path


def _inject_token(clone_url: str, token: str) -> str:
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
