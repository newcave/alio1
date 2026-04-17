"""공통 유틸리티 함수들."""
import os
import re
import io
import zipfile
from pathlib import Path
from typing import Iterable

import pandas as pd


def sanitize_filename(name: str, max_len: int = 180) -> str:
    """파일명에서 OS 금지문자 제거 + 길이 제한."""
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] if name else "file"


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def count_files(folder: str | Path, ext: str = ".pdf") -> int:
    """폴더 내 특정 확장자 파일 개수 (재귀)."""
    p = Path(folder)
    if not p.exists():
        return 0
    return sum(1 for _ in p.glob(f"**/*{ext}"))


def list_files(folder: str | Path, exts: Iterable[str] = (".pdf",)) -> list[Path]:
    """폴더 내 지정 확장자 파일 리스트 (재귀, 정렬)."""
    p = Path(folder)
    if not p.exists():
        return []
    results: list[Path] = []
    for ext in exts:
        results.extend(p.glob(f"**/*{ext}"))
    return sorted(results)


def count_csv_rows(path: str | Path) -> int:
    try:
        return len(pd.read_csv(path, encoding="utf-8-sig"))
    except Exception:
        return 0


def make_zip_bytes(files: list[Path], base_folder: str | Path | None = None) -> bytes:
    """여러 파일을 하나의 ZIP 바이트스트림으로 묶는다."""
    base = Path(base_folder) if base_folder else None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if not f.exists():
                continue
            arcname = f.relative_to(base) if base and base in f.parents else f.name
            zf.write(f, arcname=str(arcname))
    return buf.getvalue()


def human_size(n_bytes: int) -> str:
    """바이트 수를 사람이 읽을 수 있는 형식으로."""
    units = ["B", "KB", "MB", "GB"]
    size = float(n_bytes)
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
