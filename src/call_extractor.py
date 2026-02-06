"""
함수 호출 관계 추출 전담 모듈
"""

from tree_sitter_languages import get_language, get_parser
import re

class CallExtractor:
    def __init__(self, language: str = "python"):
        self.language = get_language(language)
        self.parser = get_parser(language)

        # Python용 Tree-sitter 쿼리
        self.call_query = self.language.query("""
            (call
                function: [
                    (identifier) @func_name
                    (attribute) @method_name
                ]
            )
        """)

        self.import_query = self.language.query("""
            (import_statement
                name: (dotted_name) @import_name
            )
            (import_from_statement
                module_name: (dotted_name) @module_name
            )
        """)

    def _is_valid_function_name(self, name: str) -> bool:
        """함수명이 유효한지 검증"""
        if not name or not isinstance(name, str):
            return False
        
        name = name.strip()
        
        # 빈 문자열
        if not name:
            return False
        
        # 길이 제한 (일반적인 함수명 길이)
        if len(name) < 2 or len(name) > 50:
            return False
        
        # Python 함수명 규칙: 영문자, 숫자, 언더스코어, 점(모듈 참조)만 허용
        # 단, 첫 글자는 숫자 불가
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_.]*$', name):
            return False
        
        # 특수문자가 포함된 코드 조각 제거
        invalid_chars = [' ', '=', '#', '<', '>', '(', ')', '[', ']', '{', '}', ':', ',', ';', '"', "'", '`', '\\', '/', '|', '&', '*', '+', '-', '%', '!', '?', '@', '$', '^']
        if any(char in name for char in invalid_chars):
            return False
        
        # 숫자로만 이루어진 경우
        if name.isdigit():
            return False
        
        # 언더스코어나 점으로만 이루어진 경우
        if all(c in ['_', '.'] for c in name):
            return False
        
        # Python 예약어 제외 (선택사항)
        python_keywords = {
            'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
            'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
            'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
            'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return',
            'try', 'while', 'with', 'yield'
        }
        if name in python_keywords:
            return False
        
        return True

    def extract_calls(self, code: str) -> list[str]:
        """코드에서 호출하는 모든 함수 추출"""
        try:
            tree = self.parser.parse(bytes(code, "utf8"))
            captures = self.call_query.captures(tree.root_node)

            calls = []
            for node, tag in captures:
                func_name = code[node.start_byte:node.end_byte]
                
                # ⭐ 유효성 검증 추가
                if self._is_valid_function_name(func_name):
                    calls.append(func_name)

            return list(set(calls))  # 중복 제거
        except Exception as e:
            print(f"⚠️ Call extraction error: {e}")
            return []

    def extract_imports(self, code: str) -> list[str]:
        """import 문 추출"""
        try:
            tree = self.parser.parse(bytes(code, "utf8"))
            captures = self.import_query.captures(tree.root_node)

            imports = []
            for node, tag in captures:
                import_name = code[node.start_byte:node.end_byte]
                imports.append(import_name)

            return list(set(imports))
        except Exception as e:
            print(f"⚠️ Import extraction error: {e}")
            return []