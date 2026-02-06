# src/models.py
from dataclasses import dataclass, field


@dataclass
class CodeChunk:
    name: str
    type: str  # function, class
    content: str
    filepath: str
    start_line: int
    language: str


    qualified_name: str = ""  # 예: backend.utils.save_data
    module_path: str = ""  # 예: backend.utils

    #Import 맵핑 (예: {'np': 'numpy', 'db': 'app.database'})
    imports: dict[str, str] = field(default_factory=dict)

    # 상속 정보 (예: ['BaseModel', 'mixin'])
    bases: list[str] = field(default_factory=list)

    # 관계 정보
    calls: list[str] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)

    docstring: str = ""
    summary: str = ""


@dataclass
class CallGraph:
    nodes: dict[str, CodeChunk]
    edges: dict[str, list[str]]
    reverse_edges: dict[str, list[str]]
