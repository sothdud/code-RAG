from dataclasses import dataclass

@dataclass
class LanguageSpec:
    name: str             # tree-sitter 언어 이름
    extension: str        # 파일 확장자
    structure_query: str  # 클래스/함수/프로퍼티 추출 쿼리
    call_query: str       # 함수 호출 추출 쿼리

# 1. Python 설정 (기존 로직 유지)
PYTHON_SPEC = LanguageSpec(
    name="python",
    extension=".py",
    structure_query="""
        (class_definition
            name: (identifier) @name
            body: (block) @body
        ) @class

        (function_definition
            name: (identifier) @name
            body: (block) @body
        ) @function
    """,
    call_query="" # CallExtractor 사용하므로 공란
)

# 2. C# 설정 (새로 추가)
CSHARP_SPEC = LanguageSpec(
    name="c_sharp",
    extension=".cs",
    structure_query="""
    (namespace_declaration
        name: (identifier) @namespace
        body: (declaration_list) @body
    ) @namespace_block

    (class_declaration
        name: (identifier) @name
        body: (declaration_list) @body
    ) @class

    (method_declaration
        type: (_) @return_type
        name: (identifier) @name
        body: (block) @body
    ) @function
    
    (property_declaration
        name: (identifier) @name
    ) @property
    """,
    call_query="""
    (invocation_expression
        function: (identifier) @func_name
    )
    (invocation_expression
        function: (member_access_expression
            name: (identifier) @method_name
        )
    )
    """
)

# src/language_specs.py 맨 아래에 추가

XAML_SPEC = LanguageSpec(
    name="html",  # XML 전용 파서가 없을 경우 HTML 파서를 사용해도 XAML 구조(태그) 분석이 가능합니다.
    extension=".xaml",
    structure_query="""
    (element
        (start_tag (tag_name) @name)
    ) @element
    """,
    call_query=""  # UI 파일이므로 함수 호출은 추출하지 않음
)