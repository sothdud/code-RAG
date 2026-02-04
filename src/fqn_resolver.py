from pathlib import Path
from tree_sitter import Node


def resolve_fqn_from_ast(
        func_node: Node,
        file_path: Path,
        repo_root: Path,
        code_bytes: bytes
) -> str | None:
    """
    AST 노드와 파일 경로를 기반으로 Python의 Fully Qualified Name(FQN)을 생성합니다.
    예: src/auth/login.py 내부의 User 클래스의 sign_in 함수
        -> src.auth.login.User.sign_in
    """
    try:
        # 1. 파일 경로를 모듈 경로로 변환 (예: src/utils.py -> src.utils)
        try:
            rel_path = file_path.resolve().relative_to(repo_root.resolve())
            module_parts = list(rel_path.with_suffix('').parts)
        except ValueError:
            # 경로 계산 실패 시 파일명만 사용
            module_parts = [file_path.stem]

        # 2. AST를 타고 올라가며 부모(클래스/함수) 이름 추적
        names = []

        # 현재 노드의 이름 찾기
        name_node = func_node.child_by_field_name('name')
        if name_node:
            names.append(code_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8'))

        # 부모 노드 탐색
        parent = func_node.parent
        while parent:
            if parent.type in ('class_definition', 'function_definition'):
                p_name_node = parent.child_by_field_name('name')
                if p_name_node:
                    p_name = code_bytes[p_name_node.start_byte:p_name_node.end_byte].decode('utf-8')
                    names.append(p_name)
            parent = parent.parent

        # 3. [모듈경로] + [부모클래스] + [함수명] 결합
        # names는 [함수명, 부모클래스, ...] 순서이므로 뒤집어야 함
        full_parts = module_parts + names[::-1]

        return ".".join(full_parts)

    except Exception as e:
        print(f"⚠️ FQN Resolution Error: {e}")
        return None