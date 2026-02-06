import os

# 1. 메모리 파편화 방지

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import uuid
import torch
import gc
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance, Filter, FieldCondition, MatchText
from sentence_transformers import SentenceTransformer, CrossEncoder
from dotenv import load_dotenv
from .models import CodeChunk
from qdrant_client.models import MatchAny # ì´ importê°€ ê¼­ ìžˆì–´ì•¼ í•©ë‹ˆë‹¤

load_dotenv()


class VectorStore:
    def __init__(self):
        self.client = QdrantClient(url=os.getenv("QDRANT_URL"))
        self.collection = os.getenv("COLLECTION_NAME")

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model_path = os.getenv("EMBEDDING_MODEL_PATH", "jinaai/jina-embeddings-v2-base-en")

        print(f"ðŸ“¡ Loading Embedding Model: {model_path}")
        print(f"ðŸš€ Acceleration Device: {device.upper()}")

        print("âš–ï¸ Loading Reranker Model...")
        self.reranker = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2',
                                     device=device)
        
        self.embedder = SentenceTransformer(model_path, device=device, trust_remote_code=True)
        self.embedding_dim = self.embedder.get_sentence_embedding_dimension()
        print(f"ðŸ“ Embedding Dimension: {self.embedding_dim}")

        # ì´ˆê¸° ì—°ê²° í…ŒìŠ¤íŠ¸
        try:
            self._ensure_collection_exists()
        except Exception as e:
            print(f"âš ï¸ DB Connection Error: {e}")

    def _ensure_collection_exists(self):
        """[v1.7.3 í˜¸í™˜] ì»¬ë ‰ì…˜ ëª©ë¡ì„ ì§ì ‘ ì¡°íšŒí•˜ì—¬ í™•ì¸"""
        try:
            response = self.client.get_collections()
            exists = any(c.name == self.collection for c in response.collections)

            if not exists:
                print(f"ðŸ“¦ Creating new collection: {self.collection}")
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=VectorParams(size=self.embedding_dim, distance=Distance.COSINE)
                )
        except Exception as e:
            print(f"âš ï¸ Error checking collection: {e}")

    def recreate_collection(self):
        """[v1.7.3 í˜¸í™˜] ì»¬ë ‰ì…˜ ì‚­ì œ í›„ ìž¬ìƒì„±"""
        print(f"ðŸ—‘ï¸ Recreating Qdrant collection: {self.collection}")
        
        try:
            # 1. í˜„ìž¬ ëª©ë¡ ê°€ì ¸ì˜¤ê¸°
            response = self.client.get_collections()
            exists = any(c.name == self.collection for c in response.collections)

            # 2. ìžˆìœ¼ë©´ ì‚­ì œ
            if exists:
                self.client.delete_collection(self.collection)
                print("  âœ“ Existing collection deleted.")
            
            # 3. ìƒˆë¡œ ìƒì„±
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.embedding_dim, distance=Distance.COSINE)
            )
            print("  âœ“ New collection created.")

        except Exception as e:
            print(f"âš ï¸ Error recreating collection: {e}")

    def upsert_chunks(self, chunks: list[CodeChunk], batch_size: int = 5):
        total = len(chunks)

        for i in range(0, total, batch_size):
            batch = chunks[i:i + batch_size]
            points = []
            texts_to_embed = []

            for chunk in batch:
                clean_calls = []
                for call in chunk.calls:
                    # 1. ë¬¸ìžì—´ì¸ì§€ í™•ì¸ (í˜¹ì‹œ ëª¨ë¥¼ ì˜¤ë¥˜ ë°©ì§€)
                    if not isinstance(call, str): continue
                    
                    # 2. í•„í„°ë§ ì¡°ê±´
                    # - ê³µë°±ì´ ìžˆë‹¤? -> ì£¼ì„ì¼ í™•ë¥  99% (í•¨ìˆ˜ëª…ì—” ê³µë°± ì—†ìŒ)
                    # - #, <, >, =, : ê°™ì€ íŠ¹ìˆ˜ë¬¸ìžê°€ ìžˆë‹¤? -> ì½”ë“œ íŒŒíŽ¸ìž„
                    # - ê¸¸ì´ê°€ ë„ˆë¬´ ê¸¸ë‹¤(50ìž ì´ìƒ)? -> ë¬¸ìž¥ì¼ í™•ë¥  ë†’ìŒ
                    if any(char in call for char in [' ', '#', '<', '>', '=', ':']):
                        continue
                    if len(call) > 50:
                        continue
                        
                    clean_calls.append(call)
                chunk.calls = clean_calls
                
                meta_parts = [
                    f"Name: {chunk.name}",
                    f"Type: {chunk.type}",
                    f"Language: {chunk.language}"
                ]
                if chunk.filepath: meta_parts.append(f"File Path: {chunk.filepath}")
                if chunk.module_path: meta_parts.append(f"Module: {chunk.module_path}")
                
                doc_part = f"\nDescription: {chunk.docstring}" if chunk.docstring else ""
                meta_str = "\n".join(meta_parts)
                embed_text = f"{meta_str}{doc_part}\n\nCode Content:\n{chunk.content}"
                texts_to_embed.append(embed_text)

            try:
                vectors = self.embedder.encode(texts_to_embed).tolist()

                for idx, chunk in enumerate(batch):
                    points.append(PointStruct(
                        id=str(uuid.uuid4()),
                        vector=vectors[idx],
                        payload={
                            "name": chunk.name,
                            "type": chunk.type,
                            "content": chunk.content,
                            "filepath": chunk.filepath,
                            "start_line": chunk.start_line,
                            "language": chunk.language,
                            "qualified_name": chunk.qualified_name,
                            "calls": chunk.calls,
                            "called_by": chunk.called_by,
                            "imports": chunk.imports,
                            "module_path": chunk.module_path,
                            "docstring": chunk.docstring,
                        }
                    ))

                if points:
                    self.client.upsert(
                        collection_name=self.collection,
                        points=points,
                        wait=True
                    )
                    print(f"  âœ“ Saved batch {i // batch_size + 1}/{(total + batch_size - 1) // batch_size}")

            except Exception as e:
                print(f"  âœ— Batch {i // batch_size + 1} failed: {e}")

            finally:
                if 'vectors' in locals(): del vectors
                if 'points' in locals(): del points
                torch.cuda.empty_cache()
                gc.collect()

    def search(self, query: str, top_k: int = 5, query_filter=None):
        query_vector = self.embedder.encode(query).tolist()
        return self.client.search(
            collection_name=self.collection,
            query_vector=query_vector,
            query_filter=query_filter,
            limit=top_k
        )

    def search_by_filepath(self, filepath_keyword: str, top_k: int = 10):
        try:
            results = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=Filter(must=[FieldCondition(key="filepath", match=MatchText(text=filepath_keyword))]),
                limit=top_k,
                with_payload=True,
                with_vectors=False
            )
            return results[0]
        except Exception as e:
            print(f"âš ï¸ File search error: {e}")
            return []

    def hybrid_search(self, query: str, filepath_keyword: str = None, top_k: int = 5):
        query_vector = self.embedder.encode(query).tolist()
        filter_conditions = None
        if filepath_keyword:
            filter_conditions = Filter(must=[FieldCondition(key="filepath", match=MatchText(text=filepath_keyword))])

        try:
            return self.client.search(
                collection_name=self.collection,
                query_vector=query_vector,
                query_filter=filter_conditions,
                limit=top_k
            )
        except Exception as e:
            print(f"âš ï¸ Hybrid search error: {e}")
            return []

    def load_call_graph(self):
        from .models import CallGraph
        from collections import defaultdict

        print("ðŸ”„ Loading call graph from Qdrant...")
        try:
            all_points = []
            offset = None
            while True:
                result = self.client.scroll(
                    collection_name=self.collection,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False
                )
                points, offset = result
                all_points.extend(points)
                if offset is None: break
            print(f"  âœ“ Loaded {len(all_points)} functions from Qdrant")
        except Exception as e:
            print(f"âš ï¸ Error loading from Qdrant: {e}")
            return CallGraph(nodes={}, edges={}, reverse_edges={})

        nodes = {}
        edges = defaultdict(list)
        reverse_edges = defaultdict(list)

        for point in all_points:
            payload = point.payload
            qn = payload.get('qualified_name', '')
            if not qn: continue

            chunk = CodeChunk(
                name=payload.get('name', ''),
                type=payload.get('type', ''),
                content=payload.get('content', ''),
                filepath=payload.get('filepath', ''),
                start_line=payload.get('start_line', 0),
                language=payload.get('language', ''),
                qualified_name=qn,
                calls=payload.get('calls', []),
                called_by=payload.get('called_by', []),
                imports=payload.get('imports', []),
                module_path=payload.get('module_path', ''),
            )
            nodes[qn] = chunk
            for callee in chunk.calls: edges[qn].append(callee)
            for caller in chunk.called_by: reverse_edges[qn].append(caller)

        return CallGraph(nodes=nodes, edges=dict(edges), reverse_edges=dict(reverse_edges))

    def rerank(self, query: str, results: list, top_k: int = 10):
        if not results: return []

        passages = []
        for res in results:
            content = res.payload.get('content', '') if hasattr(res, 'payload') else res['content']
            passages.append(content)

        model_inputs = [[query, passage] for passage in passages]
        scores = self.reranker.predict(model_inputs)
        ranked_results = sorted(zip(results, scores), key=lambda x: x[1], reverse=True)
        return [item[0] for item in ranked_results[:top_k]]

    def retrieve_by_filenames(self, names: list[str]):
        if not names: return []
        try:
            results, _ = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=Filter(must=[FieldCondition(key="qualified_name", match=MatchAny(any=names))]),
                limit=len(names) + 5,
                with_payload=True, with_vectors=False
            )
            return [res.payload for res in results if res.payload]
        except Exception as e:
            print(f"âš ï¸ Error retrieving by names: {e}")
            return []