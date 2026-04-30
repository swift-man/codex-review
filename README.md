# codex-review

Codex OAuth(ChatGPT 구독) 기반 GitHub PR **전체 코드베이스** 리뷰 봇.
GitHub App 웹훅으로 PR 이벤트를 받아, 레포를 체크아웃하고 전체 파일을 컨텍스트로 넣어
`codex exec` CLI로 리뷰를 생성한 뒤 PR에 리뷰를 게시합니다.

## 특징

- GitHub App 설치 토큰 기반 인증 (PAT 불필요)
- diff가 아닌 **전체 코드베이스**를 컨텍스트로 사용
- Codex CLI를 `subprocess`로 호출 → 로그인된 ChatGPT 계정의 OAuth 토큰 사용 (기본 모델 `gpt-5.4`)
- 한국어 리뷰 고정 출력 (JSON 스키마 강제)
- **리뷰 4섹션**: `좋은 점` / `🔴 반드시 수정할 사항` / `💡 권장 개선 사항` / `기술 단위 코멘트(라인 고정)`
- 라인 코멘트는 **4단계 등급**(`Critical` / `Major` / `Minor` / `Suggestion`) 으로 분류되고, PR 화면에서 각 코멘트 본문 최상단에 `[Critical] …` 형태의 대괄호 접두로 표기
- 라인 고정 코멘트만 인라인으로 게시, 라인 번호 없는 지적은 `개선할 점`으로 이동
- 기초 타입(`str`/`list`/`String`/`Array` 등) 수준의 팁 배제, **Python/TypeScript/React 공식 상위 API**에 초점
- 리뷰 큐는 **제한된 동시성**으로 처리 — `REVIEW_CONCURRENCY` env(기본 `1`=직렬)로 동시 리뷰 개수 조절. 같은 저장소에 대한 checkout 은 저장소별 lock 으로 직렬화되어 작업 트리 경쟁을 방지
- 컨텍스트 예산 초과 시 **자동 diff-only 모드 fallback** — 전체 코드베이스가 `CODEX_MAX_INPUT_TOKENS` 를 넘어 변경 파일이 빠지면, PR unified patch 만 가지고 리뷰를 계속한다. diff 조차 담기지 않으면 그때서야 안내 코멘트만 게시
- SOLID — 계층 분리, `Protocol`로 의존성 역전

## 아키텍처

```
GitHub PR event
  → FastAPI /github/webhook (HMAC 검증, 202 즉시 응답)
  → asyncio.Queue (Semaphore(REVIEW_CONCURRENCY) 로 제한된 동시성)
      1. Installation Token 발급 (JWT → GitHub App API)
      2. PR 메타 / 변경 파일 조회
      3. git clone --filter=blob:none + checkout head SHA (캐시)
      4. 파일 수집 + 필터 + 우선순위 + 토큰 예산
      5. `codex exec --model ... -` 호출 (stdin: 프롬프트)
      6. JSON 파싱 → POST /pulls/{n}/reviews
```

```
src/codex_review/
├── interfaces/       # Protocol: GitHubClient, ReviewEngine, RepoFetcher, FileCollector
├── domain/           # PullRequest, ReviewResult, Finding, FileDump (frozen dataclass)
├── application/
│   ├── review_pr_use_case.py   # 오케스트레이션
│   └── webhook_handler.py      # HMAC 검증 + asyncio.Queue + Semaphore(N) 워커
├── infrastructure/
│   ├── github_app_client.py    # JWT → installation token → REST
│   ├── git_repo_fetcher.py     # clone/fetch/checkout
│   ├── file_dump_collector.py  # 필터 + 우선순위 + 토큰 예산
│   ├── codex_prompt.py         # 한국어 시스템 규칙 + 파일 직렬화
│   ├── codex_parser.py         # JSON 추출 + fallback
│   └── codex_cli_engine.py     # subprocess(codex exec) 호출
├── config.py         # pydantic-settings
└── main.py           # FastAPI 조립 (DI)
```

## 전제 조건

- macOS, Python 3.11+
- `git` 설치
- `codex` CLI가 PATH에 있고 ChatGPT 계정으로 로그인되어 있어야 함
  - 확인: `codex whoami` / `ls ~/.codex/auth.json`
