import os
import sys
import json
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track
from collections import defaultdict

from src.models import CodeChunk
from src.parser import ASTParser
from src.graph_builder import GraphBuilder
from src.database import VectorStore
from src.graph_store import GraphStore
from src.path_utils import should_skip_path

load_dotenv()
console = Console()

STATE_FILE = ".ingest_state.json"
SUPPORTED_EXTENSIONS = {".py", ".cs", ".xaml", ".cpp", ".h", ".hpp", ".c"}
FORCE_REINDEX = os.getenv("FORCE_REINDEX", "false").lower() == "true"


def calculate_file_hash(filepath: Path) -> str:
    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except FileNotFoundError:
        return ""


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    SOURCE_PATH = os.getenv("SOURCE_CODE_PATH", "./")

    #1. DB 초기화
    console.print("[bold cyan]🗄️  Initializing PostgreSQL...[/bold cyan]")
    db = VectorStore()
  
    graph_builder = GraphBuilder()

    # 2. 파일 스캔 및 변경 감지
    console.print("🔍 Phase 1: Detecting Changes...")

    source_path = Path(SOURCE_PATH).resolve()
    old_state   = load_state()
    current_state: dict[str, str] = {}
    files_to_process: list[str] = []

    for root, dirs, files in os.walk(source_path):
        root_path = Path(root)

        if should_skip_path(root_path, source_path):
            dirs[:] = []  
            continue

        for file in files:
            file_path = root_path / file

            if should_skip_path(file_path, source_path):
                continue
            if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if str(file_path).endswith('.xaml.cs'):
                continue

            file_hash = calculate_file_hash(file_path)
            rel_path  = str(file_path.relative_to(source_path))
            current_state[rel_path] = file_hash

            if (
                rel_path not in old_state
                or old_state[rel_path] != file_hash
                or FORCE_REINDEX
            ):
                files_to_process.append(str(file_path))

    deleted_files = set(old_state.keys()) - set(current_state.keys())
    if deleted_files:
        console.print(f"🗑️  Found {len(deleted_files)} deleted files (manual cleanup may be needed).")

    if not files_to_process and not deleted_files:
        console.print("\n✅ No changes detected. System is up to date.")
        save_state(current_state)
        return

    console.print(f"📦 Found {len(files_to_process)} files to process.")

    #파싱
    console.print("\n[bold yellow]🔨 Phase 2: Parsing Code (Without LLM yet)...[/bold yellow]")
    parser = ASTParser()
    chunks_to_upsert: list[CodeChunk] = []

    for filepath in track(files_to_process, description="Parsing files..."):
        file_chunks = parser.parse(filepath)
        chunks_to_upsert.extend(file_chunks)
    
    cs_chunks = [c for c in chunks_to_upsert if str(c.filepath).endswith('.cs')]
    if len(cs_chunks) == 0:
        console.print("[bold red]⚠️ C# 파일에서 청크가 하나도 안 나왔습니다! parser.py가 .cs를 못 읽고 있습니다.[/bold red]")

    #폴더 단위 요약 청크 생성
    console.print("\n[bold blue]📁 Phase 2-1: Generating Folder Prompts...[/bold blue]")
    folder_groups: dict[str, list[CodeChunk]] = defaultdict(list)
    for chunk in chunks_to_upsert:
        folder_path = chunk.module_path
        if not folder_path:
            folder_path = os.path.dirname(chunk.filepath)
        
        if folder_path:
            folder_groups[folder_path].append(chunk)

    folder_chunks: list[CodeChunk] = []
    for folder_path, chunks_in_folder in folder_groups.items():
        files_in_folder = list(set(os.path.basename(c.filepath) for c in chunks_in_folder))

        core_elements = []
        for c in chunks_in_folder:
            if c.type in ["class", "method", "function", "view", "file"]:
                # 요약이 아직 없으므로 이름과 타입만 추출
                core_elements.append(f"- {c.type} [{c.name}]")

        prompt = (
            f"다음은 '{folder_path}' 폴더 내부에 있는 소스코드 구조입니다.\n\n"
            f"[포함된 파일]\n{', '.join(files_in_folder)}\n\n"
            f"[핵심 요소들]\n{chr(10).join(core_elements[:40])}\n\n"
            "당신은 시니어 아키텍트입니다. 위 정보를 바탕으로 다음 두 가지를 작성해주세요.\n"
            "1. 이 폴더 전체의 기술적 목적과 아키텍처 상의 역할\n"
            "2. 핵심 메서드/클래스들이 상호작용하는 관계 요약\n"
            "**한국어로 작성**"
        )
        
        #즉시 호출 안 하고 prompt를 content에 넣어둡니다.
        folder_chunk = CodeChunk(
            name=os.path.basename(folder_path),
            type="folder",
            content=prompt, # 프롬프트를 담아서 병렬 요약 때 던질 수 있게 함
            summary="",
            filepath=folder_path,
            start_line=0,
            language="architecture",
            qualified_name=folder_path.replace("\\", "/").replace("/", "."),
            module_path=folder_path,
            docstring="",
        )
        folder_chunks.append(folder_chunk)

    chunks_to_upsert.extend(folder_chunks)

    #일괄요약
    console.print("\n[bold cyan]🚀 Phase 2-2: Generating ALL Summaries in Parallel...[/bold cyan]")
    parser.enrich_summaries_in_batch(chunks_to_upsert, max_workers=10)

    # 그래프 빌더에 청크 등록
    for chunk in chunks_to_upsert:
        graph_builder.add_chunk(chunk)

    console.print(f"   > Total {len(chunks_to_upsert)} chunks generated (files + folders).")

    # 4. 벡터 DB 업서트
    if chunks_to_upsert:
            console.print("\n[bold green]💾 Phase 3: Upserting to PostgreSQL (pgvector)...[/bold green]")
            
            unique_chunks_map = {}
            for chunk in chunks_to_upsert:
                identifier = chunk.qualified_name or f"{chunk.filepath}_{chunk.name}_{chunk.start_line}"
                unique_chunks_map[identifier] = chunk
                
            deduped_chunks = list(unique_chunks_map.values())
            for c in deduped_chunks:
                if c.type == "view" or c.name.endswith(".xaml"):
                    pass # 디버그 로그 제거 (너무 많아질 수 있으므로)
            
            db.upsert_chunks(deduped_chunks)

    # 5. 그래프 DB 동기화
    if chunks_to_upsert:
        console.print("\n[bold magenta]🕸️  Phase 4: Syncing Call Graph (call_edges)...[/bold magenta]")
        graph_builder.build_call_graph(db=db)

    # 6. 상태 저장
    save_state(current_state)
    console.print("\n[bold blue]✨ Ingest Complete![/bold blue]")

if __name__ == "__main__":
    main()