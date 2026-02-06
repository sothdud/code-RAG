import os
from tree_sitter_languages import get_language, get_parser
from .models import CodeChunk
from .call_extractor import CallExtractor
from .fqn_resolver import resolve_fqn_from_ast
from pathlib import Path


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

    def _normalize_filepath(self, filepath: str) -> str:
        """
        ⭐ 핵심 수정: 경로에서 연속된 중복 디렉토리 제거
        
        예:
        - docs/api/dreamer/dreamer/afi/model.py 
          → docs/api/dreamer/afi/model.py
        
        - src/utils/utils/helper.py
          → src/utils/helper.py
        """
        try:
            path_obj = Path(filepath)
            parts = list(path_obj.parts)
            
            # 연속된 중복 제거
            cleaned_parts = []
            prev_part = None
            
            for part in parts:
                if part != prev_part:  # 이전과 다르면 추가
                    cleaned_parts.append(part)
                prev_part = part
            
            return str(Path(*cleaned_parts)) if cleaned_parts else filepath
            
        except Exception as e:
            # 에러 발생 시 원본 경로 반환
            return filepath

    def _extract_imports(self, code_bytes: bytes) -> dict[str, str]:
        """파일 상단의 Import 구문을 텍스트 파싱으로 신속하게 분석"""
        imports = {}
        try:
            code_str = code_bytes.decode('utf-8', errors='ignore')
            for line in code_str.split('\n'):
                line = line.strip()
                if line.startswith("import "):
                    # ex: import os, sys
                    parts = line.replace("import ", "").split(" as ")
                    if len(parts) == 2:
                        imports[parts[1].strip()] = parts[0].strip()
                    else:
                        for p in parts[0].split(','):
                            p = p.strip()
                            imports[p] = p
                elif line.startswith("from "):
                    # ex: from .models import CodeChunk
                    try:
                        parts = line.split(" import ")
                        if len(parts) < 2: continue
                        module = parts[0].replace("from ", "").strip()
                        names = parts[1].split(",")
                        for name in names:
                            name = name.strip()
                            if " as " in name:
                                orig, alias = name.split(" as ")
                                imports[alias.strip()] = f"{module}.{orig.strip()}"
                            else:
                                imports[name] = f"{module}.{name}"
                    except:
                        continue
        except Exception:
            pass
        return imports

    def parse_file(self, filepath: str) -> list[CodeChunk]:
        if not filepath.endswith(".py"): return []

        try:
            # ⭐ 경로 정규화 (중복 제거)
            normalized_filepath = self._normalize_filepath(filepath)
            
            # 실제 파일은 원본 경로로 읽기
            path_obj = Path(filepath)
            repo_root = Path(".").resolve()

            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                code_text = f.read()

            code_bytes = bytes(code_text, "utf8")
            code_lines = code_text.splitlines()
            total_lines = len(code_lines)

            tree = self.parser.parse(code_bytes)
            file_imports = self._extract_imports(code_bytes)

            chunks = []
            captures = self.structure_query.captures(tree.root_node)
            processed_nodes = set()

            for node, tag in captures:
                if node in processed_nodes: continue

                # 기본 함수명 추출
                name_node = node.child_by_field_name('name')
                if not name_node: continue
                func_name = code_bytes[name_node.start_byte:name_node.end_byte].decode("utf8")

                # FQN 생성
                qualified_name = resolve_fqn_from_ast(
                    func_node=node,
                    file_path=Path(normalized_filepath),  # ⭐ 정규화된 경로 사용
                    repo_root=repo_root,
                    code_bytes=code_bytes
                )

                # 실패 시 안전장치
                if not qualified_name:
                    qualified_name = f"{Path(normalized_filepath).stem}.{func_name}"

                # Docstring 추출
                docstring = ""
                body_node = node.child_by_field_name('body')
                if body_node:
                    for child in body_node.children:
                        if child.type == 'expression_statement':
                            first_child = child.children[0] if child.children else None
                            if first_child and first_child.type == 'string':
                                doc = code_bytes[first_child.start_byte:first_child.end_byte].decode("utf8")
                                docstring = doc.strip('"""').strip("'''").strip()
                                break
                        elif child.type == 'comment':
                            continue
                        else:
                            break

                # Context 추출
                start_line_idx = node.start_point[0]
                end_line_idx = node.end_point[0]
                context_start = max(0, start_line_idx - self.context_lines)
                context_end = min(total_lines, end_line_idx + 1 + self.context_lines)

                chunk_content = "\n".join(code_lines[context_start:context_end])

                # Call Extractor
                pure_content = code_bytes[node.start_byte:node.end_byte].decode("utf8")
                calls = self.call_extractor.extract_calls(pure_content)

                valid_calls = []
                for c in calls:
                    c = c.strip()
                    
                    if not c: continue
                    
                    if any(char in c for char in [' ', '=', '#', '<', '>', '(', ')', '[', ']', '{', '}', ':', ',']):
                        continue
                        
                    if len(c) > 50 or len(c) < 2:
                        continue
                        
                    valid_calls.append(c)
                
                calls = valid_calls

                chunks.append(CodeChunk(
                    name=func_name,
                    type=tag,
                    content=chunk_content,
                    filepath=normalized_filepath,  # ⭐ 정규화된 경로 저장
                    start_line=start_line_idx + 1,
                    language="python",
                    qualified_name=qualified_name,
                    module_path=qualified_name.rsplit('.', 1)[0] if '.' in qualified_name else "",
                    imports=file_imports,
                    docstring=docstring,
                    calls=calls
                ))
                processed_nodes.add(node)

            return chunks

        except Exception as e:
            print(f"⚠️ Error parsing {filepath}: {e}")
            return []