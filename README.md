# 🔍 Code-RAG

코드베이스를 분석하고 자연어로 질문할 수 있는 **멀티 언어 코드 RAG 시스템**입니다.  
Python, C#, XAML, C/C++ 소스코드를 AST로 파싱하고, pgvector + BM25 하이브리드 검색과 호출 그래프(Call Graph)를 결합하여 LLM이 정확한 코드 컨텍스트를 기반으로 답변합니다.

---

## ✨ 주요 기능

- **멀티 언어 AST 파싱** — Python, C#, XAML, C/C++ (tree-sitter 기반)
- **하이브리드 검색** — pgvector HNSW 코사인 벡터 검색 + BM25 키워드 검색 + RRF 퓨전
- **Call Graph 탐색** — PostgreSQL WITH RECURSIVE CTE로 함수 호출 관계 추적
- **LLM 요약 생성** — 파싱 시 각 청크에 대해 자동 요약 생성 (vLLM / Ollama 호환)
- **에러 자동 진단** — Python/C# 트레이스백 파싱 → 코드 검색 → LLM 분석
- **증분 인제스트** — MD5 파일 해시 기반 변경 감지로 수정된 파일만 재처리
- **FQN 해석** — AST 기반 Fully Qualified Name 자동 추출 (Python, C#, C++)

---

## 🏗️ 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│                         ingest.py                               │
│   파일 스캔 → ASTParser → GraphBuilder → VectorStore(pgvector)  │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │  PostgreSQL + pgvector │
                    │  - code_chunks 테이블  │
                    │  - call_edges 테이블   │
                    └─────────┬──────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                      search_engine.py                          │
│   Vector Search + BM25 + RRF Fusion + Graph Context           │
└─────────────────────────────┬─────────────────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │     agent.py       │
                    │  Summary → Full    │
                    │  Content → LLM     │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │      llm.py        │
                    │  vLLM / Ollama     │
                    │  Streaming 답변    │
                    └────────────────────┘
```

---

## 📁 프로젝트 구조

```
code-rag/
├── ingest.py                 # 코드 인제스트 CLI 진입점
├── docker-compose.yaml       # PostgreSQL(pgvector) + Ollama
├── .env                      # 환경 변수 설정
└── src/
    ├── agent.py              # 검색 → LLM 답변 파이프라인
    ├── search_engine.py      # 하이브리드 검색 엔진 (Vector + BM25 + Graph)
    ├── database.py           # PostgreSQL / pgvector VectorStore
    ├── graph_store.py        # Call Graph 인터페이스 (PostgreSQL 위임)
    ├── graph_builder.py      # 호출 관계 그래프 빌더
    ├── parser.py             # AST 파서 (멀티 언어)
    ├── call_extractor.py     # 함수 호출 관계 추출 (tree-sitter)
    ├── fqn_resolver.py       # Fully Qualified Name 해석기
    ├── language_specs.py     # 언어별 tree-sitter 쿼리 명세
    ├── models.py             # 데이터 모델 (CodeChunk, CallGraph)
    ├── context_builder.py    # LLM 컨텍스트 생성
    ├── prompts.py            # 시스템 프롬프트 및 컨텍스트 포매터
    ├── llm.py                # LLM 클라이언트 (vLLM OpenAI-compatible)
    ├── error_diagnostic_engine.py  # 에러 진단 엔진
    ├── error_diagnostic_api.py     # 에러 진단 FastAPI 라우터
    ├── source_extraction.py  # 파일에서 소스 라인 추출
    └── path_utils.py         # 경로 필터링 유틸리티
```

---

## ⚙️ 설치 및 실행

### 1. 사전 요구사항

- Docker & Docker Compose
- Python 3.11+
- GPU 서버 (vLLM 또는 Ollama용, CPU도 가능)

### 2. 인프라 실행

```bash
docker-compose up -d
```

PostgreSQL(pgvector)이 포트 `5433`에, Ollama가 `11434`에 실행됩니다.

### 3. Python 환경 설정

```bash
pip install -r requirements.txt
```

주요 의존성:

```
fastapi
pgvector
psycopg2-binary
rank-bm25
tree-sitter
tree-sitter-languages
sentence-transformers
loguru
rich
python-dotenv
requests
beautifulsoup4
```

### 4. 환경 변수 설정

`.env` 파일을 프로젝트 루트에 생성합니다 (`_env` 파일 참고):

```env
# PostgreSQL
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
POSTGRES_DB=coderag
POSTGRES_USER=coderag
POSTGRES_PASSWORD=coderag

# LLM (vLLM OpenAI-compatible API)
VLLM_API_BASE=http://localhost:8000/v1
LLM_MODEL=Qwen/Qwen3-30B-A3B
AGENT_MODEL=Qwen/Qwen3-30B-A3B

# 임베딩 모델
EMBEDDING_MODEL_PATH=jinaai/jina-embeddings-v2-base-en

# 분석할 코드 경로
SOURCE_CODE_PATH=./your-project/

# 전체 재인덱싱 강제 여부
FORCE_REINDEX=false
```

### 5. 코드 인제스트

```bash
python ingest.py
```

변경된 파일만 자동으로 감지하여 처리합니다. 전체 재인덱싱이 필요한 경우:

```bash
FORCE_REINDEX=true python ingest.py
```

인제스트 흐름:

1. **파일 스캔** — MD5 해시 기반 변경 감지
2. **AST 파싱** — 함수/클래스/메서드 단위로 청크 생성
3. **LLM 요약** — 각 청크에 대한 2줄 요약 자동 생성
4. **폴더 요약** — 폴더 단위 아키텍처 요약 청크 생성
5. **벡터 DB 업서트** — pgvector에 임베딩 저장
6. **콜 그래프 동기화** — call_edges 테이블 업데이트

---

## 🔎 검색 파이프라인

`SmartSearchEngine.search()` 는 다음 순서로 동작합니다:

1. **BM25 자동 갱신** — DB 청크 수 변화 감지 시 자동 재빌드
2. **정확 매칭** — 함수/클래스명 SQL INDEX 조회 (최상위 배치)
3. **벡터 검색** — pgvector HNSW 코사인 유사도
4. **BM25 키워드 검색** — snake_case/CamelCase 토크나이저 적용
5. **RRF 퓨전** — Reciprocal Rank Fusion으로 두 결과 병합
6. **Reranking** — CrossEncoder 기반 재랭킹
7. **Graph Context 확장** — WITH RECURSIVE CTE로 호출 관계 추가

---

## 🌐 에러 진단 API

FastAPI 기반 에러 진단 엔드포인트를 제공합니다.

| 엔드포인트 | 설명 |
|---|---|
| `POST /api/diagnostic/analyze-error` | 트레이스백 전체 분석 (LLM 포함) |
| `POST /api/diagnostic/quick-check` | 빠른 파싱만 (LLM 없음) |
| `POST /api/diagnostic/search-similar-errors` | 유사 코드 위치 검색 |
| `POST /api/diagnostic/batch-analyze` | 최대 10개 에러 배치 처리 |
| `GET /api/diagnostic/stats` | 시스템 상태 확인 |

**지원 언어:** Python 트레이스백, C# 스택 트레이스 자동 감지

---

## 🗣️ 지원 언어

| 언어 | 확장자 | 파싱 방식 | FQN 해석 |
|---|---|---|---|
| Python | `.py` | tree-sitter | 모듈 경로 기반 |
| C# | `.cs` | tree-sitter | 네임스페이스.클래스.메서드 |
| XAML | `.xaml` | tree-sitter + Binding 정규식 | 네임스페이스.View |
| C/C++ | `.cpp`, `.h`, `.hpp`, `.c` | tree-sitter + 정규식 fallback | 네임스페이스.클래스.메서드 |

---

## 🔧 주요 설정

| 환경 변수 | 설명 | 기본값 |
|---|---|---|
| `POSTGRES_HOST` | PostgreSQL 호스트 | `localhost` |
| `POSTGRES_PORT` | PostgreSQL 포트 | `5433` |
| `VLLM_API_BASE` | vLLM API 엔드포인트 | `http://localhost:8000/v1` |
| `LLM_MODEL` | 답변 생성 모델 | `Qwen/Qwen3-30B-A3B` |
| `AGENT_MODEL` | Tool Calling 모델 | `Qwen/Qwen3-30B-A3B` |
| `EMBEDDING_MODEL_PATH` | 임베딩 모델 | `jinaai/jina-embeddings-v2-base-en` |
| `SOURCE_CODE_PATH` | 분석할 코드 경로 | `./` |
| `FORCE_REINDEX` | 전체 재인덱싱 강제 | `false` |
| `VLLM_TIMEOUT` | LLM 요청 타임아웃(초) | `500` |

---

## 🚫 인덱싱 제외 경로

`path_utils.py` 에서 다음 경로는 자동으로 제외됩니다:

- `.git`, `.venv`, `venv`, `env`, `__pycache__`
- `node_modules`, `dist`, `build`, `.idea`, `.vscode`
- `site-packages`, `egg-info`
- `.`으로 시작하는 숨김 파일/폴더
- `.xaml.cs` 파일 (`.xaml`에 병합 처리)

---

## 📝 라이선스

MIT
