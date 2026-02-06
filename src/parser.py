import os
import xml.etree.ElementTree as ET
from tree_sitter_languages import get_language, get_parser
from .models import CodeChunk
from .call_extractor import CallExtractor
from .fqn_resolver import resolve_fqn_from_ast
from pathlib import Path


class UIParser:
    """
    .ui (XML) 파일을 파싱하여 위젯 정보를 CodeChunk로 변환
    """
    def parse_ui(self, filepath: str, repo_root: str):
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
            chunks = []
            
            file_name = os.path.basename(filepath)
            
            # 모든 위젯을 순회하며 정보 추출
            for widget in root.iter('widget'):
                widget_name = widget.get('name')
                widget_class = widget.get('class')
                
                if not widget_name or not widget_class:
                    continue
                    
                # XML 내용을 그대로 저장
                xml_content = ET.tostring(widget, encoding='unicode')
                
                # FQN: 파일명.위젯명 (예: auto_labeling.ui.runAutoLabelButton)
                qualified_name = f"{file_name}.{widget_name}"

                chunks.append(CodeChunk(
                    name=widget_name,
                    type="ui_widget",  # UI 위젯임을 표시
                    content=xml_content,
                    filepath=filepath,
                    start_line=0, 
                    language="xml",
                    qualified_name=qualified_name,
                    module_path=file_name,
                    imports={}, # [수정] 딕셔너리 형태로 통일 (기존 [])
                    docstring=f"UI Widget: {widget_name} ({widget_class})",
                    calls=[] 
                ))
            return chunks
        except Exception as e:
            print(f"Failed to parse UI file {filepath}: {e}")
            return []


class ASTParser:
    def __init__(self, context_lines: int = 5):
        """
        Args:
            context_lines (int): 청크 생성 시 함수/클래스 전후로 포함할 문맥 라인 수.
        """
        self.language = get_language("python")
        self.parser = get_parser("python")
        self.call_extractor = CallExtractor("python")
        self.context_lines = context_lines
        self.ui_parser = UIParser()

        # 1. 함수와 클래스 추출 쿼리 (구조 + 독스트링 포함)
        self.structure_query = self.language.query("""
            (class_definition
                name: (identifier) @name
                body: (block) @body
            ) @class

            (function_definition
                name: (identifier) @name
                body: (block) @body
            ) @function
        """)

    def _extract_imports(self, code_bytes: bytes) -> dict[str, str]:
        """파일 상단의 Import 구문을 텍스트 파싱으로 신속하게 분석"""
        imports = {}
        try:
            code_str = code_bytes.decode('utf-8', errors='ignore')
            for line in code_str.split('\n'):
                line = line.strip()
                if line.startswith("import "):
                    parts = line.replace("import ", "").split(" as ")
                    if len(parts) == 2:
                        imports[parts[1].strip()] = parts[0].strip()
                    else:
                        for p in parts[0].split(','):
                            p = p.strip()
                            imports[p] = p
                elif line.startswith("from "):
                    try:
                        parts = line.split(" import ")
                        if len(parts) < 2: continue
                        module = parts[0].replace("from ", "").strip()
                        names = parts[1].split(",")
                        for name in names:
                            name = name.strip()
                            if " as " in name:
                                origin, alias = name.split(" as ")
                                imports[alias.strip()] = f"{module}.{origin.strip()}"
                            else:
                                imports[name] = f"{module}.{name}"
                    except:
                        continue
        except:
            pass
        return imports

    def parse_file(self, filepath: str, repo_root: str):
        """파일 확장자에 따라 적절한 파싱 수행"""
        if filepath.endswith(".ui"):
            return self.ui_parser.parse_ui(filepath, repo_root)
        
        with open(filepath, "rb") as f:
            code_bytes = f.read()

        tree = self.parser.parse(code_bytes)
        root_node = tree.root_node
        
        # [수정] Import 구문을 리스트로 변환하지 않고 딕셔너리 그대로 사용
        file_imports = self._extract_imports(code_bytes)
        
        captures = self.structure_query.captures(root_node)
        chunks = []
        processed_nodes = set()

        for node, tag in captures:
            if tag not in ('class', 'function'): continue
            if node in processed_nodes: continue
            
            # FQN 해결
            qualified_name = resolve_fqn_from_ast(node, Path(filepath), Path(repo_root), code_bytes)
            if not qualified_name: continue
            
            start_line_idx = node.start_point[0]
            end_line_idx = node.end_point[0]
            
            # 코드 추출
            chunk_content = code_bytes[node.start_byte:node.end_byte].decode('utf-8', errors='ignore')
            
            # 함수 이름 추출
            name_node = node.child_by_field_name('name')
            func_name = code_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')

            # 호출 및 속성 추출
            calls = self.call_extractor.extract_calls(chunk_content)

            docstring = "" 

            chunks.append(CodeChunk(
                name=func_name,
                type=tag,
                content=chunk_content,
                filepath=filepath,
                start_line=start_line_idx + 1,
                language="python",
                qualified_name=qualified_name,
                module_path=qualified_name.rsplit('.', 1)[0] if '.' in qualified_name else "",
                imports=file_imports, # 딕셔너리 전달
                docstring=docstring,
                calls=calls
            ))
            processed_nodes.add(node)

        return chunks