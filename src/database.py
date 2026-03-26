import os
import uuid
import torch
import gc
from typing import Optional
from psycopg2 import pool
import psycopg2.extras
from sentence_transformers import SentenceTransformer, CrossEncoder
from dotenv import load_dotenv
from .models import CodeChunk

load_dotenv()

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

# ── SQL DDL ────────────────────────────────────────────────────────────────────
# .format() / f-string 충돌 방지: DDL 내 {} 리터럴이 많으므로 리스트로 분리
def _build_ddl(dim: int) -> list:
    """실행 순서대로 DDL 구문 리스트를 반환합니다."""
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",

        (
            "CREATE TABLE IF NOT EXISTS code_chunks ("
            "id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            "qualified_name TEXT NOT NULL UNIQUE,"          # ← UNIQUE: 중복 upsert 기준
            "name TEXT NOT NULL,"
            "type TEXT NOT NULL,"
            "content TEXT NOT NULL,"
            "summary TEXT,"
            "filepath TEXT NOT NULL,"
            "start_line INTEGER NOT NULL DEFAULT 0,"
            "language TEXT NOT NULL,"
            "module_path TEXT,"
            "docstring TEXT,"
            "calls TEXT[],"
            "called_by TEXT[],"
            "imports JSONB DEFAULT '{}'::jsonb,"
            f"embedding vector({dim})"
            ")"
        ),
        (
           "CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx "
            "ON code_chunks USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 32, ef_construction = 128)"
        ),

        "CREATE INDEX IF NOT EXISTS code_chunks_qname_idx ON code_chunks (qualified_name)",
        "CREATE INDEX IF NOT EXISTS code_chunks_name_idx  ON code_chunks (name)",
        "CREATE INDEX IF NOT EXISTS code_chunks_lang_idx  ON code_chunks (language)",
        "CREATE INDEX IF NOT EXISTS code_chunks_fp_idx    ON code_chunks (filepath)",

        (
            "CREATE TABLE IF NOT EXISTS call_edges ("
            "id SERIAL PRIMARY KEY,"
            "caller_qn TEXT NOT NULL,"
            "callee_qn TEXT NOT NULL,"
            "UNIQUE (caller_qn, callee_qn)"
            ")"
        ),

        "CREATE INDEX IF NOT EXISTS call_edges_caller_idx        ON call_edges (caller_qn)",
        "CREATE INDEX IF NOT EXISTS call_edges_callee_idx        ON call_edges (callee_qn)",
        "CREATE INDEX IF NOT EXISTS call_edges_caller_callee_idx ON call_edges (caller_qn, callee_qn)",
    ]


