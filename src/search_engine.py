"""
Memgraph 기반의 하이브리드 검색 엔진
Vector(의미 검색) + BM25(키워드 검색) + Graph(맥락/흐름 검색)
"""
import re
from rank_bm25 import BM25Okapi
from qdrant_client.models import Filter, FieldCondition, MatchText
from .database import VectorStore
from .graph_store import GraphStore
from loguru import logger

class SmartSearchEngine:

    def __init__(self, vector_store: VectorStore, graph_store: GraphStore):
        self.db = vector_store
        self.graph = graph_store
        
        # ---------------------------------------------------------
        # 🚀 [NEW] BM25 인덱스 초기화 (메모리 로드)
        # ---------------------------------------------------------
        logger.info("⏳ Initializing BM25 Index from Vector Store...")
        
        self.all_chunks = self._fetch_all_docs_from_db()
        
        if self.all_chunks:
            tokenized_corpus = [self._tokenize_code(doc.get('content', '')) for doc in self.all_chunks]
            self.bm25 = BM25Okapi(tokenized_corpus)
            logger.success(f"✅ BM25 Index Ready! (Loaded {len(self.all_chunks)} chunks)")
        else:
            logger.warning("⚠️ No data found in Qdrant. BM25 will be disabled until data is ingested.")
            self.bm25 = None

    def _tokenize_code(self, text: str):
        """
        코드용 토크나이저: snake_case, CamelCase, 특수문자 등을 분리하여 인덱싱
        예: "INVALID_KEY" -> ["invalid", "key"]
        """
        clean_text = re.sub(r"[_\.\(\)\[\]\{\}\=\:\,\;\"\'\/]", " ", text)
        return clean_text.lower().split()

    def _fetch_all_docs_from_db(self):
        """Qdrant에서 모든 청크 데이터를 가져옵니다."""
        try:
            all_points = []
            offset = None
            while True:
                points, offset = self.db.client.scroll(
                    collection_name=self.db.collection,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False
                )
                for p in points:
                    if p.payload:
                        all_points.append(p.payload)
                
                if offset is None:
                    break
            return all_points
        except Exception as e:
            logger.error(f"⚠️ Failed to fetch docs for BM25: {e}")
            return []

    def reciprocal_rank_fusion(self, vector_results, bm25_results, k=60):
        """
        🧬 RRF 알고리즘: 두 검색 결과의 순위를 합산하여 재정렬
        Score = 1 / (k + rank)
        """
        fusion_scores = {}

        # 1. Vector 결과 점수 매기기
        for rank, item in enumerate(vector_results):
            payload = item.payload if hasattr(item, 'payload') else item
            doc_id = payload.get('qualified_name') or payload.get('filepath')
            
            if not doc_id: continue

            if doc_id not in fusion_scores:
                fusion_scores[doc_id] = {'doc': payload, 'score': 0}
            fusion_scores[doc_id]['score'] += 1 / (k + rank)

        # 2. BM25 결과 점수 매기기
        for rank, item in enumerate(bm25_results):
            doc_id = item.get('qualified_name') or item.get('filepath')
            
            if not doc_id: continue

            if doc_id not in fusion_scores:
                fusion_scores[doc_id] = {'doc': item, 'score': 0}
            fusion_scores[doc_id]['score'] += 1 / (k + rank)

        # 3. 점수 높은 순 정렬
        sorted_results = sorted(fusion_scores.values(), key=lambda x: x['score'], reverse=True)
        
        return [item['doc'] for item in sorted_results]

    def _extract_filenames(self, query: str) -> list[str]:
        """질문에서 .py 파일명들을 추출"""
        return re.findall(r'\b[\w-]+\.py\b', query)
    
    def _extract_function_names(self, query: str) -> list[str]:
        """
        질문에서 함수명/클래스명을 추출
        예: "predict_tree_klarf가 뭐해?" -> ["predict_tree_klarf"]
        """
        # Python 함수명 패턴: snake_case, camelCase
        pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]{1,49})\b'
        candidates = re.findall(pattern, query)
        
        # 일반 영단어 제외
        common_words = {'what', 'does', 'how', 'why', 'where', 'when', 
                       'function', 'class', 'method', 'file', 'code', 'this',
                       'that', 'the', 'is', 'are', 'do', 'can', 'will', 'from'}
        
        function_names = []
        for c in candidates:
            # 최소 3자 이상이거나 언더스코어 포함
            if (len(c) >= 3 or '_' in c) and c.lower() not in common_words:
                function_names.append(c)
        
        return function_names
    
    def _get_exact_function_chunks(self, function_names: list[str]) -> list:
        """
        ⭐ 함수명과 정확히 일치하는 청크들을 직접 가져오기
        (검색 순위와 무관하게 반드시 포함시키기 위함)
        """
        if not function_names or not self.all_chunks:
            return []
        
        exact_matches = []
        seen_qns = set()  # 중복 방지
        
        for chunk in self.all_chunks:
            chunk_name = chunk.get('name', '')
            qn = chunk.get('qualified_name', '')
            
            if qn in seen_qns:
                continue
            
            # 함수명이 정확히 일치하거나 qualified_name 끝부분이 일치
            for target_name in function_names:
                if chunk_name == target_name or qn.endswith(f'.{target_name}'):
                    exact_matches.append(chunk)
                    seen_qns.add(qn)
                    logger.info(f"  ✅ Exact match found: {qn}")
                    break
        
        return exact_matches

    def search(self, query: str, top_k: int = 5):
        """
        [하이브리드 검색 파이프라인]
        1. ⭐ Exact Function Name Matching (NEW!)
        2. Keyword Search (BM25): 정확한 단어 매칭
        3. Vector Search (Dense): 의미적 유사성
        4. RRF Fusion: 순위 혼합
        5. Reranking (Cross-Encoder): 정밀 재검증
        6. Context Expansion: Graph DB 문맥 보강
        """
        print(f"🔎 Hybrid Searching for: '{query}'")

        # ---------------------------------------------------------
        # ⭐ 0. 함수명 정확 매칭 (NEW!)
        # ---------------------------------------------------------
        target_functions = self._extract_function_names(query)
        exact_function_chunks = []
        
        if target_functions:
            print(f"  🎯 Detected function names: {target_functions}")
            exact_function_chunks = self._get_exact_function_chunks(target_functions)
            print(f"  ✅ Found {len(exact_function_chunks)} exact matches")
        
        # 1. 파일명 필터 확인
        target_files = self._extract_filenames(query)
        search_filters = None
        if target_files:
            print(f"  📂 Filter by files: {target_files}")
            search_filters = Filter(
                should=[
                    FieldCondition(
                        key="filepath", 
                        match=MatchText(text=fname)
                    ) for fname in target_files
                ]
            )

        # ---------------------------------------------------------
        # 2. Vector Search (Dense)
        # ---------------------------------------------------------
        vector_candidates = self.db.search(query, top_k=top_k * 4, query_filter=search_filters)
        
        # ---------------------------------------------------------
        # 3. Keyword Search (BM25)
        # ---------------------------------------------------------
        bm25_candidates = []
        if self.bm25:
            tokenized_query = self._tokenize_code(query)
            bm25_candidates = self.bm25.get_top_n(tokenized_query, self.all_chunks, n=top_k * 4)

        # ---------------------------------------------------------
        # 4. RRF (Reciprocal Rank Fusion) 결합
        # ---------------------------------------------------------
        print(f"  🧬 Fusing: Vector({len(vector_candidates)}) + BM25({len(bm25_candidates)})")
        
        candidates_before_rerank = self.reciprocal_rank_fusion(
            vector_candidates, 
            bm25_candidates, 
            k=60
        )

        # ---------------------------------------------------------
        # ⭐ 4.5 정확히 일치하는 함수를 최상위에 삽입 (NEW!)
        # ---------------------------------------------------------
        if exact_function_chunks:
            # 중복 제거: exact match가 이미 후보에 있으면 제거
            exact_qns = {c.get('qualified_name') for c in exact_function_chunks}
            candidates_before_rerank = [
                c for c in candidates_before_rerank 
                if c.get('qualified_name') not in exact_qns
            ]
            
            # 정확 매칭을 맨 앞에 추가
            candidates_before_rerank = exact_function_chunks + candidates_before_rerank
            print(f"  🎯 Exact matches promoted to top!")

        # ---------------------------------------------------------
        # 5. Reranking (Cross-Encoder)
        # ---------------------------------------------------------
        slice_for_rerank = candidates_before_rerank[:20]
        
        if slice_for_rerank:
            print(f"  ⚖️ Reranking top {len(slice_for_rerank)} candidates...")
            
            # ⭐ 개선: 정확 매칭 함수는 rerank에서도 높은 우선순위 유지
            if exact_function_chunks:
                # 정확 매칭은 무조건 포함
                non_exact = [c for c in slice_for_rerank if c not in exact_function_chunks]
                # rerank는 나머지에만 적용
                reranked_rest = self.db.rerank(query, non_exact, top_k=max(1, top_k - len(exact_function_chunks)))
                final_results = exact_function_chunks + reranked_rest
                # top_k 개수 맞추기
                final_results = final_results[:top_k]
            else:
                final_results = self.db.rerank(query, slice_for_rerank, top_k=top_k)
        else:
            final_results = []

        if not final_results:
            print("  ⚠️ No candidates found.")
            return []

        # ---------------------------------------------------------
        # 6. Graph Context Expansion
        # ---------------------------------------------------------
        enhanced_results = []

        for i, payload in enumerate(final_results):
            qualified_name = payload.get('qualified_name')

            context_entry = {
                "chunk": payload,
                "flow_context": [],
                "related_code": [],
            }

            if qualified_name:
                # 1. 실행 흐름 가져오기 (Graph)
                context_entry["flow_context"] = self.graph.get_execution_flow(qualified_name, depth=2)

                # 2. 상위 3개만 Callee 코드 가져오기
                if i < 3:
                    callees = self.graph.get_callees(qualified_name)
                    if callees:
                        context_entry['related_code'] = self.db.retrieve_by_filenames(callees)

            enhanced_results.append(context_entry)

        return enhanced_results