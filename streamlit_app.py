"""
K-water ALIO 연구보고서 자동화 파이프라인 (Streamlit Cloud 배포 버전)

실행(로컬): streamlit run streamlit_app.py
배포     : GitHub에 푸시 → share.streamlit.io 에서 연결
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

# 로컬 파이프라인 모듈 임포트
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
# 1. 페이지 설정 및 스타일
# ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Alio 보고서 파이프라인",
    page_icon="💧",
    layout="wide",
)

st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #f0f4f8; }
[data-testid="stSidebar"] { background: #0a2540; }
[data-testid="stSidebar"] p, span, label, h1, h2, h3, .stMarkdown { color: white !important; }
[data-testid="stSidebar"] input, textarea, [data-baseweb="select"] > div { color: #1a1a2e !important; background: white !important; }

.header-card {
    background: linear-gradient(135deg, #0a2540 0%, #1a3a5c 100%);
    border-radius: 16px; padding: 28px 36px; margin-bottom: 24px; color: white;
}
.header-card h1 { margin: 0; font-size: 1.8rem; font-weight: 700; }
.header-card p  { margin: 6px 0 0; color: #90caf9; font-size: 0.95rem; }

.step-card { background: white; border-radius: 12px; padding: 20px 24px; border-left: 5px solid #ccc; box-shadow: 0 2px 8px rgba(0,0,0,0.07); }
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

.log-box { background: #1e1e2e; color: #a6e3a1; font-family: 'Courier New', monospace; font-size: 0.82rem; padding: 16px; border-radius: 8px; max-height: 300px; overflow-y: auto; white-space: pre-wrap; }
.sidebar-credit { position: fixed; bottom: 12px; left: 12px; font-size: 0.70rem; color: #90caf9 !important; opacity: 0.75; }
</style>
""", unsafe_allow_html=True)


def is_streamlit_cloud() -> bool:
    return bool(os.environ.get("STREAMLIT_SERVER_HEADLESS") or os.environ.get("HOSTNAME", "").startswith("streamlit-"))

def get_openai_key() -> str:
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return str(st.secrets["OPENAI_API_KEY"])
    except Exception:
        pass
    return os.environ.get("OPENAI_API_KEY", "")

# ────────────────────────────────────────────────────────────
# 2. 세션 경로 및 변수 초기화
# ────────────────────────────────────────────────────────────
if "work_dir" not in st.session_state:
    if is_streamlit_cloud():
        st.session_state.work_dir = os.path.join(os.getcwd(), "data_workspace")
    else:
        st.session_state.work_dir = str(Path.cwd())
    os.makedirs(st.session_state.work_dir, exist_ok=True)

for key, default in {
    "log_crawl": "", "log_preprocess": "", "log_classify": "", "chat_history": []
}.items():
    st.session_state.setdefault(key, default)

TODAY = datetime.now().strftime("%Y%m%d")

@st.cache_data
def load_org_list() -> list[str]:
    for path in ("data/org_list.json", "org_list.json"):
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return ["한국수자원공사"]

ORG_LIST = load_org_list()

# ────────────────────────────────────────────────────────────
# 3. 사이드바 설정
# ────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ 설정")
    st.markdown("---")

    default_idx = ORG_LIST.index("한국수자원공사") if "한국수자원공사" in ORG_LIST else 0
    org_name = st.selectbox("기관명 선택", options=ORG_LIST, index=default_idx)

    st.markdown("---")
    st.markdown("**경로 설정**")
    work_dir = st.text_input("작업 폴더", value=st.session_state.work_dir)
    st.session_state.work_dir = work_dir
    work_path = Path(work_dir)

    safe_org = org_name.strip().replace(" ", "_")
    download_dir = st.text_input("PDF 저장 폴더", value=str(work_path / f"downloads_{safe_org}_{TODAY}"))
    metadata_csv = st.text_input("메타데이터 CSV", value=str(work_path / f"2_extracted_metadata_{TODAY}.csv"))
    classified_csv = st.text_input("분류 결과 CSV", value=str(work_path / f"3_classified_reports_{TODAY}.csv"))

    if is_streamlit_cloud():
        st.info("☁️ Streamlit Cloud 구동 중")
    else:
        st.success("💻 로컬 환경")

    if not get_openai_key():
        st.warning("⚠️ OpenAI API 키 없음. `.streamlit/secrets.toml`을 확인하세요.")

    st.markdown('<div class="sidebar-credit">originally generated by wjelly@kwater.or.kr</div>', unsafe_allow_html=True)


