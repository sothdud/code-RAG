from pathlib import Path

def extract_source_lines(file_path: Path, start_line: int, end_line: int) -> str:
    """
    디스크에서 실제 파일을 읽어 특정 라인 범위를 추출합니다.
    (줄 번호 거짓말 방지용 핵심 함수)
    """
    try:
        if not file_path.exists():
            return f"# Error: File not found at {file_path}"

        # 파일 읽기 (utf-8)
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total_lines = len(lines)
        
        # 라인 범위 보정 (1-based index -> 0-based index)
        # 사용자가 요청한 줄이 파일 범위를 벗어나면 조정
        if start_line < 1: start_line = 1
        if end_line > total_lines: end_line = total_lines
        
        # 실제 추출 (리스트 슬라이싱)
        # start_line - 1 : 리스트 인덱스는 0부터 시작하므로
        snippet = lines[start_line - 1 : end_line]

        return "".join(snippet).rstrip()

    except Exception as e:
        return f"# Error reading file: {str(e)}"