- GitHub App 생성 및 대상 레포에 설치
  - 권한: Pull requests (R/W), Contents (R), Metadata (R)
  - 이벤트 구독: `Pull request`

## 설치

```bash
bash scripts/install_local_review.sh
cp scripts/local_review_env.example.sh scripts/local_review_env.sh
$EDITOR scripts/local_review_env.sh   # App ID / key path / webhook secret 입력
```

## 실행

```bash
bash scripts/run_webhook_server.sh
# → http://127.0.0.1:8000/github/webhook 수신 대기
```

테스트 웹훅 발사:
```bash
REPO_FULL_NAME=owner/repo PR_NUMBER=1 INSTALLATION_ID=1234567 \
    bash scripts/send_test_webhook.sh
```

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `GITHUB_APP_ID` | — | GitHub App ID (필수) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | PEM 경로 (또는 `GITHUB_APP_PRIVATE_KEY` inline) |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC 서명 검증용 비밀 (필수) |
| `CODEX_BIN` | `codex` | Codex CLI 실행 파일 |
| `CODEX_MODEL` | `gpt-5.4` | 모델 (`gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`, `codex-auto-review`) |
| `CODEX_REASONING_EFFORT` | `high` | `low`/`medium`/`high`/`xhigh` |
| `CODEX_MAX_INPUT_TOKENS` | `300000` | 전체 컨텍스트 토큰 예산 |
| `CODEX_TIMEOUT_SEC` | `600` | 호출 타임아웃 |
| `REPO_CACHE_DIR` | `~/.codex-review/repos` | clone 캐시 위치 |
| `FILE_MAX_BYTES` | `204800` | 단일 파일 크기 상한 |
| `DATA_FILE_MAX_BYTES` | `20000` | JSON/YAML/XML 등 모호한 확장자의 별도 상한. `package.json`·`tsconfig.json`·`pyproject.toml` 같은 화이트리스트 매니페스트는 두 파일 크기 제한 모두 면제. 단 전체 컨텍스트 예산(`CODEX_MAX_INPUT_TOKENS`) 초과 시에는 우선순위에 따라 제외될 수 있음. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 바인딩 주소 |
| `REVIEW_CONCURRENCY` | `1` | 동시 실행 리뷰 개수. `1`=직렬, `2~`=병렬. Codex 쿼터와 맞춰 조절 |
| `REVIEW_QUEUE_MAXSIZE` | `(concurrency × 10)` | 웹훅 큐 상한. 가득 차면 503 반환. 비우면 자동 계산 |
| `CODEX_ENABLE_DIFF_FALLBACK` | `true` | 예산 초과 시 diff-only 모드 자동 전환. `false` 로 내리면 "리뷰 스킵 + 안내 코멘트" 경로만 사용 |
| `GITHUB_APP_SLUG` | — | GitHub App slug (예: `codex-review-bot`). 설정 시 follow-up 기능 활성화 — `synchronize`/`reopened` 이벤트에서 봇이 단 옛 코멘트의 자동 해소 여부를 판정해 답글 + thread resolve 처리 |
| `DRY_RUN` | `0` | `1`이면 로그만 남기고 게시 안 함 |

## 동작 규칙

- 수신 이벤트: `opened`, `synchronize`, `reopened`, `ready_for_review`
- Draft PR은 skip
- 파일 필터: `.git`, `node_modules`, `dist`, `build`, `vendor`, `__pycache__` 등 디렉터리와
  `*.lock`, 바이너리, 미디어, 폰트, `package-lock.json` 등은 자동 제외
- 우선순위: 변경 파일 → `src/app/lib/pkg/...` → 기타
- 예산 초과 시:
  1. **1차 fallback** — 변경 파일이 빠졌다면 diff-only 모드로 자동 전환하여 PR 의 unified patch 만 가지고 리뷰를 수행. 리뷰 본문 최상단에 `> ⚠️ 리뷰 범위: diff-only (자동 전환)` 배지가 표시됨.
  2. **2차 fallback** — diff 조차 예산을 넘거나 GitHub 가 patch 를 전혀 돌려주지 않으면 리뷰를 **수행하지 않고** PR에 안내 코멘트만 게시.

## 리뷰 출력 (4섹션 + 4단계 라인 등급)

