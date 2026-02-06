"""
함수 호출 관계 추출 전담 모듈
"""

from tree_sitter_languages import get_language, get_parser
import re

class CallExtractor:
    def __init__(self, language: str = "python"):
        self.language = get_language(language)
        self.parser = get_parser(language)

        # 1. 일반 함수 호출 쿼리
        self.call_query = self.language.query("""
            (call
                function: [
                    (identifier) @func_name
                    (attribute) @method_name
                ]
            )
        """)
        
        # 2. 속성 접근 쿼리 (UI 위젯 참조 감지용: self.widgetName)
        self.attr_query = self.language.query("""
            (attribute
                object: (identifier) @obj
                attribute: (identifier) @attr
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
        if not name: return False
        if len(name) < 2 or len(name) > 50: return False
        
        python_keywords = {
            'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
            'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
            'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return',
            'try', 'while', 'with', 'yield'
        }
        if name in python_keywords:
            return False
        
        # 특수문자 포함 여부 체크
        if any(char in name for char in [' ', '=', '#', '<', '>', '(', ')', '[', ']', '{', '}', ':', ',']):
            return False
            
        return True

    def extract_calls(self, code: str) -> list[str]:
        """코드에서 호출하는 모든 함수 및 UI 위젯 참조 추출"""
        try:
            tree = self.parser.parse(bytes(code, "utf8"))
            
            calls = set()
            
            # 1. 함수 호출 추출
            captures = self.call_query.captures(tree.root_node)
            for node, tag in captures:
                func_name = code[node.start_byte:node.end_byte]
                if self._is_valid_function_name(func_name):
                    calls.add(func_name)

            # 2. [개선] 속성 접근 추출 (self.xxx)
            attr_captures = self.attr_query.captures(tree.root_node)
            for node, tag in attr_captures:
                full_text = code[node.start_byte:node.end_byte]
                if full_text.startswith("self."):
                    # 'self.runAutoLabelButton.clicked.connect(...)'
                    # → 'runAutoLabelButton' 추출
                    parts = full_text.split('.', 2)  # ['self', 'runAutoLabelButton', 'clicked.connect(...)']
                    if len(parts) > 1:
                        attr_name = parts[1]
                        # 메서드 체인 첫 번째 속성만 추출
                        if self._is_valid_function_name(attr_name):
                            calls.add(attr_name)

            return list(calls)
        except Exception as e:
            print(f"⚠️ Call extraction error: {e}")
            return []

    def extract_imports(self, code: str) -> list[str]:
        """import 문 추출 (기존 유지)"""
        try:
            tree = self.parser.parse(bytes(code, "utf8"))
            captures = self.import_query.captures(tree.root_node)

            imports = []
            for node, tag in captures:
                imports.append(code[node.start_byte:node.end_byte])
            return list(set(imports))
        except:
            return []