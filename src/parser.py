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
            # ✨ [추가] 경로 객체 준비
            path_obj = Path(filepath)
            repo_root = Path(".").resolve()  # 현재 실행 위치를 프로젝트 루트로 가정

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

                # ✨ [핵심 변경] 새로 만든 resolver 사용
                qualified_name = resolve_fqn_from_ast(
                    func_node=node,
                    file_path=path_obj,
                    repo_root=repo_root,
                    code_bytes=code_bytes
                )

                # 실패 시 안전장치 (파일명.함수명)
                if not qualified_name:
                    qualified_name = f"{path_obj.stem}.{func_name}"

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
                    c = c.strip() # 앞뒤 공백 제거
                    
                    # 1. 빈 문자열이면 버림
                    if not c: continue
                    
                    # 2. '공백'이나 '특수문자'가 섞여있으면 무조건 버림 (함수 이름 아님)
                    # (함수명에는 띄어쓰기, =, #, <, >, 괄호 등이 절대 못 들어감)
                    if any(char in c for char in [' ', '=', '#', '<', '>', '(', ')', '[', ']', '{', '}', ':', ',']):
                        continue
                        
                    # 3. 너무 길거나(50자), 너무 짧으면(1자) 버림
                    if len(c) > 50 or len(c) < 2:
                        continue
                        
                    valid_calls.append(c)
                
                # 깨끗한 리스트로 교체
                calls = valid_calls

                chunks.append(CodeChunk(
                    name=func_name,
                    type=tag,
                    content=chunk_content,
                    filepath=filepath,
                    start_line=start_line_idx + 1,
                    language="python",
                    qualified_name=qualified_name,  # 정확한 경로가 들어감
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
