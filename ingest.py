import os
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
    # 1. 설정
    TARGET_DIR = Path(os.getenv("SOURCE_CODE_PATH", "./data"))
    if not TARGET_DIR.exists():
        console.print(f"[red]❌ 경로 없음: {TARGET_DIR}[/red]")
        return

    parser = ASTParser()
    graph_builder = GraphBuilder()
    db = VectorStore()
    graph_store = GraphStore()
    
    # 2. 상태 로드 및 변경 감지
    console.print(f"\n[bold yellow]🔍 Phase 1: Detecting Changes...[/bold yellow]")
    
    previous_state = load_state()
    current_state = {}
    
    all_files = []
    # 파일 탐색
    for root, dirs, files in os.walk(TARGET_DIR):
        root_path = Path(root)
        
        # 디렉토리 제외 처리 (재귀적 탐색 효율화)
        dirs[:] = [d for d in dirs if not should_skip_path(root_path / d, TARGET_DIR)]
        
        for file in files:
            if not file.endswith('.py'): continue
            file_path = root_path / file
            if should_skip_path(file_path, TARGET_DIR): continue
            
            all_files.append(file_path)

    # 변경 사항 분류
    files_to_embed = []      # 임베딩 새로 해야 할 파일 (신규/수정)
    files_to_delete = []     # DB에서 지워야 할 파일 (수정/삭제)
    unchanged_files = []     # 변경 없는 파일
    
    # 2-1. 신규 및 수정 파일 감지
    for file_path in all_files:
        str_path = str(file_path)
        current_hash = calculate_file_hash(file_path)
        current_state[str_path] = current_hash
        
        prev_hash = previous_state.get(str_path)
        
        if prev_hash != current_hash:
            if prev_hash is None:
                console.print(f"  [green]+ New:[/green] {file_path.name}")
            else:
                console.print(f"  [yellow]* Modified:[/yellow] {file_path.name}")
                files_to_delete.append(str_path) # 수정된 경우 기존 거 삭제 필요
            files_to_embed.append(file_path)
        else:
            unchanged_files.append(file_path)

    # 2-2. 삭제된 파일 감지
    for old_path in previous_state:
        if old_path not in current_state:
            console.print(f"  [red]- Deleted:[/red] {old_path}")
            files_to_delete.append(old_path)

    # 변경사항이 없으면 조기 종료 (Graph 재생성은 필요할 수 있으나, 보통 코드 변경 없으면 Graph도 그대로)
    if not files_to_embed and not files_to_delete:
        console.print("\n[bold green]✅ No changes detected. System is up to date.[/bold green]")
        return

    # ---------------------------------------------------------
    # 3. 데이터베이스 정리 (Incremental)
    # ---------------------------------------------------------
    # 주의: db.recreate_collection()은 이제 최초 실행 시에만 수동으로 하거나, 
    # 별도 옵션으로 빼야 합니다. 여기서는 자동 삭제 로직으로 대체합니다.
    
    if files_to_delete:
        console.print(f"\n[bold red]🗑️ Removing obsolete chunks ({len(files_to_delete)} files)...[/bold red]")
        # Qdrant에서 파일 경로 기준으로 삭제
        for file_path in files_to_delete:
            try:
                db.client.delete(
                    collection_name=db.collection,
                    points_selector=Filter(
                        must=[
                            FieldCondition(
                                key="filepath",
                                match=MatchText(text=file_path)
                            )
                        ]
                    )
                )
            except Exception as e:
                console.print(f"  ⚠️ Failed to delete {file_path}: {e}")

    # ---------------------------------------------------------
    # 4. 파싱 및 그래프 빌드 (Hybrid)
    # ---------------------------------------------------------
    # Graph 연결성(Caller/Callee)을 위해 '구조'는 전체 파일에서 파악해야 합니다.
    # 하지만 '임베딩(Vector)'은 변경된 파일만 수행하면 됩니다.
    
    console.print(f"\n[bold cyan]🧠 Phase 2: Parsing & Building Structure...[/bold cyan]")
    
    all_chunks_for_graph = []       # 그래프용 (전체)
    chunks_to_upsert = []           # 벡터 저장용 (변경분만)
    
    # 전체 파일을 파싱은 하되 (빠름), 임베딩 대상만 분류
    for file_path in track(all_files, description="Parsing AST..."):
        try:
            chunks = parser.parse_file(str(file_path))
            
            # 그래프 빌더엔 무조건 추가 (전체 문맥 형성)
            for chunk in chunks:
                graph_builder.add_chunk(chunk)
                all_chunks_for_graph.append(chunk)
            
            # 벡터 DB엔 변경된 파일만 추가
            if file_path in files_to_embed:
                chunks_to_upsert.extend(chunks)
                
        except Exception as e:
            console.print(f"  [red]Error parsing {file_path.name}: {e}[/red]")

    # ---------------------------------------------------------
    # 5. Vector DB 저장 (Time Saver!)
    # ---------------------------------------------------------
    if chunks_to_upsert:
        console.print(f"\n[bold green]💾 Phase 3: Updating Vector DB ({len(chunks_to_upsert)} chunks)...[/bold green]")
        # 여기가 가장 느린 구간인데, 변경된 파일만 하므로 속도 획기적 개선
        db.upsert_chunks(chunks_to_upsert)
    else:
        console.print("\n[dim]💾 Phase 3: Vector DB skipped (No new content)[/dim]")

    # ---------------------------------------------------------
    # 6. Graph DB 저장 (Full Sync)
    # ---------------------------------------------------------
    # Graph는 부분 업데이트 시 관계 끊김(Dangling Edges) 처리가 복잡하므로 
    # 전체 재생성(Full Re-indexing) 전략을 유지합니다. (비용 저렴)
    console.print(f"\n[bold magenta]🕸️ Phase 4: Syncing Graph DB...[/bold magenta]")
    
    call_graph = graph_builder.build_call_graph()
    
    # Memgraph 초기화 후 전체 노드/엣지 다시 쓰기
    graph_store.clear_all_data()
    graph_store.save_graph_data(all_chunks_for_graph, call_graph.edges)

    # ---------------------------------------------------------
    # 7. 상태 저장
    # ---------------------------------------------------------
    save_state(current_state)
    console.print("\n[bold blue]✨ Incremental Ingest Complete![/bold blue]")

if __name__ == "__main__":
    main()