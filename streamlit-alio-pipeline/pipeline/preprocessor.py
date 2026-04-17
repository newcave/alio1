"""
STEP 2. PDF 메타데이터 자동 추출 (함수형 리팩터링)

- 제목(KR/EN), 출판 날짜, 저자, 요약문(KR/EN) 추출
- `run(pdf_dir, csv_out, on_progress=...)` 로 호출
- CLI 단독 실행도 가능
"""
from __future__ import annotations

import os
import re
import csv
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional

import pdfplumber


# ────────────────────────────────────────────────────────────
# 텍스트 유틸
# ────────────────────────────────────────────────────────────
def _clean(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\n+", "\n", text)
    text = re.sub(r" +", " ", text)
    return text.strip()


# ────────────────────────────────────────────────────────────
# 제목 + 날짜 (1페이지)
# ────────────────────────────────────────────────────────────
_NOISE_PATTERNS = [
    r"^\d{4}-[A-Z]", r"^[A-Z]\d{6}$", r"^Final\s+Report",
    r"^K-water", r"^KOREA", r"^WATER\s+RESOURCES",
    r"^CORPORATION", r"^\d{4}\s*[.\-]\s*\d{1,2}",
]
_DATE_RE = re.compile(r"(\d{4})\s*[.\-]\s*(\d{1,2})\s*$")


def _is_noise(line: str) -> bool:
    return any(re.match(p, line, re.IGNORECASE) for p in _NOISE_PATTERNS)


def extract_title_and_date(page1_text: str) -> dict:
    lines = [l.strip() for l in page1_text.split("\n") if l.strip()]
    lines = [l for l in lines if not re.match(r"^-\s*\d+\s*-$", l)]

    clean_lines = [l for l in lines if not _is_noise(l)]

    # 한글 후보
    kr_lines = [
        l for l in clean_lines
        if any("\uAC00" <= c <= "\uD7A3" for c in l) and len(l) > 3
    ]

    # 날짜 (원본 lines에서 탐색)
    date = ""
    for line in lines:
        dm = _DATE_RE.search(line)
        if dm:
            date = f"{dm.group(1)}.{int(dm.group(2)):02d}"
            break

    # 영어 줄 그룹 수집 (연속 병합)
    en_groups: list[str] = []
    cur: list[str] = []
    for line in clean_lines:
        alpha = sum(1 for c in line if c.isascii() and c.isalpha())
        total_alpha = sum(1 for c in line if c.isalpha())
        is_en = total_alpha > 0 and alpha / total_alpha > 0.7 and len(line) > 5
        if is_en:
            cur.append(line)
        elif cur:
            en_groups.append(" ".join(cur))
            cur = []
    if cur:
        en_groups.append(" ".join(cur))

    title_en = max(en_groups, key=len) if en_groups else ""
    if title_en:
        # 소문자→대문자 경계에 공백 보정 (PDF 렌더링 이슈)
        title_en = re.sub(r"([a-z])([A-Z])", r"\1 \2", title_en)
        title_en = re.sub(r"\s+", " ", title_en).strip()

    # 한글 제목
    title_kr = ""
    if kr_lines:
        if len(kr_lines) >= 2 and len(kr_lines[0]) < 20 and len(kr_lines[1]) < 20:
            title_kr = f"{kr_lines[0]} {kr_lines[1]}"
        else:
            title_kr = kr_lines[0]

    return {"title_kr": title_kr, "title_en": title_en, "date": date}


# ────────────────────────────────────────────────────────────
# 저자 (제출문 페이지)
# ────────────────────────────────────────────────────────────
_TITLE_PATTERNS = [
    r"K-water연구원", r"수자원운영처", r"연구관리처", r"상하수도연구소",
    r"[가-힣]+운영처", r"[가-힣]+관리처", r"[가-힣]+연구처", r"[가-힣]+연구소",
    r"경상국립대학교", r"청\s*주\s*대\s*학\s*교", r"인\s*하\s*대\s*학\s*교",
    r"[가-힣]+대학교", r"[가-힣]+대학원", r"[가-힣]+연구원",
    r"연\s*구\s*책\s*임\s*자", r"연\s*구\s*수\s*행\s*자", r"자\s*문",
    r"수\s*석\s*연\s*구\s*원", r"선\s*임\s*연\s*구\s*원", r"책\s*임\s*연\s*구\s*원",
    r"수\s*석\s*위\s*원", r"책\s*임\s*[위윈]\s*원",
    r"연\s*구\s*위\s*원", r"연\s*구\s*교\s*수", r"연\s*구\s*원",
    r"교\s*수", r"\d+급", r"[:\s]+",
]
_TITLE_WORDS = {"연구원", "연구위원", "수석위원", "책임위원", "연구교수", "교수"}
_SKIP = {"귀하", "합니다", "연구", "보고", "공사", "제출문", "수행한", "포털",
         "연구원", "연구소", "대학교", "연구교수", "교수"}


def extract_authors(page_text: str) -> list[str]:
    lines = page_text.split("\n")
    has_keyword = any("연구책임자" in l or "연구수행자" in l for l in lines)
    if not has_keyword:
        return []

    in_section = False
    authors: list[str] = []

    for line in lines:
        line = line.strip()
        if not line or re.match(r"^-\s*\d+\s*-$", line):
            continue
        if "연구책임자" in line or "연구수행자" in line:
            in_section = True
        if not in_section:
            continue

        cleaned = line
        for pat in _TITLE_PATTERNS:
            cleaned = re.sub(pat, " ", cleaned)
        cleaned = cleaned.strip()

        m = re.search(r"([가-힣])\s([가-힣])\s([가-힣])(?:\s([가-힣]))?\s*$", cleaned)
        if m:
            name = "".join(g for g in m.groups() if g)
            if name not in authors and name not in _TITLE_WORDS:
                authors.append(name)
        else:
            m2 = re.search(r"([가-힣]{3,4})\s*$", cleaned)
            if m2:
                name = m2.group(1)
                if name not in authors and name not in _SKIP:
                    authors.append(name)

        if re.match(r"^-\s*\d", line):
            in_section = False

    # 4자 이름 대응: 끝 3글자 트리밍 (원본 스크립트 동작 유지)
    authors = [a[-3:] for a in authors]
    seen: set[str] = set()
    return [a for a in authors if len(a) >= 3 and not (a in seen or seen.add(a))]


# ────────────────────────────────────────────────────────────
# 요약문 (KR/EN)
# ────────────────────────────────────────────────────────────
_END_MARKERS = re.compile(
    r"^(?:차\s*례|목\s*차|표\s*차\s*례|그\s*림\s*차\s*례|Contents|제\s*1\s*장|Chapter\s*1)",
    re.MULTILINE | re.IGNORECASE,
)


def extract_summary(pages_text: list[str]) -> dict:
    summary_kr = ""
    summary_en = ""
    full = "\n".join(pages_text[:30])
    end = _END_MARKERS.search(full)
    searchable = full[: end.start()] if end else full

    # KR
    kr_end = r"(?=S\s*U\s*M\s*M\s*A\s*R\s*Y|ABSTRACT|Abstract|\Z)"
    for pat in (r"요\s*약\s*문\s*\n(.*?)" + kr_end, r"요약문\s*\n(.*?)" + kr_end):
        m = re.search(pat, searchable, re.DOTALL)
        if m:
            summary_kr = _clean(m.group(1))
            break

    # EN
    for pat in (
        r"S\s*U\s*M\s*M\s*A\s*R\s*Y\s*\n(.*?)(?=\Z)",
        r"(?:ABSTRACT|Abstract)\s*\n(.*?)(?=\Z)",
        r"(?:영문\s*요약|English\s*Summary)\s*\n(.*?)(?=\Z)",
    ):
        m = re.search(pat, searchable, re.DOTALL)
        if m:
            summary_en = _clean(m.group(1))
            break

    return {"summary_kr": summary_kr, "summary_en": summary_en}


def _find_page(pages_text: list[str], keywords: list[str]) -> int:
    for i, text in enumerate(pages_text):
        if any(kw in text for kw in keywords):
            return i
    return -1


# ────────────────────────────────────────────────────────────
# 단일 PDF 처리
# ────────────────────────────────────────────────────────────
@dataclass
class PdfMetadata:
    file: str
    title_kr: str = ""
    title_en: str = ""
    date: str = ""
    authors: list[str] = None
    summary_kr: str = ""
    summary_en: str = ""
    error: Optional[str] = None

    def __post_init__(self):
        if self.authors is None:
            self.authors = []


def extract_pdf_metadata(pdf_path: str | Path) -> PdfMetadata:
    pdf_path = Path(pdf_path)
    meta = PdfMetadata(file=pdf_path.name)
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages_text = [(p.extract_text() or "") for p in pdf.pages[:30]]
            if not pages_text:
                meta.error = "텍스트 추출 실패"
                return meta

            # 표지 후보: 앞 3페이지 중 한글 있음 & 짧음 & 제출문 아님
            candidates: list[tuple[int, int]] = []
            for i, text in enumerate(pages_text[:3]):
                has_kr = any("\uAC00" <= c <= "\uD7A3" for c in text)
                if has_kr and len(text) < 600 and "제 출 문" not in text and "제출문" not in text:
                    candidates.append((i, len(text)))
            title_idx = min(candidates, key=lambda x: x[1])[0] if candidates else 0

            ti = extract_title_and_date(pages_text[title_idx])
            meta.title_kr = ti["title_kr"]
            meta.title_en = ti["title_en"]
            meta.date = ti["date"]

            sub_idx = _find_page(pages_text, ["제 출 문", "제출문"])
            if sub_idx >= 0:
                meta.authors = extract_authors(pages_text[sub_idx])

            sm = extract_summary(pages_text)
            meta.summary_kr = sm["summary_kr"]
            meta.summary_en = sm["summary_en"]

    except Exception as e:
        meta.error = str(e)

    return meta


# ────────────────────────────────────────────────────────────
# 메인 run
# ────────────────────────────────────────────────────────────
def run(
    pdf_dir: str | Path,
    csv_out: str | Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> list[PdfMetadata]:
    log = on_log or (lambda s: print(s, flush=True))
    progress = on_progress or (lambda c, t, m: None)

    pdf_dir = Path(pdf_dir)
    if not pdf_dir.exists():
        raise FileNotFoundError(f"폴더 없음: {pdf_dir}")

    pdf_files = sorted(pdf_dir.glob("**/*.pdf"))
    total = len(pdf_files)
    log(f"TOTAL:{total}")
    progress(0, total, "시작")

    results: list[PdfMetadata] = []
    for idx, f in enumerate(pdf_files, start=1):
        log(f"PROGRESS:{idx}/{total} {f.name}")
        progress(idx, total, f.name)
        meta = extract_pdf_metadata(f)
        status = "[OK]" if not meta.error else f"[ERR] {meta.error}"
        log(f"STATUS:{status}")
        results.append(meta)

    # CSV 저장
    csv_out = Path(csv_out)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_out, "w", encoding="utf-8-sig", newline="") as f:
        if results:
            fieldnames = list(asdict(results[0]).keys())
        else:
            fieldnames = ["file", "title_kr", "title_en", "date", "authors",
                          "summary_kr", "summary_en", "error"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = asdict(r)
            row["authors"] = ", ".join(r.authors or [])
            writer.writerow(row)

    log(f"CSV saved: {csv_out} ({len(results)})")
    return results


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from datetime import datetime as _dt
    _today = os.environ.get("RUN_DATE", _dt.now().strftime("%Y%m%d"))
    target = os.environ.get("DOWNLOAD_DIR", f"./downloads_{_today}")
    out_csv = os.environ.get("METADATA_CSV", f"2_extracted_metadata_{_today}.csv")
    run(target, out_csv)
