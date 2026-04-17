import logging
import re
import sys

_SECRET_PATTERN = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*\S+"
)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SECRET_PATTERN.sub(r"\1=***", record.msg)
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
