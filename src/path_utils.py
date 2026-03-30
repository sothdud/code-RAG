import os
from pathlib import Path

# 무시할 폴더 및 파일 목록
IGNORE_DIRS = {
    '.git', '.venv', 'venv', 'env', '__pycache__', 
    'node_modules', 'dist', 'build', '.idea', '.vscode',
    'site-packages', 'egg-info'
}

IGNORE_FILES = {
    '.DS_Store', 'poetry.lock', 'package-lock.json', '.gitignore'
}

def should_skip_path(path: Path, root_path: Path) -> bool:

    # 1. 파일/폴더 이름 자체가 무시 목록에 있는 경우
    if path.name in IGNORE_DIRS or path.name in IGNORE_FILES:
        return True
    
    # 2. 숨김 파일/폴더 
    if path.name.startswith('.') and path.name != '.':
        return True

    # 3. 경로 중간에 무시할 디렉토리가 포함된 경우
    try:
        # 루트 기준 상대 경로로 변환
        rel_path = path.relative_to(root_path)
        
        # 경로의 각 부분(폴더명)을 검사
        for part in rel_path.parts:
            if part in IGNORE_DIRS or (part.startswith('.') and part != '.'):
                return True
    except ValueError:
        pass

    return False