# ────────────────────────────────────────────────────────────
# 4. 헤더 및 상태 카드
# ────────────────────────────────────────────────────────────
st.markdown(f'<div class="header-card"><h1>💧 Alio 보고서 파이프라인</h1><p>대상: <b>{org_name}</b></p></div>', unsafe_allow_html=True)

def step_status(has_output: bool, prereq_done: bool) -> str:
    if has_output: return "done"
    if prereq_done: return "active"
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
        <div class="step-num">STEP 01</div><div class="step-title">크롤링 / 업로드</div>
        {badge_html(s1)}<div class="step-desc" style="margin-top:8px">PDF: <b>{count_files(download_dir)}개</b></div>
    </div>""", unsafe_allow_html=True)

with col2:
    st.markdown(f"""
    <div class="step-card {s2}">
        <div class="step-num">STEP 02</div><div class="step-title">PDF 전처리</div>
        {badge_html(s2)}<div class="step-desc" style="margin-top:8px">추출: <b>{count_csv_rows(metadata_csv) if metadata_exists else 0}건</b></div>
    </div>""", unsafe_allow_html=True)

with col3:
    st.markdown(f"""
    <div class="step-card {s3}">
        <div class="step-num">STEP 03</div><div class="step-title">AI 기술분류</div>
        {badge_html(s3)}<div class="step-desc" style="margin-top:8px">분류: <b>{count_csv_rows(classified_csv) if classified_exists else 0}건</b></div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)


# ────────────────────────────────────────────────────────────
# 5. 파이프라인 실행 스레드 함수
# ────────────────────────────────────────────────────────────
def run_with_progress(fn, *, label: str, spinner_text: str, log_area, progress_area, status_area, log_key: str):
    log_lines: list[str] = []
    log_q: queue.Queue = queue.Queue()
    state = {"progress": (0, 0, ""), "result": None, "error": None, "done": False}

    def on_log(line: str): log_q.put(("log", line))
    def on_progress(cur: int, total: int, msg: str): log_q.put(("progress", cur, total, msg))

    def worker():
        try: state["result"] = fn(on_log=on_log, on_progress=on_progress)
        except Exception as e: state["error"] = str(e)
        finally: state["done"] = True

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    pbar = progress_area.progress(0, text=f"{spinner_text} 준비 중...")
    while not state["done"] or not log_q.empty():
        try: item = log_q.get(timeout=0.1)
        except queue.Empty: continue

        if item[0] == "log":
            log_lines.append(item[1])
            log_area.markdown('<div class="log-box">' + "\n".join(log_lines[-40:]) + '</div>', unsafe_allow_html=True)
        elif item[0] == "progress":
            _, cur, total, msg = item
            if total > 0: pbar.progress(min(cur / total, 1.0), text=f"{spinner_text} {cur}/{total} · {msg[:60]}")
            else: status_area.markdown(f"`{msg}`")

    th.join(timeout=1.0)
    pbar.progress(1.0, text=f"{label} 완료")
    st.session_state[log_key] = "\n".join(log_lines)

    if state["error"]:
        st.error(f"{label} 오류: {state['error']}")
        return None
    return state["result"]


# ────────────────────────────────────────────────────────────
# 6. 메인 실행 버튼 구역 (이곳에서 에러가 났던 UI 플레이스홀더를 고정 선언합니다)
# ────────────────────────────────────────────────────────────
st.markdown("### 🚀 파이프라인 제어 콘솔")
bc1, bc2, bc3, bc4 = st.columns(4)
run_crawl = bc1.button("▶ 크롤링 실행", use_container_width=True)
run_preproc = bc2.button("▶ 데이터 전처리", use_container_width=True)
run_classify = bc3.button("▶ AI 분류", use_container_width=True)
run_all = bc4.button("⚡ 전체 실행", use_container_width=True, type="primary")

# [핵심 수정] NameError 방지: 런타임에 로그와 프로그레스바를 띄워줄 빈 공간 확보
log_area = st.empty()
progress_area = st.empty()
status_area = st.empty()

# -- 크롤링
if run_crawl or run_all:
    ensure_dir(download_dir)
    with st.spinner(f"크롤링 중... ({org_name})"):
        try:
            run_with_progress(
                lambda ol, op: crawler.run(organ_name=org_name, out_dir=download_dir, headless=is_streamlit_cloud(), on_log=ol, on_progress=op),
                label="크롤링", spinner_text="크롤링 중", log_area=log_area, progress_area=progress_area, status_area=status_area, log_key="log_crawl"
            )
            st.rerun()
        except ImportError as e:
            st.error(f"Selenium 에러: {e}. 하단의 'PDF 다운로드' 탭에서 파일을 직접 업로드해 보세요.")
        except Exception as e:
            st.error(f"크롤링 실패: {e}")

