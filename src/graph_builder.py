"""
호출 그래프 구축 (Graph-Code의 call_processor 아이디어)
"""

from collections import defaultdict
from .models import CodeChunk, CallGraph

class GraphBuilder:
    def __init__(self):
        self.chunks: dict[str, CodeChunk] = {}
        self.call_graph: dict[str, list[str]] = defaultdict(list)
        self.reverse_call_graph: dict[str, list[str]] = defaultdict(list)

    def add_chunk(self, chunk: CodeChunk):
        """청크 추가"""
        self.chunks[chunk.qualified_name] = chunk

    def build_call_graph(self) -> CallGraph:
        """호출 그래프 생성"""
        # 1. 정방향 그래프 (누가 누구를 호출)
        for qn, chunk in self.chunks.items():
            for called_func in chunk.calls:
                # 호출된 함수의 qualified_name 찾기
                callee_qn = self._resolve_function_name(called_func, chunk)
                if callee_qn:
                    self.call_graph[qn].append(callee_qn)

        # 2. 역방향 그래프 (누가 나를 호출)
        for caller, callees in self.call_graph.items():
            for callee in callees:
                self.reverse_call_graph[callee].append(caller)

        # 3. 청크에 정보 업데이트
        for qn, chunk in self.chunks.items():
            chunk.called_by = self.reverse_call_graph.get(qn, [])

        return CallGraph(
            nodes=self.chunks,
            edges=self.call_graph,
            reverse_edges=self.reverse_call_graph
        )

    def _resolve_function_name(self, func_name: str, context_chunk: CodeChunk) -> str | None:
        """
        함수 이름 해석 (간단 버전)
        "validate_email" -> "backend.utils.validator.validate_email"
        """
        # 1. 같은 파일 내 검색
        module_prefix = context_chunk.module_path.replace("/", ".").replace("\\", ".").replace(".py", "")
        candidate = f"{module_prefix}.{func_name}"
        if candidate in self.chunks:
            return candidate

        # 2. import된 모듈에서 검색
        for import_module in context_chunk.imports:
            candidate = f"{import_module}.{func_name}"
            if candidate in self.chunks:
                return candidate

        # 3. 전체 검색 (fallback)
        for qn in self.chunks:
            if qn.endswith(f".{func_name}"):
                return qn

        return None