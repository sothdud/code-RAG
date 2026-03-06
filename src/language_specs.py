from dataclasses import dataclass

@dataclass
class LanguageSpec:
    name: str
    db_name: str            # tree-sitter 언어 이름 (또는 DB 저장용 언어명)
    extension: str          # 파일 확장자
    structure_query: str    # 클래스/함수/프로퍼티 추출 쿼리 (Tree-sitter용)
    call_query: str         # 함수 호출 추출 쿼리 (Tree-sitter용)
    binding_pattern: str = ""

# 1. Python 설정
PYTHON_SPEC = LanguageSpec(
    name="python",
    db_name="python",
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

# 2. C# 설정 
CSHARP_SPEC = LanguageSpec(
    name="c_sharp",
    db_name="c_sharp",
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
    
    (constructor_declaration
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

    (object_creation_expression
        (argument_list
            (argument (identifier) @func_name)
        )
    )
    """
)

# 3. XAML 설정 (정규식 기반)
XAML_SPEC = LanguageSpec(
    name="xaml",         
    db_name="xaml",
    extension=".xaml",
    structure_query="", 
    call_query="",       
    binding_pattern=r'\{Binding\s+(?:Path=)?([a-zA-Z0-9_.]+)'
)