# -- 데이터 전처리
if run_preproc or run_all:
    if count_files(download_dir) == 0:
        st.warning("⚠️ 파싱할 PDF가 없습니다. 먼저 크롤링하거나 아래 탭에서 파일을 직접 업로드해 주세요.")
    else:
        run_with_progress(
            lambda ol, op: preprocessor.run(pdf_dir=download_dir, csv_out=metadata_csv, on_log=ol, on_progress=op),
            label="전처리", spinner_text="PDF 처리 중", log_area=log_area, progress_area=progress_area, status_area=status_area, log_key="log_preprocess"
        )
        st.rerun()

# -- AI 기술분류
if run_classify or run_all:
    if not Path(metadata_csv).exists():
        st.warning("메타데이터 CSV가 없습니다. 전처리(STEP 02)를 먼저 실행하세요.")
    elif not get_openai_key():
        st.error("OpenAI API 키가 설정되지 않았습니다.")
    else:
        run_with_progress(
            lambda ol, op: classifier.run(input_csv=metadata_csv, output_csv=classified_csv, api_key=get_openai_key(), on_log=ol, on_progress=op),
            label="AI 분류", spinner_text="분류 중", log_area=log_area, progress_area=progress_area, status_area=status_area, log_key="log_classify"
        )
        st.rerun()


# ────────────────────────────────────────────────────────────
# 7. 결과 탭 (업로드 인터페이스 연동)
# ────────────────────────────────────────────────────────────
st.markdown("---")
tab_help, tab_pdf, tab_meta, tab_class, tab_chat, tab_log = st.tabs([
    "❓ 사용방법", "📥 PDF 다운로드/업로드", "📄 메타데이터", "📊 분류 결과", "💬 AI 채팅", "📋 로그"
])

# ── ❓ 사용방법
with tab_help:
    st.markdown("""
    ### 파이프라인 안내
    1. **PDF 확보**: '▶ 크롤링 실행' 버튼으로 ALIO에서 자동 수집하거나, **'📥 PDF 다운로드/업로드'** 탭에서 로컬 PDF를 끌어다 놓으세요.
    2. **데이터 전처리**: 업로드가 완료되면 상단의 **'▶ 데이터 전처리'**를 눌러 PDF 내용을 CSV 메타데이터로 파싱합니다.
    3. **AI 분류**: 추출된 CSV를 바탕으로 K-water 기술분류체계에 맞게 자동으로 분류합니다.
    """)

# ── 📥 PDF 다운로드/업로드
with tab_pdf:
    st.markdown("### 📥 PDF 관리 및 수동 업로드")
    
    # [핵심 수정] 업로드 된 파일을 download_dir 에 저장하여 파이프라인(전처리기)에 연동
    with st.expander("📤 PDF 직접 업로드 (크롤링 오류 시)", expanded=not pdfs_exist):
        uploaded_pdfs = st.file_uploader("PDF 파일 선택 (다중 가능)", type=["pdf", "hwp", "hwpx"], accept_multiple_files=True, key="pdf_uploader")
        if uploaded_pdfs:
            ensure_dir(download_dir)
            saved_count = 0
            for f in uploaded_pdfs:
                out = Path(download_dir) / f.name
                out.write_bytes(f.read())
                saved_count += 1
            st.success(f"✅ {saved_count}개 파일이 작업 폴더에 저장되었습니다. 이제 상단의 **'▶ 데이터 전처리'** 버튼을 누르면 파싱이 시작됩니다.")
            st.rerun()

    pdf_files = list_files(download_dir, exts=(".pdf", ".hwp", ".hwpx", ".docx"))
    st.markdown(f"#### 현재 작업 폴더 내 파일: **{len(pdf_files)}개**")

    if pdf_files:
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("📦 전체 파일 ZIP 묶기", use_container_width=True):
                with st.spinner("ZIP 생성 중..."):
                    st.session_state["zip_cache"] = make_zip_bytes(pdf_files, base_folder=download_dir)
                    st.session_state["zip_name"] = f"pdfs_{safe_org}_{TODAY}.zip"
        with c2:
            if "zip_cache" in st.session_state:
                st.download_button(
                    f"💾 {st.session_state.get('zip_name')} 다운로드 ({human_size(len(st.session_state['zip_cache']))})",
                    data=st.session_state["zip_cache"],
                    file_name=st.session_state["zip_name"],
                    mime="application/zip", use_container_width=True
                )
                
        # 리스트 페이징 출력
        PER_PAGE = 20
        total_pages = (len(pdf_files) + PER_PAGE - 1) // PER_PAGE
        page = st.number_input("목록 페이지", min_value=1, max_value=max(total_pages, 1), value=1)
        start = (page - 1) * PER_PAGE
        for i, f in enumerate(pdf_files[start:start+PER_PAGE], start=start+1):
            st.markdown(f"- **{i}.** `{f.name}` ({human_size(f.stat().st_size if f.exists() else 0)})")

