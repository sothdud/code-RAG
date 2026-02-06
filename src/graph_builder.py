import os
import re
from collections import defaultdict
from pathlib import Path
from .models import CodeChunk, CallGraph

class GraphBuilder:
    def __init__(self, repo_root: str = None):
        self.chunks: dict[str, CodeChunk] = {}
        self.ui_chunks: dict[str, CodeChunk] = {}
        self.call_graph: dict[str, list[str]] = defaultdict(list)
        self.reverse_call_graph: dict[str, list[str]] = defaultdict(list)
        
        # 파일 레벨 UI 변수 추적
        self.file_ui_vars: dict[str, dict] = {}
        
        # 클래스 레벨 UI 매핑
        self.class_to_ui_map: dict[str, str] = {}
        
        # [추가] 레포 루트 경로
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    def add_chunk(self, chunk: CodeChunk):
        """청크 추가"""
        if chunk.type == "ui_widget":
            self.ui_chunks[chunk.qualified_name] = chunk
            self.chunks[chunk.qualified_name] = chunk
            print(f"  📦 Added UI widget: {chunk.qualified_name}")
        else:
            self.chunks[chunk.qualified_name] = chunk
            
            # Step 1: 파일 레벨 UI 변수 감지
            self._detect_ui_variable(chunk)
            
            # Step 2: 클래스가 UI를 상속하는지 확인
            if chunk.type == "class":
                self._detect_ui_inheritance(chunk)

    def _detect_ui_variable(self, chunk: CodeChunk):
        """
        파일 최상단의 UI 변수 감지
        예: ui = uic.loadUiType("./auto_labeling/auto_labeling.ui")[0]
        """
        filepath = chunk.filepath
        if filepath in self.file_ui_vars:
            return
        
        # 패턴들
        patterns = [
            r'(\w+)\s*=\s*uic\.loadUiType\([\'"]([^\'"]+\.ui)[\'"]\)\[0\]',
            r'(\w+)\s*=\s*uic\.loadUiType\([\'"]([^\'"]+\.ui)[\'"]\)',
        ]
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                file_content = f.read()
            
            for pattern in patterns:
                match = re.search(pattern, file_content)
                if match:
                    var_name = match.group(1)  # "ui"
                    ui_path = match.group(2)   # "./auto_labeling/auto_labeling.ui"
                    
                    # [개선] 절대/상대 경로 모두 처리
                    ui_filename = self._resolve_ui_filename(ui_path, filepath)
                    
                    self.file_ui_vars[filepath] = {
                        "ui_var_name": var_name,
                        "ui_file": ui_filename
                    }
                    print(f"  📋 Detected UI variable: {var_name} → {ui_filename} in {os.path.basename(filepath)}")
                    break
        except Exception as e:
            print(f"  ⚠️ Failed to read file {filepath}: {e}")

    def _resolve_ui_filename(self, ui_path: str, py_filepath: str) -> str:
        """
        UI 파일 경로를 정규화
        
        예:
        - "./auto_labeling/auto_labeling.ui" → "auto_labeling.ui"
        - "../ui/main.ui" → "main.ui"
        - "C:/project/ui/dialog.ui" → "dialog.ui"
        """
        # 절대 경로인 경우
        if os.path.isabs(ui_path):
            return os.path.basename(ui_path)
        
        # 상대 경로인 경우 - Python 파일 기준으로 해석
        py_dir = os.path.dirname(py_filepath)
        full_ui_path = os.path.normpath(os.path.join(py_dir, ui_path))
        
        # 실제 파일이 존재하는지 확인
        if os.path.exists(full_ui_path):
            # repo_root 기준 상대 경로로 변환
            try:
                rel_path = os.path.relpath(full_ui_path, self.repo_root)
                # 경로 구분자를 '.'으로 변환하지 않고 파일명만 반환
                return os.path.basename(rel_path)
            except ValueError:
                # relpath 실패 시 basename만
                return os.path.basename(full_ui_path)
        
        # 파일이 없으면 basename만
        return os.path.basename(ui_path)

    def _detect_ui_inheritance(self, chunk: CodeChunk):
        """
        클래스가 UI를 상속하는지 확인
        예: class AutoLabelingDialog(QDialog, QWidget, ui):
        """
        filepath = chunk.filepath
        
        if filepath not in self.file_ui_vars:
            return
        
        ui_var_name = self.file_ui_vars[filepath]["ui_var_name"]
        ui_filename = self.file_ui_vars[filepath]["ui_file"]
        
        # 클래스 정의에서 해당 변수를 상속하는지 확인
        pattern = rf'class\s+{re.escape(chunk.name)}\s*\([^)]*\b{ui_var_name}\b[^)]*\):'
        
        if re.search(pattern, chunk.content):
            self.class_to_ui_map[chunk.qualified_name] = ui_filename
            print(f"  🔗 UI Inheritance: {chunk.qualified_name} inherits {ui_filename}")

    def build_call_graph(self) -> CallGraph:
        """호출 그래프 생성"""
        print(f"\n🕸️ Building Call Graph...")
        print(f"  📊 Total chunks: {len(self.chunks)}")
        print(f"  📦 UI widgets: {len(self.ui_chunks)}")
        print(f"  🔗 UI mappings: {len(self.class_to_ui_map)}")
        
        for qn, chunk in self.chunks.items():
            if chunk.type == "ui_widget": 
                continue

            # 이 함수가 속한 클래스가 UI를 상속받았는지 확인
            linked_ui_file = self._get_ui_file_for_chunk(chunk)

            for called_func in chunk.calls:
                # A. 일반 함수 호출 해결
                callee_qn = self._resolve_function_name(called_func, chunk)
                if callee_qn:
                    self.call_graph[qn].append(callee_qn)
                
                # B. UI 위젯 바인딩 해결
                if linked_ui_file:
                    # [중요] UI 위젯 QN 생성 방식 확인
                    ui_widget_qn = f"{linked_ui_file}.{called_func}"
                    
                    if ui_widget_qn in self.ui_chunks:
                        self.call_graph[qn].append(ui_widget_qn)
                        print(f"    ✅ UI Widget Call: {qn} → {ui_widget_qn}")

        # 역방향 그래프 구축
        for caller, callees in self.call_graph.items():
            for callee in callees:
                self.reverse_call_graph[callee].append(caller)

        for qn, chunk in self.chunks.items():
            chunk.called_by = self.reverse_call_graph.get(qn, [])

        print(f"  ✅ Graph built: {len(self.call_graph)} edges")
        
        return CallGraph(
            nodes=self.chunks,
            edges=dict(self.call_graph),
            reverse_edges=dict(self.reverse_call_graph)
        )

    def _get_ui_file_for_chunk(self, chunk: CodeChunk) -> str | None:
        """주어진 청크가 속한 클래스의 UI 파일 찾기"""
        qn = chunk.qualified_name
        parts = qn.split('.')
        
        # 1. 메서드인 경우 상위 클래스 찾기
        if len(parts) >= 3:
            for i in range(len(parts) - 1, 0, -1):
                potential_class_qn = '.'.join(parts[:i])
                if potential_class_qn in self.class_to_ui_map:
                    return self.class_to_ui_map[potential_class_qn]
        
        # 2. 청크 자체가 클래스인 경우
        if qn in self.class_to_ui_map:
            return self.class_to_ui_map[qn]
        
        return None

    def _resolve_function_name(self, func_name: str, context_chunk: CodeChunk) -> str | None:
        """함수 이름 해석"""
        module_prefix = context_chunk.module_path.replace("/", ".").replace("\\", ".").replace(".py", "")
        
        candidate = f"{module_prefix}.{func_name}"
        if candidate in self.chunks:
            return candidate

        if func_name in context_chunk.imports:
            full_path = context_chunk.imports[func_name]
            if full_path in self.chunks:
                return full_path

        return None