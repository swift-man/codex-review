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
    """logger 인자 하나를 안전한 형태로 변환 — **컨테이너 재귀** 지원.

    `_RedactFilter` 가 record.args 를 walk 할 때 단일 항목 단위로 호출하는 헬퍼.
    문자열은 마스킹, dict / list / tuple 같은 컨테이너는 안쪽 문자열까지 재귀.

    재귀가 필요한 이유 (codex PR #18 Major 반영):
      `logger.info("x %(k)s", {"k": "secret=xxx"})` 처럼 dict args 를 쓸 때, 표준
      logging 컨벤션상 LogRecord.__init__ 가 1-tuple({dict}) 를 dict 로 unwrap 하지만,
      LogRecord 를 직접 만드는 테스트나 커스텀 어댑터·필터 체인이 있는 환경에서는
      `(dict,)` 형태 그대로 도착할 수도 있다. 재귀로 양 형태 모두 안전하게 마스킹.

    객체 repr 에 시크릿이 들어 있는 케이스 (예: `logger.info("err=%s", exc)` 에서
    exc 가 자체 __str__ 으로 토큰을 노출) 는 호출지에서 명시적으로 `redact_text(str(x))`
    를 적용하도록 한다 — 여기서 객체 repr 까지 강제 문자열화하면 비파괴 계약이 깨짐.
    """
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_arg(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_arg(v) for v in value)
    if isinstance(value, list):
        return [_redact_arg(v) for v in value]
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
    """Install logging handler with secret-redaction filter.

    Idempotency / coexistence with external loggers (uvicorn, dictConfig, pytest 등):
      - 우리 핸들러가 아직 없으면 새로 설치 (기본 경로).
      - 이미 root.handlers 가 있으면(예: uvicorn 이 먼저 dictConfig 로 구성) 그 핸들러
        들 **각각에** `_RedactFilter` 를 추가 부착 — 토큰이 포함된 stderr 가 마스킹
        없이 외부 핸들러로 흘러가는 보안 갭을 막는다 (codex PR #18 Critical 반영).
      - 같은 필터가 이미 붙어 있으면 중복 부착 안 함 (재호출 안전).

    필터는 logger 가 아니라 **handler 단위**에 부착해야 한다 — Python logging 의 propagation
    경로는 부모 logger 의 handlers 를 직접 호출하지 부모 logger 의 handle() 을 거치지
    않으므로, logger 자체에 attach 한 필터는 propagated record 에 적용되지 않는다.
    """
    root = logging.getLogger()
    redact_filter = _RedactFilter()

    def _attach_if_missing(handler: logging.Handler) -> None:
        # 같은 종류의 필터가 이미 있으면 또 붙이지 않는다 — 재호출 시 중복 누적 방지.
        if not any(isinstance(f, _RedactFilter) for f in handler.filters):
            handler.addFilter(redact_filter)

    if root.handlers:
        for handler in root.handlers:
            _attach_if_missing(handler)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(redact_filter)
    root.addHandler(handler)
    root.setLevel(level)


class DeliveryLogger(logging.LoggerAdapter[logging.Logger]):
    """Prefixes every log record with `[delivery=<id>]` for webhook tracing."""

    def process(self, msg: str, kwargs: dict[str, object]) -> tuple[str, dict[str, object]]:
        delivery = self.extra.get("delivery", "-") if self.extra else "-"
        return f"[delivery={delivery}] {msg}", kwargs


def get_delivery_logger(name: str, delivery_id: str) -> DeliveryLogger:
    return DeliveryLogger(logging.getLogger(name), {"delivery": delivery_id})
