"""
K-water ALIO 연구보고서 자동화 파이프라인 (Streamlit Cloud 배포 버전)

실행(로컬): streamlit run streamlit_app.py
배포     : GitHub에 푸시 → share.streamlit.io 에서 연결

기능:
  1) ALIO 크롤링 (Selenium; Cloud에서도 packages.txt로 chromium 사용 가능)
  2) PDF 업로드(크롤링 대체) → 메타데이터 추출
  3) OpenAI GPT-4o 로 K-water 기술분류체계 매핑
  4) PDF 개별/일괄 다운로드
  5) 결과 CSV/ZIP 다운로드
"""
from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from pipeline import crawler, preprocessor, classifier
from pipeline.utils import (
    count_csv_rows,
    count_files,
    ensure_dir,
    human_size,
    list_files,
    make_zip_bytes,
)

# ────────────────────────────────────────────────────────────
# 페이지 설정
# ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Alio 보고서 파이프라인",
    page_icon="💧",
    layout="wide",
)

# ────────────────────────────────────────────────────────────
# 스타일
# ────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #f0f4f8; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stSidebar"] { background: #0a2540; }
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] .stMarkdown { color: white !important; }
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] textarea,
[data-testid="stSidebar"] [data-baseweb="select"] > div { color: #1a1a2e !important; background: white !important; }

.header-card {
    background: linear-gradient(135deg, #0a2540 0%, #1a3a5c 100%);
    border-radius: 16px;
    padding: 28px 36px;
    margin-bottom: 24px;
    color: white;
}
.header-card h1 { margin: 0; font-size: 1.8rem; font-weight: 700; }
.header-card p  { margin: 6px 0 0; color: #90caf9; font-size: 0.95rem; }

.step-card {
    background: white;
    border-radius: 12px;
    padding: 20px 24px;
    border-left: 5px solid #ccc;
    box-shadow: 0 2px 8px rgba(0,0,0,0.07);
}
.step-card.done   { border-left-color: #2e7d32; }
.step-card.active { border-left-color: #1565c0; }
.step-card.wait   { border-left-color: #bdbdbd; }
.step-num   { font-size: 0.75rem; color: #888; font-weight: 600; letter-spacing: 1px; }
.step-title { font-size: 1.05rem; font-weight: 700; color: #1a1a2e; margin: 4px 0; }
.step-desc  { font-size: 0.82rem; color: #666; }
.step-badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; margin-top: 8px; }
.badge-done   { background: #e8f5e9; color: #2e7d32; }
.badge-active { background: #e3f2fd; color: #1565c0; }
.badge-wait   { background: #f5f5f5; color: #9e9e9e; }

.metric-card { background: white; border-radius: 12px; padding: 20px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.07); }
.metric-num   { font-size: 2.2rem; font-weight: 800; color: #0a2540; }
.metric-label { font-size: 0.85rem; color: #888; margin-top: 4px; }

.log-box {
    background: #1e1e2e;
    color: #a6e3a1;
    font-family: 'Courier New', monospace;
    font-size: 0.82rem;
    padding: 16px;
    border-radius: 8px;
    max-height: 300px;
    overflow-y: auto;
    white-space: pre-wrap;
}

.sidebar-credit {
    position: fixed;
    bottom: 12px;
    left: 12px;
    font-size: 0.70rem;
    color: #90caf9 !important;
    opacity: 0.75;
    letter-spacing: 0.2px;
    z-index: 1000;
}
</style>
""", unsafe_allow_html=True)


# ────────────────────────────────────────────────────────────
# 환경 감지 (Cloud vs 로컬)
# ────────────────────────────────────────────────────────────
def is_streamlit_cloud() -> bool:
    """Streamlit Cloud 환경 추정."""
    return bool(os.environ.get("STREAMLIT_SERVER_HEADLESS") or
                os.environ.get("HOSTNAME", "").startswith("streamlit-"))


def get_openai_key() -> str:
    """Secrets → 환경변수 순으로 API 키 로드."""
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return str(st.secrets["OPENAI_API_KEY"])
    except Exception:
        pass
    return os.environ.get("OPENAI_API_KEY", "")


# ────────────────────────────────────────────────────────────
# 세션 작업 디렉토리 (Cloud에선 tempfile, 로컬은 지정 폴더)
# ────────────────────────────────────────────────────────────
if "work_dir" not in st.session_state:
    # Cloud: 세션별 tempdir / 로컬: 현재 폴더
    if is_streamlit_cloud():
        st.session_state.work_dir = tempfile.mkdtemp(prefix="alio_")
    else:
        st.session_state.work_dir = str(Path.cwd())

for key, default in {
    "log_crawl": "", "log_preprocess": "", "log_classify": "",
    "chat_history": [],
    "download_dir": "", "metadata_csv": "", "classified_csv": "",
}.items():
    st.session_state.setdefault(key, default)

TODAY = datetime.now().strftime("%Y%m%d")


# ────────────────────────────────────────────────────────────
# org_list 로드
# ────────────────────────────────────────────────────────────
@st.cache_data
def load_org_list() -> list[str]:
    for path in ("data/org_list.json", "org_list.json"):
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return ["한국수자원공사"]


ORG_LIST = load_org_list()


# ────────────────────────────────────────────────────────────
# 사이드바
# ────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ 설정")
    st.markdown("---")

    st.markdown("**크롤링 대상**")
    default_idx = ORG_LIST.index("한국수자원공사") if "한국수자원공사" in ORG_LIST else 0
    org_name = st.selectbox(
        "기관명 선택",
        options=ORG_LIST,
        index=default_idx,
        help="ALIO에서 검색할 공공기관명",
    )

    st.markdown("---")
    st.markdown("**경로 설정**")
    work_dir = st.text_input(
        "작업 폴더",
        value=st.session_state.work_dir,
        help="Cloud 환경은 세션 종료 시 초기화됩니다.",
    )
    st.session_state.work_dir = work_dir
    work_path = Path(work_dir)

    safe_org = org_name.strip().replace(" ", "_")
    default_dl = str(work_path / f"downloads_{safe_org}_{TODAY}")
    download_dir = st.text_input("PDF 저장 폴더", value=default_dl)
    metadata_csv = st.text_input(
        "메타데이터 CSV",
        value=str(work_path / f"2_extracted_metadata_{TODAY}.csv"),
    )
    classified_csv = st.text_input(
        "분류 결과 CSV",
        value=str(work_path / f"3_classified_reports_{TODAY}.csv"),
    )

    st.session_state.download_dir = download_dir
    st.session_state.metadata_csv = metadata_csv
    st.session_state.classified_csv = classified_csv

    st.markdown("---")
    st.markdown("**환경**")
    if is_streamlit_cloud():
        st.info("☁️ Streamlit Cloud — 데이터는 세션 종료 시 소실됩니다.")
    else:
        st.success("💻 로컬 환경")

    if not get_openai_key():
        st.warning("⚠️ OpenAI API 키 없음. `.streamlit/secrets.toml`에 설정하세요.")

    st.markdown("---")
    st.markdown("**실행 순서**")
    st.markdown("1. 크롤링 (또는 PDF 업로드)\n2. 전처리\n3. AI 분류")

    # 좌하단 크레딧
    st.markdown(
        '<div class="sidebar-credit">originally generated by '
        'wjelly@kwater.or.kr</div>',
        unsafe_allow_html=True,
    )


# ────────────────────────────────────────────────────────────
# 헤더
# ────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="header-card">
    <h1>💧 Alio 보고서 파이프라인</h1>
    <p>크롤링 → PDF 전처리 → AI 기술분류 | 대상: <b>{org_name}</b></p>
</div>
""", unsafe_allow_html=True)


# ────────────────────────────────────────────────────────────
# 파이프라인 상태 카드
# ────────────────────────────────────────────────────────────
def step_status(has_output: bool, prereq_done: bool) -> str:
    if has_output:
        return "done"
    if prereq_done:
        return "active"
    return "wait"


def badge_html(status: str) -> str:
    labels = {"done": "완료", "active": "실행 가능", "wait": "대기중"}
    return f'<span class="step-badge badge-{status}">{labels[status]}</span>'


pdfs_exist = count_files(download_dir) > 0
metadata_exists = Path(metadata_csv).exists()
classified_exists = Path(classified_csv).exists()

s1 = step_status(pdfs_exist, True)
s2 = step_status(metadata_exists, pdfs_exist)
s3 = step_status(classified_exists, metadata_exists)

col1, col2, col3 = st.columns(3)
with col1:
    st.markdown(f"""
    <div class="step-card {s1}">
        <div class="step-num">STEP 01</div>
        <div class="step-title">크롤링 / 업로드</div>
        <div class="step-desc">ALIO에서 PDF 자동 수집 또는 직접 업로드</div>
        {badge_html(s1)}
        <div class="step-desc" style="margin-top:8px">PDF: <b>{count_files(download_dir)}개</b></div>
    </div>""", unsafe_allow_html=True)

with col2:
    st.markdown(f"""
    <div class="step-card {s2}">
        <div class="step-num">STEP 02</div>
        <div class="step-title">PDF 전처리</div>
        <div class="step-desc">제목·저자·날짜·요약문 메타데이터 추출</div>
        {badge_html(s2)}
        <div class="step-desc" style="margin-top:8px">추출: <b>{count_csv_rows(metadata_csv) if metadata_exists else 0}건</b></div>
    </div>""", unsafe_allow_html=True)

with col3:
    st.markdown(f"""
    <div class="step-card {s3}">
        <div class="step-num">STEP 03</div>
        <div class="step-title">AI 기술분류</div>
        <div class="step-desc">GPT-4o로 K-water 기술분류체계 자동 분류</div>
        {badge_html(s3)}
        <div class="step-desc" style="margin-top:8px">분류: <b>{count_csv_rows(classified_csv) if classified_exists else 0}건</b></div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)


# ────────────────────────────────────────────────────────────
# 실행 헬퍼 (진행률 + 로그 콜백)
# ────────────────────────────────────────────────────────────
def _update_log_box(placeholder, lines: list[str], max_n: int = 40) -> None:
    placeholder.markdown(
        '<div class="log-box">' + "\n".join(lines[-max_n:]) + "</div>",
        unsafe_allow_html=True,
    )


def run_with_progress(
    fn, *, label: str, spinner_text: str, log_area, progress_area, status_area,
    log_key: str,
):
    """fn(on_log, on_progress) 를 실행하며 UI 업데이트."""
    log_lines: list[str] = []
    log_q: queue.Queue = queue.Queue()
    state = {"progress": (0, 0, ""), "result": None, "error": None, "done": False}

    def on_log(line: str):
        log_q.put(("log", line))

    def on_progress(cur: int, total: int, msg: str):
        log_q.put(("progress", cur, total, msg))

    def worker():
        try:
            state["result"] = fn(on_log=on_log, on_progress=on_progress)
        except Exception as e:
            state["error"] = str(e)
        finally:
            state["done"] = True

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    pbar = progress_area.progress(0, text=f"{spinner_text} 준비 중...")
    while not state["done"] or not log_q.empty():
        try:
            item = log_q.get(timeout=0.1)
        except queue.Empty:
            continue

        if item[0] == "log":
            line = item[1]
            log_lines.append(line)
            _update_log_box(log_area, log_lines)
        elif item[0] == "progress":
            _, cur, total, msg = item
            if total > 0:
                pbar.progress(min(cur / total, 1.0), text=f"{spinner_text} {cur}/{total} · {msg[:60]}")
            else:
                status_area.markdown(f"`{msg}`")

    th.join(timeout=1.0)
    pbar.progress(1.0, text=f"{label} 완료")
    st.session_state[log_key] = "\n".join(log_lines)

    if state["error"]:
        st.error(f"{label} 오류: {state['error']}")
        return None
    return state["result"]


# ────────────────────────────────────────────────────────────
# 실행 버튼 구역
# ────────────────────────────────────────────────────────────
st.markdown("### 🚀 실행")
bc1, bc2, bc3, bc4 = st.columns(4)
run_crawl = bc1.button("▶ 크롤링", use_container_width=True)
run_preproc = bc2.button("▶ 전처리", use_container_width=True)
run_classify = bc3.button("▶ AI 분류", use_container_width=True)
run_all = bc4.button("⚡ 전체 실행", use_container_width=True, type="primary")

log_area = st.empty()
progress_area = st.empty()
status_area = st.empty()


# ── 크롤링 ──────────────────────────────────────────────────
def _do_crawl(on_log, on_progress):
    ensure_dir(download_dir)
    headless_flag = is_streamlit_cloud()  # Cloud는 무조건 헤드리스
    return crawler.run(
        organ_name=org_name,
        out_dir=download_dir,
        headless=headless_flag,
        on_log=on_log,
        on_progress=on_progress,
    )


if run_crawl or run_all:
    with st.spinner(f"크롤링 중... ({org_name})"):
        try:
            result = run_with_progress(
                _do_crawl,
                label="크롤링",
                spinner_text="크롤링 중",
                log_area=log_area, progress_area=progress_area, status_area=status_area,
                log_key="log_crawl",
            )
            if result is not None:
                st.success(f"크롤링 완료! PDF {count_files(download_dir)}개")
        except ImportError as e:
            st.error(f"Selenium / Chrome 관련 오류: {e}\n\n"
                     "Cloud 환경이라면 packages.txt에 `chromium chromium-driver` 추가 필요. "
                     "또는 아래 **PDF 업로드** 를 사용하세요.")
        except Exception as e:
            st.error(f"크롤링 실패: {e}")


# ── 전처리 ──────────────────────────────────────────────────
def _do_preprocess(on_log, on_progress):
    return preprocessor.run(
        pdf_dir=download_dir,
        csv_out=metadata_csv,
        on_log=on_log,
        on_progress=on_progress,
    )


if run_preproc or run_all:
    if count_files(download_dir) == 0:
        st.warning("PDF가 없습니다. 먼저 크롤링하거나 아래 업로드 기능을 사용하세요.")
    else:
        run_with_progress(
            _do_preprocess,
            label="전처리",
            spinner_text="처리 중",
            log_area=log_area, progress_area=progress_area, status_area=status_area,
            log_key="log_preprocess",
        )
        if Path(metadata_csv).exists():
            st.success(f"전처리 완료! {count_csv_rows(metadata_csv)}건 추출")


# ── 분류 ────────────────────────────────────────────────────
def _do_classify(on_log, on_progress):
    return classifier.run(
        input_csv=metadata_csv,
        output_csv=classified_csv,
        api_key=get_openai_key(),
        on_log=on_log,
        on_progress=on_progress,
    )


if run_classify or run_all:
    if not Path(metadata_csv).exists():
        st.warning("메타데이터 CSV가 없습니다. STEP 02를 먼저 실행하세요.")
    elif not get_openai_key():
        st.error("OpenAI API 키가 설정되지 않았습니다. `.streamlit/secrets.toml` 또는 "
                 "환경변수 `OPENAI_API_KEY`를 설정하세요.")
    else:
        run_with_progress(
            _do_classify,
            label="분류",
            spinner_text="분류 중",
            log_area=log_area, progress_area=progress_area, status_area=status_area,
            log_key="log_classify",
        )
        if Path(classified_csv).exists():
            st.success(f"분류 완료! {count_csv_rows(classified_csv)}건 분류")


# ────────────────────────────────────────────────────────────
# 결과 탭 (사용방법 · PDF 다운로드 · 메타데이터 · 분류결과 · AI 채팅 · 로그)
# ────────────────────────────────────────────────────────────
st.markdown("---")
tab_help, tab_pdf, tab_meta, tab_class, tab_chat, tab_log = st.tabs([
    "❓ 사용방법", "📥 PDF 다운로드", "📄 메타데이터",
    "📊 분류 결과", "💬 AI 채팅", "📋 로그",
])


# ── ❓ 사용방법 ──────────────────────────────────────────────
with tab_help:
    st.markdown("""
    ## 사용방법

    ### 1. 설정
    - **사이드바**에서 대상 기관 선택 (ALIO 등록 공공기관 300여 개)
    - 작업 폴더/CSV 경로는 보통 기본값 그대로 사용

    ### 2. PDF 확보 (둘 중 택 1)
    #### A. 자동 크롤링
    - **▶ 크롤링** 버튼 → Selenium이 ALIO 사이트에서 PDF 자동 수집
    - ⚠️ Streamlit Cloud에서는 `packages.txt`에 `chromium`과 `chromium-driver`가 필요

    #### B. 직접 업로드 (Cloud에서 크롤링 불가할 때)
    - **📥 PDF 다운로드** 탭 하단의 업로드 영역에 PDF를 끌어다 놓기

    ### 3. 전처리 & 분류
    - **▶ 전처리** → 제목 / 저자 / 날짜 / 요약문 추출 (CSV 저장)
    - **▶ AI 분류** → GPT-4o가 K-water 대분류·중분류 매핑

    ### 4. 결과 확인 / 다운로드
    - 📄 메타데이터, 📊 분류 결과 탭에서 확인
    - CSV / PDF / 전체 ZIP 다운로드 지원
    - 💬 AI 채팅 탭에서 자연어로 질의 가능

    ### 5. API 키 설정
    - **로컬**: 프로젝트 루트에 `.env` 생성 → `OPENAI_API_KEY=sk-...`
    - **Streamlit Cloud**: 앱 설정 → Secrets → `OPENAI_API_KEY = "sk-..."`

    ### 6. Streamlit Cloud 배포
    1. 이 코드를 GitHub 저장소에 푸시
    2. [share.streamlit.io](https://share.streamlit.io) → New app
    3. 저장소 선택 → 메인 파일: `streamlit_app.py`
    4. Secrets에 OpenAI 키 등록
    5. 배포 완료 (URL은 `<앱이름>.streamlit.app` 형태)
    """)


# ── 📥 PDF 다운로드 탭 ──────────────────────────────────────
with tab_pdf:
    st.markdown("### 📥 PDF 관리")

    # 업로드 영역 (Cloud 대응 / 크롤링 대체)
    with st.expander("📤 PDF 직접 업로드 (크롤링 대신)", expanded=not pdfs_exist):
        st.caption("크롤링 없이 로컬 PDF를 직접 업로드하면 바로 전처리/분류를 시작할 수 있습니다.")
        uploaded = st.file_uploader(
            "PDF 파일 선택 (다중 가능)",
            type=["pdf"],
            accept_multiple_files=True,
            key="pdf_uploader",
        )
        if uploaded:
            ensure_dir(download_dir)
            saved_count = 0
            for f in uploaded:
                out = Path(download_dir) / f.name
                out.write_bytes(f.read())
                saved_count += 1
            st.success(f"{saved_count}개 PDF가 `{download_dir}` 에 저장되었습니다. 전처리를 실행하세요.")
            st.rerun()

    # 수집된 PDF 목록
    pdf_files = list_files(download_dir, exts=(".pdf", ".hwp", ".hwpx", ".docx"))
    st.markdown(f"#### 수집된 보고서: **{len(pdf_files)}개**")

    if not pdf_files:
        st.info("아직 수집된 PDF가 없습니다.")
    else:
        # 전체 ZIP 다운로드
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("📦 전체 ZIP 다운로드 준비", use_container_width=True):
                with st.spinner("ZIP 생성 중..."):
                    zip_bytes = make_zip_bytes(pdf_files, base_folder=download_dir)
                    st.session_state["zip_cache"] = zip_bytes
                    st.session_state["zip_name"] = f"{safe_org}_{TODAY}_{len(pdf_files)}files.zip"
        with c2:
            if "zip_cache" in st.session_state:
                st.download_button(
                    f"💾 {st.session_state.get('zip_name', 'pdfs.zip')} 다운로드 "
                    f"({human_size(len(st.session_state['zip_cache']))})",
                    data=st.session_state["zip_cache"],
                    file_name=st.session_state["zip_name"],
                    mime="application/zip",
                    use_container_width=True,
                )

        st.markdown("#### 개별 파일")
        # 페이지네이션
        PER_PAGE = 20
        total_pages = (len(pdf_files) + PER_PAGE - 1) // PER_PAGE
        page = st.number_input(
            f"페이지 (전체 {total_pages}페이지)",
            min_value=1, max_value=max(total_pages, 1), value=1, step=1,
        )
        start = (page - 1) * PER_PAGE
        end = start + PER_PAGE

        for i, f in enumerate(pdf_files[start:end], start=start + 1):
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            fc1, fc2, fc3 = st.columns([5, 1, 1])
            fc1.markdown(f"**{i}.** `{f.name}`")
            fc2.markdown(f"<small>{human_size(size)}</small>", unsafe_allow_html=True)
            try:
                with open(f, "rb") as fp:
                    fc3.download_button(
                        "⬇️", data=fp.read(),
                        file_name=f.name,
                        mime="application/pdf",
                        key=f"dl_{i}",
                        use_container_width=True,
                    )
            except Exception as e:
                fc3.caption(f"오류: {e}")


# ── 📄 메타데이터 탭 ────────────────────────────────────────
with tab_meta:
    if Path(metadata_csv).exists():
        try:
            df_meta = pd.read_csv(metadata_csv, encoding="utf-8-sig")
            st.markdown(f"총 **{len(df_meta)}건** 추출됨")
            show = [c for c in ["file", "title_kr", "title_en", "date", "authors",
                                "summary_kr", "summary_en"] if c in df_meta.columns]
            st.dataframe(df_meta[show], use_container_width=True, height=500)
            st.download_button(
                "📥 메타데이터 CSV 다운로드",
                data=df_meta.to_csv(index=False).encode("utf-8-sig"),
                file_name=Path(metadata_csv).name,
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"파일 읽기 오류: {e}")
    else:
        st.info("아직 메타데이터가 없습니다. STEP 02를 실행하세요.")


# ── 📊 분류 결과 탭 ─────────────────────────────────────────
with tab_class:
    if Path(classified_csv).exists():
        try:
            df = pd.read_csv(classified_csv, encoding="utf-8-sig")

            m1, m2, m3, m4 = st.columns(4)
            m1.markdown(
                f'<div class="metric-card"><div class="metric-num">{len(df)}</div>'
                f'<div class="metric-label">전체 보고서</div></div>', unsafe_allow_html=True)
            m2.markdown(
                f'<div class="metric-card"><div class="metric-num">'
                f'{df["대분류"].nunique() if "대분류" in df.columns else 0}</div>'
                f'<div class="metric-label">대분류 수</div></div>', unsafe_allow_html=True)
            m3.markdown(
                f'<div class="metric-card"><div class="metric-num">'
                f'{df["중분류"].nunique() if "중분류" in df.columns else 0}</div>'
                f'<div class="metric-label">중분류 수</div></div>', unsafe_allow_html=True)
            err_cnt = int(df["분류오류"].astype(str).str.len().gt(0).sum()) if "분류오류" in df.columns else 0
            m4.markdown(
                f'<div class="metric-card"><div class="metric-num">{err_cnt}</div>'
                f'<div class="metric-label">오류 건수</div></div>', unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            if "대분류" in df.columns:
                try:
                    import plotly.express as px
                    cc1, cc2 = st.columns(2)
                    with cc1:
                        st.markdown("#### 대분류별 보고서 수")
                        cat = df["대분류"].value_counts().reset_index()
                        cat.columns = ["대분류", "건수"]
                        fig1 = px.bar(
                            cat, x="대분류", y="건수", color="대분류",
                            color_discrete_sequence=px.colors.qualitative.Set2, text="건수",
                        )
                        fig1.update_layout(showlegend=False, height=350,
                                           plot_bgcolor="white", paper_bgcolor="white")
                        fig1.update_traces(textposition="outside")
                        st.plotly_chart(fig1, use_container_width=True)

                    with cc2:
                        st.markdown("#### 대분류 비율")
                        fig2 = px.pie(
                            cat, names="대분류", values="건수",
                            color_discrete_sequence=px.colors.qualitative.Set2, hole=0.4,
                        )
                        fig2.update_layout(height=350, paper_bgcolor="white")
                        st.plotly_chart(fig2, use_container_width=True)

                    st.markdown("#### 중분류별 보고서 수")
                    sub = df["중분류"].value_counts().reset_index()
                    sub.columns = ["중분류", "건수"]
                    fig3 = px.bar(
                        sub, x="중분류", y="건수", color="건수",
                        color_continuous_scale="Blues", text="건수",
                    )
                    fig3.update_layout(height=400, plot_bgcolor="white", paper_bgcolor="white")
                    fig3.update_traces(textposition="outside")
                    st.plotly_chart(fig3, use_container_width=True)
                except ImportError:
                    st.warning("차트 표시: `pip install plotly` 필요")

            st.markdown("#### 전체 분류 결과")
            show_cols = [c for c in ["file", "title_kr", "date", "authors",
                                     "대분류", "중분류", "분류근거"] if c in df.columns]
            st.dataframe(df[show_cols], use_container_width=True, height=400)

            st.download_button(
                "📥 분류 결과 CSV 다운로드",
                data=df.to_csv(index=False).encode("utf-8-sig"),
                file_name=Path(classified_csv).name,
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"파일 읽기 오류: {e}")
    else:
        st.info("아직 분류 결과가 없습니다. STEP 03을 실행하세요.")


# ── 💬 AI 채팅 탭 ───────────────────────────────────────────
with tab_chat:
    if not Path(classified_csv).exists():
        st.info("분류 결과가 없습니다. STEP 03을 먼저 실행하세요.")
    elif not get_openai_key():
        st.error("OpenAI API 키가 설정되지 않았습니다.")
    else:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=get_openai_key())

            df_chat = pd.read_csv(classified_csv, encoding="utf-8-sig")
            keep = [c for c in ["file", "title_kr", "date", "authors", "대분류",
                                "중분류", "분류근거", "summary_kr"] if c in df_chat.columns]
            df_ctx = df_chat[keep].copy()
            if "summary_kr" in df_ctx.columns:
                df_ctx["summary_kr"] = df_ctx["summary_kr"].astype(str).str[:100]
            data_ctx = df_ctx.to_csv(index=False)

            system_prompt = (
                "당신은 K-water 연구보고서 데이터를 분석하는 전문 어시스턴트입니다.\n"
                "아래는 수집된 연구보고서 목록(CSV)입니다. 이 데이터를 바탕으로 답변하세요.\n"
                "숫자나 통계를 물으면 정확히 세어서 답하고, 특정 보고서는 제목과 날짜를 함께 알려주세요.\n\n"
                f"[보고서 데이터]\n{data_ctx}"
            )

            st.markdown(f"#### {org_name} 연구보고서 AI 채팅")
            st.caption(f"총 {len(df_chat)}건의 분류 완료 보고서를 기반으로 질문하세요.")

            e1, e2, e3, e4 = st.columns(4)
            if e1.button("수자원 보고서 몇 건?", use_container_width=True):
                st.session_state.chat_history.append(
                    {"role": "user", "content": "수자원 대분류 보고서는 총 몇 건인가요?"})
            if e2.button("가장 최근 보고서는?", use_container_width=True):
                st.session_state.chat_history.append(
                    {"role": "user", "content": "가장 최근에 발행된 보고서 3건을 알려주세요."})
            if e3.button("대분류별 통계", use_container_width=True):
                st.session_state.chat_history.append(
                    {"role": "user", "content": "대분류별 보고서 건수를 표로 정리해주세요."})
            if e4.button("에너지 분야 목록", use_container_width=True):
                st.session_state.chat_history.append(
                    {"role": "user", "content": "에너지 대분류 보고서 목록을 알려주세요."})

            for msg in st.session_state.chat_history:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

            user_input = st.chat_input("보고서에 대해 무엇이든 물어보세요...")

            # 예시 버튼으로 질문 추가된 경우 or 사용자 입력
            pending = user_input or (
                st.session_state.chat_history[-1]["content"]
                if st.session_state.chat_history
                and st.session_state.chat_history[-1]["role"] == "user"
                and (len(st.session_state.chat_history) < 2
                     or st.session_state.chat_history[-2]["role"] == "assistant")
                else None
            )

            if user_input:
                st.session_state.chat_history.append({"role": "user", "content": user_input})
                with st.chat_message("user"):
                    st.markdown(user_input)

            if pending:
                with st.chat_message("assistant"):
                    with st.spinner("답변 준비 중..."):
                        messages = [{"role": "system", "content": system_prompt}] + \
                                   st.session_state.chat_history[-10:]
                        resp = client.chat.completions.create(
                            model="gpt-4o",
                            messages=messages,
                            max_tokens=1000,
                            temperature=0.3,
                        )
                        answer = resp.choices[0].message.content
                    st.markdown(answer)
                    st.session_state.chat_history.append({"role": "assistant", "content": answer})

            if st.session_state.chat_history:
                if st.button("🗑️ 대화 초기화", type="secondary"):
                    st.session_state.chat_history = []
                    st.rerun()

        except Exception as e:
            st.error(f"채팅 오류: {e}")


# ── 📋 로그 탭 ──────────────────────────────────────────────
with tab_log:
    lt1, lt2, lt3 = st.tabs(["크롤링 로그", "전처리 로그", "분류 로그"])
    with lt1:
        st.markdown(
            f'<div class="log-box">{st.session_state.log_crawl or "아직 실행 기록이 없습니다."}</div>',
            unsafe_allow_html=True)
    with lt2:
        st.markdown(
            f'<div class="log-box">{st.session_state.log_preprocess or "아직 실행 기록이 없습니다."}</div>',
            unsafe_allow_html=True)
    with lt3:
        st.markdown(
            f'<div class="log-box">{st.session_state.log_classify or "아직 실행 기록이 없습니다."}</div>',
            unsafe_allow_html=True)
