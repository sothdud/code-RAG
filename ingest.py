import os
import sys
import json
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

# Qdrant Filter 관련 임포트 (삭제용)
from qdrant_client.models import Filter, FieldCondition, MatchText

from src.parser import ASTParser
from src.graph_builder import GraphBuilder
from src.database import VectorStore
from src.graph_store import GraphStore
from src.path_utils import should_skip_path

load_dotenv()
console = Console()

STATE_FILE = ".ingest_state.json"

# ⭐ [수정] 지원할 확장자 목록에 C#과 XAML 추가
SUPPORTED_EXTENSIONS = {'.py', '.cs', '.xaml'}

def calculate_file_hash(filepath: Path) -> str:
    """파일 내용의 MD5 해시를 계산합니다."""
    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except FileNotFoundError:
        return ""

def load_state() -> dict:
    """이전 인덱싱 상태를 로드합니다."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    """현재 인덱싱 상태를 저장합니다."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def main():
    # 설정 가져오기
    QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
    COLLECTION_NAME = os.getenv("COLLECTION_NAME", "codebase_v1")
    SOURCE_PATH = os.getenv("SOURCE_CODE_PATH", "./")
    
    # 1. 모델 및 DB 초기화
    # ⭐ [수정] database.py가 encoder 인자를 받지 않으므로, 여기서 모델을 로드하지 않고 기본 생성자 호출
    db = VectorStore() 
    
    graph_store = GraphStore()
    graph_builder = GraphBuilder()
    
    # 2. 파일 스캔 및 변경 감지
    console.print("🔍 Phase 1: Detecting Changes...")
    
    source_path = Path(SOURCE_PATH).resolve()
    old_state = load_state()
    current_state = {}
    
    files_to_process = []
    all_chunks_for_graph = [] # 그래프 DB 전체 동기화를 위해 모든 청크 추적용 (선택사항)

    # 파일 탐색 로직
    for root, dirs, files in os.walk(source_path):
        root_path = Path(root)
        
        if should_skip_path(root_path, source_path):
            continue
            
        for file in files:
            file_path = root_path / file
            
            if should_skip_path(file_path, source_path):
                continue
            
            # ⭐ [수정] 확장자 필터링 로직 추가 (.py, .cs, .xaml)
            if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
                
            # 해시 계산
            file_hash = calculate_file_hash(file_path)
            rel_path = str(file_path.relative_to(source_path))
            
            current_state[rel_path] = file_hash
            
            # 변경되었거나 새로 추가된 파일인지 확인
            if rel_path not in old_state or old_state[rel_path] != file_hash:
                files_to_process.append(str(file_path))

    # 삭제된 파일 처리
    deleted_files = set(old_state.keys()) - set(current_state.keys())
    if deleted_files:
        console.print(f"🗑️  Found {len(deleted_files)} deleted files.")
        # 삭제 로직은 기존 database.py에 구현되어 있다면 호출 (현재는 생략)

    # 변경사항이 없으면 종료 (단, 그래프 재구축 강제 옵션이 있다면 진행)
    # 여기서는 간단히 변경사항 없으면 종료 처리
    if not files_to_process and not deleted_files:
        console.print("\n✅ No changes detected. System is up to date.")
        save_state(current_state)
        return

    console.print(f"📦 Found {len(files_to_process)} files to process.")

    # 3. 파싱 (Parsing)
    console.print(f"\n[bold yellow]🔨 Phase 2: Parsing Code...[/bold yellow]")
    
    parser = ASTParser()
    chunks_to_upsert = []
    
    # 트랙킹하며 파싱
    for filepath in track(files_to_process, description="Parsing files..."):
        # ⭐ [수정] parser.py가 이제 내부적으로 확장자를 체크하므로 그대로 호출
        file_chunks = parser.parse(filepath)
        chunks_to_upsert.extend(file_chunks)

    # 그래프 빌더에는 '전체' 청크를 넣어야 관계가 정확하지만, 
    # 여기서는 '변경된' 청크만 추가하여 점진적 업데이트를 수행합니다.
    # (만약 전체 관계를 다시 맺고 싶다면 전체 파일을 다시 파싱해야 합니다)
    for chunk in chunks_to_upsert:
        graph_builder.add_chunk(chunk)
        all_chunks_for_graph.append(chunk)

    console.print(f"   > Generated {len(chunks_to_upsert)} new chunks.")

    # 4. 벡터 DB 업서트 (Vector DB Upsert)
    if chunks_to_upsert:
        console.print(f"\n[bold green]💾 Phase 3: Updating Vector DB...[/bold green]")
        db.upsert_chunks(chunks_to_upsert)
    
    # 5. 그래프 DB 동기화 (Graph DB Sync)
    # 변경된 부분에 대해서만 그래프 관계 생성
    if chunks_to_upsert:
        console.print(f"\n[bold magenta]🕸️ Phase 4: Syncing Graph DB...[/bold magenta]")
        call_graph = graph_builder.build_call_graph()
        graph_store.save_graph_data(chunks_to_upsert, call_graph.edges)

    # 6. 상태 저장
    save_state(current_state)
    console.print("\n[bold blue]✨ Ingest Complete![/bold blue]")

if __name__ == "__main__":
    main()