# ── 📄 메타데이터
with tab_meta:
    if Path(metadata_csv).exists():
        try:
            df_meta = pd.read_csv(metadata_csv, encoding="utf-8-sig")
            st.markdown(f"총 **{len(df_meta)}건** 추출 완료")
            st.dataframe(df_meta, use_container_width=True, height=400)
            st.download_button("📥 메타데이터 다운로드", data=df_meta.to_csv(index=False).encode("utf-8-sig"), file_name=Path(metadata_csv).name, mime="text/csv")
        except Exception as e:
            st.error(f"파일 읽기 오류: {e}")
    else:
        st.info("아직 메타데이터가 없습니다. PDF 업로드 후 전처리(STEP 02)를 실행하세요.")

# ── 📊 분류 결과
with tab_class:
    if Path(classified_csv).exists():
        try:
            df_class = pd.read_csv(classified_csv, encoding="utf-8-sig")
            m1, m2, m3 = st.columns(3)
            m1.metric("전체 보고서", f"{len(df_class)}건")
            m2.metric("대분류 수", f"{df_class['대분류'].nunique() if '대분류' in df_class.columns else 0}개")
            m3.metric("오류 건수", f"{int(df_class['분류오류'].astype(str).str.len().gt(0).sum()) if '분류오류' in df_class.columns else 0}건")
            st.dataframe(df_class, use_container_width=True, height=400)
            st.download_button("📥 분류 결과 다운로드", data=df_class.to_csv(index=False).encode("utf-8-sig"), file_name=Path(classified_csv).name, mime="text/csv")
        except Exception as e:
            st.error(f"파일 읽기 오류: {e}")
    else:
        st.info("아직 분류 결과가 없습니다. AI 분류(STEP 03)를 실행하세요.")

# ── 💬 AI 채팅
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
            keep = [c for c in ["file", "title_kr", "date", "대분류", "중분류", "summary_kr"] if c in df_chat.columns]
            df_ctx = df_chat[keep].copy()
            if "summary_kr" in df_ctx.columns:
                df_ctx["summary_kr"] = df_ctx["summary_kr"].astype(str).str[:100]
            
            system_prompt = (
                "당신은 연구보고서 데이터를 분석하는 전문 어시스턴트입니다.\n"
                f"아래는 수집된 연구보고서 목록(CSV)입니다.\n[데이터]\n{df_ctx.to_csv(index=False)}"
            )

            st.markdown(f"#### {org_name} 연구보고서 기반 질의응답 (GPT-4o)")
            
            for msg in st.session_state.chat_history:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

            if user_input := st.chat_input("보고서에 대해 물어보세요..."):
                st.session_state.chat_history.append({"role": "user", "content": user_input})
                with st.chat_message("user"): st.markdown(user_input)
                with st.chat_message("assistant"):
                    with st.spinner("답변 생성 중..."):
                        messages = [{"role": "system", "content": system_prompt}] + st.session_state.chat_history[-10:]
                        resp = client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.3)
                        answer = resp.choices[0].message.content
                    st.markdown(answer)
                    st.session_state.chat_history.append({"role": "assistant", "content": answer})
                    
            if st.session_state.chat_history:
                if st.button("🗑️ 대화 초기화"):
                    st.session_state.chat_history = []
                    st.rerun()
        except Exception as e:
            st.error(f"채팅 오류: {e}")

# ── 📋 로그
with tab_log:
    lt1, lt2, lt3 = st.tabs(["크롤링 로그", "전처리 로그", "분류 로그"])
    with lt1: st.markdown(f'<div class="log-box">{st.session_state.log_crawl or "기록 없음"}</div>', unsafe_allow_html=True)
    with lt2: st.markdown(f'<div class="log-box">{st.session_state.log_preprocess or "기록 없음"}</div>', unsafe_allow_html=True)
    with lt3: st.markdown(f'<div class="log-box">{st.session_state.log_classify or "기록 없음"}</div>', unsafe_allow_html=True)
