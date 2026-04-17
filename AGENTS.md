# AGENTS.md

이 문서는 본 저장소에서 작업하는 AI 에이전트 및 개발자를 위한 가이드입니다.

## 프로젝트 개요

- 언어: Python 3.11+
- 설계 원칙: SOLID
- 패키지 관리: `uv` 또는 `pip` + `venv`
- 테스트: `pytest`
- 타입 체크: `mypy` (strict)
- 린트/포맷: `ruff`, `black`

## 핵심 개발 원칙 (SOLID)

모든 코드는 SOLID 원칙을 준수해야 합니다.

### 1. SRP (Single Responsibility Principle)
- 하나의 클래스/모듈은 하나의 변경 이유만 가진다.
- 함수는 한 가지 일만 수행한다.
- 파일당 하나의 주요 클래스를 권장한다.

### 2. OCP (Open/Closed Principle)
- 확장에는 열려 있고, 수정에는 닫혀 있어야 한다.
- 새로운 동작은 기존 코드 수정이 아닌 추가(상속/조합)로 구현한다.
- `abc.ABC`를 이용한 추상 클래스 또는 `typing.Protocol`을 적극 활용한다.

### 3. LSP (Liskov Substitution Principle)
- 하위 타입은 상위 타입을 대체할 수 있어야 한다.
- 하위 클래스는 상위 클래스의 계약(contract)을 깨지 않는다.
- 예외를 더 강하게 던지거나 반환 타입을 좁히지 않는다.

### 4. ISP (Interface Segregation Principle)
- 사용하지 않는 메서드에 의존하도록 강요하지 않는다.
- 크고 일반적인 인터페이스 대신, 작고 구체적인 인터페이스를 여러 개 만든다.
- `Protocol`을 사용해 역할별로 인터페이스를 분리한다.

### 5. DIP (Dependency Inversion Principle)
- 상위 모듈은 하위 모듈에 의존하지 않는다. 둘 다 추상화에 의존한다.
- 구체 클래스가 아닌 `Protocol`/`ABC`에 의존한다.
- 의존성은 생성자 주입(Constructor Injection)을 기본으로 한다.

## 프로젝트 구조

```
.
├── src/
│   └── <package_name>/
│       ├── __init__.py
│       ├── domain/          # 엔티티, 값 객체, 도메인 서비스
│       ├── application/     # 유스케이스, 애플리케이션 서비스
│       ├── infrastructure/  # 외부 시스템 어댑터 (DB, HTTP 등)
│       └── interfaces/      # Protocol / ABC 정의
├── tests/
│   ├── unit/
│   └── integration/
├── pyproject.toml
├── README.md
└── AGENTS.md
```

계층 간 의존 방향: `interfaces` ← `domain` ← `application` ← `infrastructure`
(상위 계층이 하위 계층의 추상화에만 의존)

## 코딩 규칙

- 모든 public 함수/메서드에 타입 힌트를 붙인다.
- 가변 전역 상태 금지. 의존성은 주입한다.
- `print` 대신 `logging` 모듈을 사용한다.
- 예외는 구체적으로 처리한다. `except Exception:` 지양.
- 매직 넘버/문자열은 상수 또는 Enum으로 추출한다.
- 함수 길이는 50줄, 클래스는 200줄을 넘지 않도록 한다.

## 예시 패턴

```python
from typing import Protocol
from dataclasses import dataclass

# interfaces/
class UserRepository(Protocol):
    def find_by_id(self, user_id: str) -> "User | None": ...
    def save(self, user: "User") -> None: ...

# domain/
@dataclass(frozen=True)
class User:
    id: str
    email: str

# application/ — DIP: 추상화에 의존
class RegisterUserUseCase:
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    def execute(self, user_id: str, email: str) -> User:
        user = User(id=user_id, email=email)
        self._repo.save(user)
        return user

# infrastructure/ — 구체 구현
class InMemoryUserRepository:
    def __init__(self) -> None:
        self._store: dict[str, User] = {}

    def find_by_id(self, user_id: str) -> User | None:
        return self._store.get(user_id)

    def save(self, user: User) -> None:
        self._store[user.id] = user
```

## 테스트 규칙

- 모든 새 기능에는 단위 테스트를 작성한다.
- 테스트는 AAA (Arrange-Act-Assert) 구조를 따른다.
- 외부 의존성은 Fake/Stub으로 대체한다 (DIP의 이점 활용).
- 커버리지 목표: 핵심 도메인 90%+, 전체 80%+.

## 커밋 및 PR

- 커밋 메시지: `<type>: <subject>` (예: `feat: add user registration use case`)
- PR 생성 전: `ruff check`, `mypy`, `pytest` 모두 통과해야 한다.
- 한 PR은 하나의 논리적 변경만 포함한다.

## 에이전트가 지켜야 할 것

1. 기존 코드 스타일과 구조를 먼저 파악한 뒤 수정한다.
2. SOLID 원칙을 위반하는 변경은 거부하거나 리팩터링을 제안한다.
3. 불필요한 추상화는 피한다 (YAGNI). 단, 계층 경계는 유지한다.
4. 변경 범위를 최소화한다. 요청되지 않은 리팩터링은 하지 않는다.
5. 새 파일 생성보다 기존 파일 수정을 우선한다.
