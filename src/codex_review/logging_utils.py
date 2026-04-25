import logging
import re
import sys
from typing import Any

# `key=value` 또는 `key: Bearer value` 형태의 평문 시크릿 패턴.
# `\S+(?:\s+\S+)?` 로 최대 2개 토큰을 흡수해 `authorization=Bearer ghs_xxx` 같은 두-단어
# 자격증명도 끝까지 마스킹. 한 토큰만 있으면 첫 번째만 매칭.
_SECRET_PATTERN = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*\S+(?:\s+\S+)?"
)

# `https://user:token@host/path` 형태의 URL 자격증명 패턴 (codex stderr 등에 GitHub
# token URL 이 섞일 수 있음). git_repo_fetcher 와 같은 룰을 logging 계층에서도 적용해
# logger.error("...%s", stderr) 같은 호출에서도 마스킹되도록 한다.
_URL_USERINFO_PATTERN = re.compile(
    r"(?P<scheme>https?)://[^/@\s]+:[^/@\s]+@"
)


def redact_text(text: str) -> str:
    """문자열 안의 두 종류 시크릿(URL userinfo, 평문 key=value) 을 모두 마스킹.

    URL 패턴을 **먼저** 적용 — `https://x-access-token:xxx@host` 형태에선 URL 의 일부
    로서 `token:xxx` 가 등장하므로, SECRET 패턴이 먼저 동작하면 URL 구조가 깨져
    URL 마스킹이 더 이상 안 먹는다. 순서: URL → SECRET 이 안전한 조합.

    공개 헬퍼: 로그 외에도 PR 코멘트·예외 메시지 등 **사용자에게 노출되는 다른 경로**
    (예: `logger.exception` 의 traceback 안의 exc 문자열, GitHub PR 본문 게시) 에서도
    사용한다 — 단일 진실의 마스킹 룰을 공유해야 누락 표면이 안 생김 (codex PR #18 Critical).
    """
    text = _URL_USERINFO_PATTERN.sub(r"\g<scheme>://***@", text)
    text = _SECRET_PATTERN.sub(r"\1=***", text)
    return text


# 내부 호환용 alias — 기존 `_redact_text` 호출자도 그대로 동작.
_redact_text = redact_text


def _redact_arg(value: Any) -> Any:
    """logger 인자 하나를 안전한 형태로 변환. 문자열이 아니면 그대로 반환 — 비파괴.

    포맷 시 `%s` 가 호출하는 `__str__` 결과까지 마스킹하려면 string 으로 강제 변환해야
    하지만 그건 원본 객체를 잃는다. 보수적으로 str 만 처리하고, 객체 repr 에 시크릿이
    들어 있는 케이스(드뭄) 는 호출지에서 명시적으로 문자열화하도록 한다.
    """
    if isinstance(value, str):
        return _redact_text(value)
    return value


class _RedactFilter(logging.Filter):
    """로그 레코드의 `msg` 와 `args` 양쪽에서 시크릿을 마스킹.

    이전 구현은 `record.msg` 만 봤기 때문에 `logger.error("rc=%d ...:\\n%s", rc, stderr)`
    처럼 stderr 전체를 args 로 넘기면 마스킹이 우회됐다 (codex PR #18 Major 지적).
    args 가 tuple/list 면 각 원소를, dict 면 각 value 를 walk 한다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        if record.args:
            # `LogRecord.__init__` 는 args 가 1-tuple 로 감싼 Mapping 일 때 자동으로
            # 내부 dict 로 unwrap 하므로, 일반 호출 경로(`logger.info(msg, {dict})`) 의
            # record.args 는 dict 로 도착한다. 직접 LogRecord 를 만드는 테스트나 단일
            # 값 args 케이스도 함께 커버.
            if isinstance(record.args, dict):
                # %(key)s 포맷 — 값만 마스킹.
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, (tuple, list)):
                record.args = tuple(_redact_arg(a) for a in record.args)
            else:
                record.args = _redact_arg(record.args)
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(_RedactFilter())
    root.addHandler(handler)
    root.setLevel(level)


class DeliveryLogger(logging.LoggerAdapter[logging.Logger]):
    """Prefixes every log record with `[delivery=<id>]` for webhook tracing."""

    def process(self, msg: str, kwargs: dict[str, object]) -> tuple[str, dict[str, object]]:
        delivery = self.extra.get("delivery", "-") if self.extra else "-"
        return f"[delivery={delivery}] {msg}", kwargs


def get_delivery_logger(name: str, delivery_id: str) -> DeliveryLogger:
    return DeliveryLogger(logging.getLogger(name), {"delivery": delivery_id})