모델은 아래 JSON 스키마를 엄격히 따라야 합니다.

```json
{
  "summary": "...",
  "event": "COMMENT | REQUEST_CHANGES | APPROVE",
  "positives":    ["좋은 점 ..."],
  "must_fix":     ["반드시 수정할 사항 (파일/모듈 단위) ..."],
  "improvements": ["권장 개선 사항 (파일/모듈 단위) ..."],
  "comments": [
    {
      "path": "src/x.py",
      "line": 42,
      "severity": "critical | major | minor | suggestion",
      "body": "기술 단위 코멘트 (라인 고정)"
    }
  ]
}
```

- `positives` / `must_fix` / `improvements` → PR 리뷰 본문 해당 섹션으로 렌더
- `comments` → GitHub 인라인 리뷰 코멘트로 라인에 붙음 (**line / severity 필수**)
- 인라인 코멘트 본문은 등급별 대괄호 접두와 함께 게시됨 — 예: `[Critical] None 체크 누락…`

### 라인 코멘트 등급 기준

| 등급 | 의미 | 대표 예시 |
|---|---|---|
| `critical` | 반드시 막아야 하는 문제 | 장애 가능성 · 데이터 손실 · 보안 취약점 · 크래시 |
| `major` | 머지 전에 고치는 게 좋은 문제 | 버그 가능성 · 예외 처리 누락 · 상태 불일치 · 동시성 · 큰 테스트 누락 |
| `minor` | 당장 큰 문제는 아니지만 개선 가치 있음 | 가독성 · 중복 · 네이밍 · 구조 개선 |
| `suggestion` | 선택 제안 | 대안 · 취향 차이 · 리팩터링 아이디어 |

`critical` / `major` 가 하나라도 있으면 리뷰 이벤트는 자동으로 `REQUEST_CHANGES` 로 승격됩니다(모델이 `COMMENT` 로 내려도 서버가 덮어씀).

### 기술 단위 코멘트의 취향

기초 수준(`str`/`list`/`String`/`Array`/`JSON.parse` 등)의 팁은 제외하도록 프롬프트에서 강제합니다.
대신 아래와 같은 **공식 상위 API** 사용을 지적/권장하도록 유도합니다.

- **Python**: `collections.Counter/defaultdict/deque`, `itertools`, `functools.cache/singledispatch`,
  `dataclasses(frozen=True, slots=True)`, `typing.Protocol/TypedDict/assert_never`,
  `pathlib.Path`, `contextlib.ExitStack/suppress`, `asyncio.TaskGroup`, `enum.StrEnum`, pydantic `BaseModel`
- **TypeScript**: `Map/Set/WeakMap/WeakRef`, 유틸리티 타입(`Readonly/Pick/Omit/ReturnType/Awaited`),
  `satisfies`, discriminated union exhaustiveness, `structuredClone`, `AbortController`,
  `Promise.allSettled/any`, `Intl.*`, Zod `z.infer`
- **React**: `useMemo/useCallback`의 올바른 의존성, `useReducer`, `useId`,
  `useSyncExternalStore`, `startTransition`, `useDeferredValue`, `Suspense/ErrorBoundary`,
  `use()` hook, `useFormStatus/useOptimistic`, React Query `queryKey/staleTime`

모두 `src/codex_review/infrastructure/codex_prompt.py`에서 조정 가능합니다.

## 테스트

```bash
.venv/bin/pytest tests/unit -q
```

## 배포 (선택)

- `deploy/nginx-codex-review.conf`: 리버스 프록시 예시 (TLS, `/github/webhook`, `/healthz`)
- macOS LaunchAgent / `tmux`+`nohup` 등으로 서버 상주
- GitHub App 웹훅 URL을 nginx 엔드포인트로 지정, 시크릿을 `GITHUB_WEBHOOK_SECRET`과 일치시킬 것

## 참고

설계상 `/Users/m4_25/develop/codereview`의 운영 패턴(HMAC 검증 → 202 즉시 응답 →
백그라운드 처리, 구조화 로그, App JWT 흐름)을 재사용했습니다.
본 프로젝트는 MLX 로컬 모델 대신 **Codex CLI + OAuth**를 사용하고,
**diff가 아닌 전체 코드**를 컨텍스트로 넣는다는 점에서 차이가 있습니다.
