# code-RAG

on-premise 기반 code rag 입니다.

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