class VectorStore:
    def __init__(self, collection_name: Optional[str] = None):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model_path = os.getenv("EMBEDDING_MODEL_PATH", "sentence-transformers/all-MiniLM-L6-v2")

        print(f"📡 Loading Embedding Model: {model_path}")
        print(f"🚀 Acceleration Device: {device.upper()}")

        print("⚖️ Loading Reranker Model...")
        self.reranker = CrossEncoder(
            "cross-encoder/ms-marco-MiniLM-L-6-v2", device=device
        )

        self.embedder = SentenceTransformer(
            model_path, device=device, trust_remote_code=True
        )
        self.embedding_dim = self.embedder.get_sentence_embedding_dimension()
        print(f"🔍 Embedding Dimension: {self.embedding_dim}")

        self.collection = collection_name or os.getenv("COLLECTION_NAME", "code_chunks")
        self._pool = self._create_pool()
        self._ensure_schema()

    def _create_pool(self):
        dsn = os.getenv(
            "POSTGRES_DSN",
            f"host={os.getenv('POSTGRES_HOST', '192.168.0.87')} "
            f"port={os.getenv('POSTGRES_PORT', '5433')} "
            f"dbname={os.getenv('POSTGRES_DB', 'coderag')} "
            f"user={os.getenv('POSTGRES_USER', 'coderag')} "
            f"password={os.getenv('POSTGRES_PASSWORD', 'coderag')}",
        )
        try:
            p = pool.ThreadedConnectionPool(minconn=1, maxconn=8, dsn=dsn)
            print("✅ PostgreSQL connection pool ready.")
            return p
        except Exception as e:
            print(f"⚠️ PostgreSQL connection failed: {e}")
            raise

    def _conn(self):
        return self._pool.getconn()

    def _release(self, conn):
        self._pool.putconn(conn)

    def _ensure_schema(self):
        statements = _build_ddl(self.embedding_dim)
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    for stmt in statements:
                        cur.execute(stmt)
            print("✅ Schema ready.")
        except Exception as e:
            print(f"⚠️ Schema init error: {e}")
        finally:
            self._release(conn)

    def recreate_collection(self):
        print("🗑️ Recreating PostgreSQL tables...")
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS call_edges CASCADE;")
                    cur.execute("DROP TABLE IF EXISTS code_chunks CASCADE;")
                    cur.execute("DROP TABLE IF EXISTS semantic_query_cache CASCADE;")
            self._ensure_schema()
            print("✅ Tables recreated.")
        finally:
            self._release(conn)

    #임베딩 시 원본 코드가 아닌 "요약본(summary)"을 사용하도록 수정
    def _make_embed_text(self, chunk: CodeChunk) -> str:
        return f"Name: {chunk.name}\nType: {chunk.type}\nSummary: {chunk.summary}\nPath: {chunk.filepath}"

    def _upsert_single(self, chunk: CodeChunk, vector: list) -> bool:
        conn = self._conn()
        try:
            imports_json = (
                psycopg2.extras.Json(chunk.imports)
                if isinstance(chunk.imports, dict)
                else psycopg2.extras.Json({})
            )
            with conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO code_chunks
                            (id, qualified_name, name, type, content, summary,
                             filepath, start_line, language, module_path,
                             docstring, calls, called_by, imports, embedding)
                        VALUES %s
                        ON CONFLICT (qualified_name) DO UPDATE SET
                            name        = EXCLUDED.name,
                            type        = EXCLUDED.type,
                            content     = EXCLUDED.content,
                            summary     = EXCLUDED.summary,
                            filepath    = EXCLUDED.filepath,
                            start_line  = EXCLUDED.start_line,
                            language    = EXCLUDED.language,
                            module_path = EXCLUDED.module_path,
                            docstring   = EXCLUDED.docstring,
                            calls       = EXCLUDED.calls,
                            called_by   = EXCLUDED.called_by,
                            imports     = EXCLUDED.imports,
                            embedding   = EXCLUDED.embedding
                        """,
                        [(
                            str(uuid.uuid4()),
                            chunk.qualified_name, chunk.name, chunk.type,
                            chunk.content, chunk.summary, chunk.filepath, chunk.start_line,
                            chunk.language, chunk.module_path or "",
                            chunk.docstring or "", chunk.calls or [],
                            chunk.called_by or [], imports_json, vector,
                        )],
                        template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                    )
            return True
        except Exception as e:
            print(f"    ✗ DB insert failed [{chunk.name}]: {e}")
            return False
        finally:
            self._release(conn)

    def upsert_chunks(self, chunks: list[CodeChunk], batch_size: int = 100):
        total = len(chunks)
        total_batch = (total + batch_size - 1) // batch_size

        for i in range(0, total, batch_size):
            batch = chunks[i : i + batch_size]
            batch_num = i // batch_size + 1

            for chunk in batch:
                chunk.calls = [
                    c for c in chunk.calls
                    if isinstance(c, str)
                    and (
                        # __vm_context__ 마커는 길이/특수문자 제한 없이 통과
                        c.startswith("__vm_context__:")
                        or (
                            len(c) <= 200
                            and not any(ch in c for ch in [" ", "#"])
                        )
                    )
                ]

            #변경된 임베딩 텍스트 적용 (원본 코드 제외, 메타데이터+요약)
            texts = [self._make_embed_text(c) for c in batch]
            vectors = None

            try:
                vectors = self.embedder.encode(
                    texts, batch_size=32, show_progress_bar=False
                ).tolist()

                conn = self._conn()
                try:
                    with conn:
                        with conn.cursor() as cur:
                            records = []
                            for idx, chunk in enumerate(batch):
                                imports_json = (
                                    psycopg2.extras.Json(chunk.imports)
                                    if isinstance(chunk.imports, dict)
                                    else psycopg2.extras.Json({})
                                )
                                records.append((
                                    str(uuid.uuid4()), chunk.qualified_name, chunk.name, chunk.type, 
                                    chunk.content, chunk.summary, chunk.filepath, chunk.start_line, 
                                    chunk.language, chunk.module_path or "", chunk.docstring or "", 
                                    chunk.calls or [], chunk.called_by or [], imports_json, vectors[idx],
                                ))
                            psycopg2.extras.execute_values(
                                cur,
                                """
                                INSERT INTO code_chunks
                                    (id, qualified_name, name, type, content, summary,
                                     filepath, start_line, language, module_path,
                                     docstring, calls, called_by, imports, embedding)
                                VALUES %s
                                ON CONFLICT (qualified_name) DO UPDATE SET
                                    name        = EXCLUDED.name,
                                    type        = EXCLUDED.type,
                                    content     = EXCLUDED.content,
                                    summary     = EXCLUDED.summary,
                                    filepath    = EXCLUDED.filepath,
                                    start_line  = EXCLUDED.start_line,
                                    language    = EXCLUDED.language,
                                    module_path = EXCLUDED.module_path,
                                    docstring   = EXCLUDED.docstring,
                                    calls       = EXCLUDED.calls,
                                    called_by   = EXCLUDED.called_by,
                                    imports     = EXCLUDED.imports,
                                    embedding   = EXCLUDED.embedding
                                """,
                                records,
                                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                            )
                    print(f"  ✔ Saved batch {batch_num}/{total_batch}")
                finally:
                    self._release(conn)

            except RuntimeError as e:
                if "out of memory" in str(e).lower():
                    print(f"  ⚠️ Batch {batch_num} OOM → retrying one-by-one...")
                    torch.cuda.empty_cache()
                    gc.collect()
                    for chunk in batch:
                        try:
                            torch.cuda.empty_cache()
                            vec = self.embedder.encode(
                                [self._make_embed_text(chunk)], batch_size=1, show_progress_bar=False,
                            ).tolist()[0]
                            self._upsert_single(chunk, vec)
                        except Exception as oom2:
                            print(f"  ✗ [{chunk.name}] failed: {oom2}")
    # ── 벡터 검색 ────────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        top_k: int = 5,
        language: Optional[str] = None,
        filepath_keyword: Optional[str] = None,
    ) -> list[dict]:
        """코사인 유사도 기반 벡터 검색. dict 리스트를 반환합니다."""
        qvec    = self.embedder.encode(query).tolist()
        vec_str = "[" + ",".join(map(str, qvec)) + "]"

        where_clauses = []
        params        = []
        if language:
            where_clauses.append("language = %s")
            params.append(language)
        if filepath_keyword:
            where_clauses.append("filepath ILIKE %s")
            params.append(f"%{filepath_keyword}%")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"""
            SELECT
                id, qualified_name, name, type, content, summary, -- ✨ summary 추가
                filepath, start_line, language, module_path,
                docstring, calls, called_by, imports,
                1 - (embedding <=> %s::vector) AS score
            FROM code_chunks
            {where_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        params = [vec_str] + params + [vec_str, top_k]

        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ Vector search error: {e}")
            return []
        finally:
            self._release(conn)

    # ── 파일 경로 검색 ────────────────────────────────────────────────────────
    def search_by_filepath(self, filepath_keyword: str, top_k: int = 10) -> list[dict]:
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, qualified_name, name, type, content, summary, -- ✨ summary 추가
                           filepath, start_line, language, module_path,
                           docstring, calls, called_by, imports
                    FROM code_chunks
                    WHERE filepath ILIKE %s
                    LIMIT %s
                    """,
                    (f"%{filepath_keyword}%", top_k),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ File search error: {e}")
            return []
        finally:
            self._release(conn)

    # ── qualified_name으로 청크 조회 ─────────────────────────────────────────
    def retrieve_by_filenames(self, names: list[str]) -> list[dict]:
        if not names:
            return []
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, qualified_name, name, type, content, summary, -- ✨ summary 추가
                           filepath, start_line, language, module_path,
                           docstring, calls, called_by, imports
                    FROM code_chunks
                    WHERE qualified_name = ANY(%s)
                    LIMIT %s
                    """,
                    (names, len(names) + 5),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ Retrieve by names error: {e}")
            return []
        finally:
            self._release(conn)

    # ── 전체 청크 스크롤 (BM25 인덱스 구축용) ───────────────────────────────
    def scroll_all(self) -> list[dict]:
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, qualified_name, name, type, content,
                           filepath, start_line, language, module_path,
                           docstring, calls, called_by, imports
                    FROM code_chunks
                    ORDER BY qualified_name
                    """
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ Scroll error: {e}")
            return []
        finally:
            self._release(conn)

    # ── 리랭킹 ───────────────────────────────────────────────────────────────
    def rerank(self, query: str, results: list[dict], top_k: int = 10) -> list[dict]:
        if not results:
            return []
        passages = [r.get("content", "") for r in results]
        scores   = self.reranker.predict([[query, p] for p in passages])
        ranked   = sorted(zip(results, scores), key=lambda x: x[1], reverse=True)
        return [item[0] for item in ranked[:top_k]]

    # ── 호출 그래프 (call_edges 테이블) ──────────────────────────────────────
    def save_call_edges(self, edges: dict[str, list[str]]):
        if not edges:
            return
        records = [
            (caller, callee)
            for caller, callees in edges.items()
            for callee in callees
        ]
        if not records:
            return
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO call_edges (caller_qn, callee_qn)
                        VALUES %s
                        ON CONFLICT DO NOTHING
                        """,
                        records,
                    )
            print(f"  ✔ Saved {len(records)} call edges.")
        except Exception as e:
            print(f"⚠️ Save call edges error: {e}")
        finally:
            self._release(conn)

    # ── WITH RECURSIVE 기반 그래프 탐색 ──────────────────────────────────────

    def get_callees_recursive(self, qualified_name: str, max_depth: int = 3) -> list[dict]:
        """순방향 탐색: qualified_name이 직·간접적으로 호출하는 모든 함수."""
        sql = """
        WITH RECURSIVE callee_tree AS (
            SELECT e.callee_qn AS qn, 1 AS depth,
                   ARRAY[e.caller_qn, e.callee_qn] AS path
            FROM call_edges e WHERE e.caller_qn = %(seed)s
            UNION ALL
            SELECT e.callee_qn, ct.depth + 1, ct.path || e.callee_qn
            FROM call_edges e
            JOIN callee_tree ct ON e.caller_qn = ct.qn
            WHERE ct.depth < %(max_depth)s
              AND NOT (e.callee_qn = ANY(ct.path))
        )
        SELECT DISTINCT qn, MIN(depth) AS depth
        FROM callee_tree GROUP BY qn ORDER BY depth, qn;
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, {"seed": qualified_name, "max_depth": max_depth})
                return [{"qn": r[0], "depth": r[1]} for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ get_callees_recursive error: {e}")
            return []
        finally:
            self._release(conn)

    def get_callers_recursive(self, qualified_name: str, max_depth: int = 3) -> list[dict]:
        """역방향 탐색: qualified_name을 직·간접적으로 호출하는 모든 함수."""
        sql = """
        WITH RECURSIVE caller_tree AS (
            SELECT e.caller_qn AS qn, 1 AS depth,
                   ARRAY[e.callee_qn, e.caller_qn] AS path
            FROM call_edges e WHERE e.callee_qn = %(seed)s
            UNION ALL
            SELECT e.caller_qn, ct.depth + 1, ct.path || e.caller_qn
            FROM call_edges e
            JOIN caller_tree ct ON e.callee_qn = ct.qn
            WHERE ct.depth < %(max_depth)s
              AND NOT (e.caller_qn = ANY(ct.path))
        )
        SELECT DISTINCT qn, MIN(depth) AS depth
        FROM caller_tree GROUP BY qn ORDER BY depth, qn;
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, {"seed": qualified_name, "max_depth": max_depth})
                return [{"qn": r[0], "depth": r[1]} for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ get_callers_recursive error: {e}")
            return []
        finally:
            self._release(conn)

    def get_call_path(self, from_qn: str, to_qn: str, max_depth: int = 6) -> list[list[str]]:
        """from_qn → to_qn 까지의 모든 호출 경로."""
        sql = """
        WITH RECURSIVE path_search AS (
            SELECT e.callee_qn AS current,
                   ARRAY[%(from_qn)s, e.callee_qn] AS path,
                   1 AS depth
            FROM call_edges e WHERE e.caller_qn = %(from_qn)s
            UNION ALL
            SELECT e.callee_qn, ps.path || e.callee_qn, ps.depth + 1
            FROM call_edges e
            JOIN path_search ps ON e.caller_qn = ps.current
            WHERE ps.depth < %(max_depth)s
              AND NOT (e.callee_qn = ANY(ps.path))
              AND ps.current != %(to_qn)s
        )
        SELECT path FROM path_search
        WHERE current = %(to_qn)s
        ORDER BY array_length(path, 1);
        """
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, {"from_qn": from_qn, "to_qn": to_qn, "max_depth": max_depth})
                return [list(r[0]) for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ get_call_path error: {e}")
            return []
        finally:
            self._release(conn)

    def get_execution_flow(self, qualified_name: str, depth: int = 2) -> list[str]:
        """callers + callees를 WITH RECURSIVE로 조회하여 텍스트 리스트로 반환합니다."""
        callers = self.get_callers_recursive(qualified_name, max_depth=depth)
        callees = self.get_callees_recursive(qualified_name, max_depth=depth)
        flows   = []
        for item in callers:
            flows.append(f"[Caller|depth={item['depth']}] {item['qn']}")
        for item in callees:
            flows.append(f"[Callee|depth={item['depth']}] {item['qn']}")
        return flows

    def count_chunks(self) -> int:
        """code_chunks 테이블의 전체 행 수를 반환합니다 (BM25 갱신 감지용)."""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM code_chunks;")
                return cur.fetchone()[0]
        except Exception as e:
            print(f"⚠️ count_chunks error: {e}")
            return -1
        finally:
            self._release(conn)

    def search_by_exact_names(self, names: list[str]) -> list[dict]:
        """
        name 또는 qualified_name이 정확히 일치하는 청크를 SQL INDEX로 조회합니다.
        (code_chunks_name_idx, code_chunks_qname_idx 활용)
        기존 Python 루프 순회(_get_exact_function_chunks)를 대체합니다.
        """
        if not names:
            return []
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT DISTINCT
                        id, qualified_name, name, type, content, summary,
                        filepath, start_line, language, module_path,
                        docstring, calls, called_by, imports
                    FROM code_chunks
                    WHERE
                        name = ANY(%s)
                        OR qualified_name = ANY(%s)
                    ORDER BY qualified_name
                    """,
                    (names, names),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ search_by_exact_names error: {e}")
            return []
        finally:
            self._release(conn)

    def get_callees(self, qualified_name: str) -> list[str]:
        """직접 호출(depth=1)하는 callee의 qualified_name 목록."""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT callee_qn FROM call_edges WHERE caller_qn = %s",
                    (qualified_name,),
                )
                return [r[0] for r in cur.fetchall()]
        except Exception as e:
            print(f"⚠️ get_callees error: {e}")
            return []
        finally:
            self._release(conn)

    def clear_call_edges(self):
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("TRUNCATE TABLE call_edges;")
            print("✨ Call edges cleared.")
        except Exception as e:
            print(f"⚠️ clear_call_edges error: {e}")
        finally:
            self._release(conn)

    # ── CallGraph 로드 (하위 호환) ────────────────────────────────────────────
    def load_call_graph(self):
        from .models import CallGraph
        from collections import defaultdict

        print("🔄 Loading call graph from PostgreSQL...")
        all_chunks_raw = self.scroll_all()
        nodes, edges, reverse_edges = {}, defaultdict(list), defaultdict(list)

        for row in all_chunks_raw:
            qn = row.get("qualified_name", "")
            if not qn:
                continue
            chunk = CodeChunk(
                name=row.get("name", ""),
                type=row.get("type", ""),
                content=row.get("content", ""),
                filepath=row.get("filepath", ""),
                start_line=row.get("start_line", 0),
                language=row.get("language", ""),
                qualified_name=qn,
                calls=row.get("calls") or [],
                called_by=row.get("called_by") or [],
                imports=row.get("imports") or {},
                docstring=row.get("docstring", ""),
                module_path=row.get("module_path", ""),
            )
            nodes[qn] = chunk
            for callee in chunk.calls:    edges[qn].append(callee)
            for caller in chunk.called_by: reverse_edges[qn].append(caller)

        print(f"  ✔ Loaded {len(nodes)} chunks from PostgreSQL")
        return CallGraph(nodes=nodes, edges=dict(edges), reverse_edges=dict(reverse_edges))