"""
함수 호출 관계 추출 전담 모듈
"""

from tree_sitter_languages import get_language, get_parser

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

    def extract_calls(self, code: str) -> list[str]:
        """코드에서 호출하는 모든 함수 추출"""
        try:
            tree = self.parser.parse(bytes(code, "utf8"))
            captures = self.call_query.captures(tree.root_node)

            calls = []
            for node, tag in captures:
                func_name = code[node.start_byte:node.end_byte]
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
