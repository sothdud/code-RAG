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
        # 서버 시작 시 Qdrant에 있는 모든 코드를 가져와서 BM25 인덱스를 만듭니다.
        # (코드 RAG 특성상 '정확한 변수명/함수명' 매칭을 위해 필수입니다.)
        logger.info("⏳ Initializing BM25 Index from Vector Store...")
        
        self.all_chunks = self._fetch_all_docs_from_db()
        
        if self.all_chunks:
            # 코드 특화 토크나이징 적용
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
        # 특수문자를 공백으로 치환
        clean_text = re.sub(r"[_\.\(\)\[\]\{\}\=\:\,\;\"\'\/]", " ", text)
        return clean_text.lower().split()

    def _fetch_all_docs_from_db(self):
        """Qdrant에서 모든 청크 데이터를 가져옵니다."""
        try:
            all_points = []
            offset = None
            # Qdrant scroll 기능으로 전체 데이터 순회
            while True:
                points, offset = self.db.client.scroll(
                    collection_name=self.db.collection,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False
                )
                # payload(메타데이터+content)만 저장
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
            # Qdrant 결과는 객체이므로 payload 접근
            payload = item.payload if hasattr(item, 'payload') else item
            doc_id = payload.get('qualified_name') or payload.get('filepath') # 고유 키
            
            if not doc_id: continue

            if doc_id not in fusion_scores:
                fusion_scores[doc_id] = {'doc': payload, 'score': 0}
            fusion_scores[doc_id]['score'] += 1 / (k + rank)

        # 2. BM25 결과 점수 매기기
        for rank, item in enumerate(bm25_results):
            # BM25 결과는 딕셔너리(payload) 그 자체
            doc_id = item.get('qualified_name') or item.get('filepath')
            
            if not doc_id: continue

            if doc_id not in fusion_scores:
                fusion_scores[doc_id] = {'doc': item, 'score': 0}
            fusion_scores[doc_id]['score'] += 1 / (k + rank)

        # 3. 점수 높은 순 정렬
        sorted_results = sorted(fusion_scores.values(), key=lambda x: x['score'], reverse=True)
        
        # 문서 객체만 반환
        return [item['doc'] for item in sorted_results]

    def _extract_filenames(self, query: str) -> list[str]:
        """질문에서 .py 파일명들을 추출"""
        return re.findall(r'\b[\w-]+\.py\b', query)

    def search(self, query: str, top_k: int = 5):
        """
        [하이브리드 검색 파이프라인]
        1. Keyword Search (BM25): 정확한 단어 매칭
        2. Vector Search (Dense): 의미적 유사성
        3. RRF Fusion: 순위 혼합
        4. Reranking (Cross-Encoder): [NEW!] 정밀 재검증
        5. Context Expansion: Graph DB 문맥 보강
        """
        print(f"🔎 Hybrid Searching for: '{query}'")

        # 1. 파일명 필터 확인 (기존 로직 유지)
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
        # RRF를 위해 넉넉하게(4배수) 가져옵니다.
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
        
        # 여기서 나온 후보군은 약 20~40개 정도입니다.
        candidates_before_rerank = self.reciprocal_rank_fusion(
            vector_candidates, 
            bm25_candidates, 
            k=60
        )

        # ---------------------------------------------------------
        # 🔥 [4.5] Reranking (Cross-Encoder) 추가된 부분
        # ---------------------------------------------------------
        # RRF 결과 중 상위 20개만 추려서 리랭커에게 검사 맡깁니다.
        slice_for_rerank = candidates_before_rerank[:20]
        
        if slice_for_rerank:
            print(f"  ⚖️ Reranking top {len(slice_for_rerank)} candidates...")
            
            # database.py의 rerank 메서드가 (query, results, top_k)를 받아 
            # 점수순으로 정렬하여 최종 top_k개만 돌려줍니다.
            final_results = self.db.rerank(query, slice_for_rerank, top_k=top_k)
        else:
            final_results = []

        if not final_results:
            print("  ⚠️ No candidates found.")
            return []

        # ---------------------------------------------------------
        # 5. Graph Context Expansion (기존 변수명 유지)
        # ---------------------------------------------------------
        enhanced_results = []

        for i, payload in enumerate(final_results):
            # payload는 딕셔너리 형태
            qualified_name = payload.get('qualified_name')

            context_entry = {
                "chunk": payload,
                "flow_context": [],
                "related_code": [],
            }

            if qualified_name:
                # 1. 실행 흐름 가져오기 (Graph)
                # 리랭킹으로 순위가 바뀌었으므로, 이제 진짜 중요한 상위권 녀석들만 Graph를 탑니다.
                context_entry["flow_context"] = self.graph.get_execution_flow(qualified_name, depth=2)

                # 2. 상위 3개만 Callee(호출하는 함수) 코드 가져오기
                if i < 3:
                    callees = self.graph.get_callees(qualified_name)
                    if callees:
                        context_entry['related_code'] = self.db.retrieve_by_filenames(callees)

            enhanced_results.append(context_entry)

        return enhanced_results