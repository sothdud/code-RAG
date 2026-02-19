"""
호출 그래프 구축 (Python + C# + WPF MVVM 지원)
"""
import re

from collections import defaultdict
from typing import Dict, List, Set, Optional
from .models import CodeChunk, CallGraph

class GraphBuilder:
    def __init__(self):
        self.chunks: Dict[str, CodeChunk] = {}
        self.call_graph: Dict[str, List[str]] = defaultdict(list)
        self.reverse_call_graph: Dict[str, List[str]] = defaultdict(list)
        
        # 검색 최적화를 위한 인덱스
        self.function_index: Dict[str, List[str]] = defaultdict(list)  # func_name -> [qualified_names]
        self.property_index: Dict[str, List[str]] = defaultdict(list)  # prop_name -> [qualified_names]

    def add_chunk(self, chunk: CodeChunk):
        """청크 추가 및 인덱싱"""
        self.chunks[chunk.qualified_name] = chunk
        
        # 검색 인덱스 구축 (이름 -> 전체 경로 매핑)
        # Type은 Parser에서 지정한 tag (function, method, property 등)
        if chunk.type in ["function", "method"]:
            self.function_index[chunk.name].append(chunk.qualified_name)
        elif chunk.type == "property":
            self.property_index[chunk.name].append(chunk.qualified_name)

    def build_call_graph(self) -> CallGraph:
        """호출 그래프 생성 (언어별 로직 분기)"""
        
        for qn, chunk in self.chunks.items():
            # 언어별 처리 (tree-sitter 언어 이름 기준)
            if chunk.language == "python":
                self._process_python_calls(chunk)
            elif chunk.language == "c_sharp":
                self._process_csharp_calls(chunk)
            elif chunk.language in ["xaml", "html"]: # parser 설정에 따라 html로 잡힐 수 있음
                self._process_xaml_bindings(chunk)

        # 역방향 그래프 생성 (Called By)
        self._build_reverse_edges()

        # 청크 메타데이터 업데이트 (chunk 객체 내에 관계 정보 주입)
        self._update_chunk_metadata()

        return CallGraph(
            nodes=self.chunks,
            edges=dict(self.call_graph),
            reverse_edges=dict(self.reverse_call_graph)
        )

    # ==========================================
    # 1. Python 처리 로직
    # ==========================================
    def _process_python_calls(self, chunk: CodeChunk):
        for called_func in chunk.calls:
            target = self._resolve_python_name(called_func, chunk)
            if target:
                self.call_graph[chunk.qualified_name].append(target)

    def _resolve_python_name(self, func_name: str, context_chunk: CodeChunk) -> Optional[str]:
        # 1. 같은 파일/모듈 내 검색
        module_prefix = context_chunk.module_path.replace("/", ".").replace("\\", ".")
        candidate = f"{module_prefix}.{func_name}"
        if candidate in self.chunks:
            return candidate

        # 2. Import된 모듈에서 검색 (Alias 처리)
        if hasattr(context_chunk, 'imports') and context_chunk.imports:
             for name, full_path in context_chunk.imports.items():
                if name == func_name:
                    # full_path가 우리 코드베이스에 있는지 확인
                    if full_path in self.chunks:
                        return full_path
                    # 클래스 메서드 등 추가 로직 가능

        # 3. 전체 검색 (최후의 수단 - 이름이 유일한 경우)
        candidates = self.function_index.get(func_name, [])
        if len(candidates) == 1:
            return candidates[0]
        
        return None

    # ==========================================
    # 2. C# 처리 로직
    # ==========================================
    def _process_csharp_calls(self, chunk: CodeChunk):
        for called_func in chunk.calls:
            target = self._resolve_csharp_name(called_func, chunk)
            if target:
                self.call_graph[chunk.qualified_name].append(target)

    def _resolve_csharp_name(self, func_name: str, context_chunk: CodeChunk) -> Optional[str]:
        """
        C# 메서드 호출 해석
        """
        # 1. 같은 클래스 내 메서드 호출 (this.Method())
        parts = context_chunk.qualified_name.split('.')
        if len(parts) > 1:
            class_path = ".".join(parts[:-1]) # Namespace.ClassName
            candidate = f"{class_path}.{func_name}"
            if candidate in self.chunks:
                return candidate

        # 2. 전체 인덱스에서 검색
        candidates = self.function_index.get(func_name, [])
        
        if not candidates:
            return None
        
        if len(candidates) == 1:
            return candidates[0]

        # 3. 네임스페이스 매칭 (Heuristic)
        # 호출한 곳의 네임스페이스와 가장 비슷한 후보를 선택
        current_ns = ".".join(parts[:-1])
        
        for cand in candidates:
            cand_ns = ".".join(cand.split('.')[:-1])
            # 단순히 같은 네임스페이스/클래스에 속해 있다면 우선순위
            if cand_ns == current_ns:
                return cand
            
        # 정확히 모를 경우 첫 번째 후보 반환 (Loose Matching)
        return candidates[0]

    # ==========================================
    # 3. XAML (MVVM) 처리 로직
    # ==========================================
    def _process_xaml_bindings(self, chunk: CodeChunk):
        """
        XAML의 Binding을 분석하여 ViewModel의 Property와 연결합니다.
        """
        # 1. 정규식으로 {Binding Path=...} 또는 {Binding ...} 추출
        # 예: {Binding TestStatus} -> "TestStatus" 추출
        binding_pattern = re.compile(r"\{Binding\s+(?:Path=)?([a-zA-Z0-9_.]+)")
        
        # chunk.content(파일 내용)에서 직접 바인딩 구문을 찾습니다.
        found_bindings = binding_pattern.findall(chunk.content)
        
        if not found_bindings:
            return

        # 2. View 이름으로 ViewModel 추측 (Naming Convention)
        view_name = chunk.name.replace(".xaml", "")
        vm_name_guess = view_name + "Model"  # ObjectTestView -> ObjectTestViewModel
        
        for binding_path in found_bindings:
            # "SelectedExperiment.Task" 처럼 점으로 연결된 경우 첫 번째 객체(Property)만 타겟팅
            root_property = binding_path.split('.')[0]
            
            # Property Index에서 해당 이름 검색
            candidates = self.property_index.get(root_property, [])
            
            if not candidates:
                continue

            target_vm_prop = None
            
            # 후보 1: 이름 관례(ViewModel 이름)가 포함된 속성 우선 검색
            for cand in candidates:
                if vm_name_guess in cand:
                    target_vm_prop = cand
                    break
            
            # 후보 2: 관례가 안 맞으면, 이름이 일치하는 첫 번째 속성 연결 (Loose Coupling)
            if not target_vm_prop and candidates:
                target_vm_prop = candidates[0]

            if target_vm_prop:
                # 그래프에 관계 추가 (View -> ViewModel Property)
                self.call_graph[chunk.qualified_name].append(target_vm_prop)
    # ==========================================
    # 4. 공통 유틸리티
    # ==========================================
    def _build_reverse_edges(self):
        for caller, callees in self.call_graph.items():
            for callee in callees:
                if caller not in self.reverse_call_graph[callee]:
                    self.reverse_call_graph[callee].append(caller)

    def _update_chunk_metadata(self):
        """
        최종적으로 구축된 그래프 정보를 Chunk 객체 자체에 업데이트
        (DB 저장 시 관계 정보가 포함되도록 함)
        """
        for qn, chunk in self.chunks.items():
            chunk.called_by = self.reverse_call_graph.get(qn, [])
            # chunk.calls = self.call_graph.get(qn, []) # 필요시 원본 calls를 해석된 qn으로 교체