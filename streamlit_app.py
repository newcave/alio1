# ────────────────────────────────────────────────────────────
# 메인 엔진 스레드 및 실행 버튼 구역
# ────────────────────────────────────────────────────────────
st.markdown("### 🚀 파이프라인 제어 콘솔")
bc1, bc2, bc3, bc4 = st.columns(4)
run_crawl = bc1.button("▶ 크롤링 실행", use_container_width=True)
run_preproc = bc2.button("▶ 데이터 전처리 (PDF 파싱)", use_container_width=True)
run_classify = bc3.button("▶ AI 기술분류", use_container_width=True)
run_all = bc4.button("⚡ 엔드투엔드 전체 실행", use_container_width=True, type="primary")

log_area, progress_area, status_area = st.empty(), st.empty(), st.empty()

# 1. 크롤링 실행 (기존 유지)
if run_crawl or run_all:
    ensure_dir(download_dir)
    run_with_progress(
        lambda ol, op: crawler.run(organ_name=org_name, out_dir=download_dir, headless=is_streamlit_cloud(), on_log=ol, on_progress=op),
        label="크롤링", spinner_text="크롤링 중", log_area=log_area, progress_area=progress_area, status_area=status_area, log_key="log_crawl"
    )
    st.rerun()

# 2. 전처리 실행 (PDF 파싱 복구)
if run_preproc or run_all:
    if count_files(download_dir) == 0:
        st.warning("⚠️ 파싱할 PDF가 없습니다. '📥 PDF 업로드' 탭에서 파일을 먼저 올려주세요.")
    else:
        run_with_progress(
            lambda ol, op: preprocessor.run(pdf_dir=download_dir, csv_out=metadata_csv, on_log=ol, on_progress=op),
            label="전처리", spinner_text="PDF 메타데이터 추출 중", 
            log_area=log_area, progress_area=progress_area, status_area=status_area, log_key="log_preprocess"
        )
        st.rerun()

# 3. AI 분류 실행
if run_classify or run_all:
    # 만약 업로드된 메타데이터가 임시로 있다면 디스크에 저장 후 분류 실행
    if not Path(metadata_csv).exists() and st.session_state.get("uploaded_metadata_df") is not None:
        ensure_dir(Path(metadata_csv).parent)
        st.session_state.uploaded_metadata_df.to_csv(metadata_csv, index=False, encoding="utf-8-sig")

    if not Path(metadata_csv).exists():
        st.warning("⚠️ 메타데이터 소스가 없습니다. 전처리를 먼저 수행하세요.")
    elif not get_openai_key():
        st.error("OpenAI API 키를 확인할 수 없습니다.")
    else:
        run_with_progress(
            lambda ol, op: classifier.run(input_csv=metadata_csv, output_csv=classified_csv, api_key=get_openai_key(), on_log=ol, on_progress=op),
            label="AI 기술분류", spinner_text="GPT-4o 분류 매핑 중", 
            log_area=log_area, progress_area=progress_area, status_area=status_area, log_key="log_classify"
        )
        st.rerun()


# ────────────────────────────────────────────────────────────
# 결과 탭 시스템 (PDF 업로드 및 파싱 결과 연동 복구)
# ────────────────────────────────────────────────────────────
st.markdown("---")
tab_help, tab_pdf, tab_meta, tab_class, tab_chat, tab_log = st.tabs([
    "❓ 사용방법", "📥 PDF 다운로드/업로드", "📄 메타데이터 (Step 2)", "📊 분류 결과 (Step 3)", "💬 AI 현황 분석 챗", "📋 실시간 로그"
])

# -- 📥 PDF 다운로드/업로드 탭 (파싱할 파일 주입구 복구)
with tab_pdf:
    st.markdown("### 📥 PDF 파일 업로드 및 관리")
    st.caption("크롤링 오류 시 아래에 PDF를 직접 올리고 상단의 **'▶ 데이터 전처리'** 버튼을 누르면 즉시 파싱됩니다.")
    
    uploaded_pdfs = st.file_uploader("PDF 파일 선택 (다중 가능)", type=["pdf"], accept_multiple_files=True, key="pdf_uploader")
    if uploaded_pdfs:
        ensure_dir(download_dir)
        saved_count = 0
        for f in uploaded_pdfs:
            out = Path(download_dir) / f.name
            out.write_bytes(f.read())
            saved_count += 1
        st.success(f"✅ {saved_count}개 PDF 업로드 완료! 상단의 **'▶ 데이터 전처리 (PDF 파싱)'** 버튼을 눌러주세요.")
        st.rerun()

    # 현재 로드된 PDF 목록 렌더링
    pdf_files = list_files(download_dir, exts=(".pdf", ".hwp", ".hwpx", ".docx"))
    st.markdown(f"#### 수집/업로드된 보고서: **{len(pdf_files)}개**")
    
    if pdf_files:
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("📦 전체 ZIP 묶어받기", use_container_width=True):
                with st.spinner("ZIP 생성 중..."):
                    st.session_state["zip_cache"] = make_zip_bytes(pdf_files, base_folder=download_dir)
                    st.session_state["zip_name"] = f"pdfs_{TODAY}_{len(pdf_files)}files.zip"
        with c2:
            if "zip_cache" in st.session_state:
                st.download_button(
                    f"💾 {st.session_state['zip_name']} 다운로드",
                    data=st.session_state["zip_cache"],
                    file_name=st.session_state["zip_name"],
                    mime="application/zip", use_container_width=True
                )
        
        # 목록 간략히 표시 (상위 20개)
        for i, f in enumerate(pdf_files[:20], start=1):
            st.markdown(f"- `{f.name}` ({human_size(f.stat().st_size)})")
        if len(pdf_files) > 20:
            st.caption(f"...외 {len(pdf_files) - 20}개 파일")

# -- 📄 메타데이터 탭 (파싱 결과 정상 출력 복구)
with tab_meta:
    st.markdown("### 📄 추출 메타데이터 검조")
    
    df_meta = None
    # 1순위: 직접 파싱한 결과물 로드
    if Path(metadata_csv).exists():
        df_meta = pd.read_csv(metadata_csv, encoding="utf-8-sig")
    # 2순위: 수동으로 주입한 백업 CSV가 있다면 로드
    elif st.session_state.get("uploaded_metadata_df") is not None:
        df_meta = st.session_state.uploaded_metadata_df

    if df_meta is not None:
        st.markdown(f"현재 추출/로드된 데이터: **{len(df_meta)}건**")
        st.dataframe(df_meta, use_container_width=True, height=400)
        st.download_button(
            "📥 메타데이터 CSV 다운로드", 
            data=df_meta.to_csv(index=False).encode("utf-8-sig"), 
            file_name=Path(metadata_csv).name, mime="text/csv"
        )
    else:
        st.info("💡 파싱된 데이터가 없습니다. 📥 탭에서 PDF를 업로드한 뒤 파싱을 진행하세요.")

    # (비상용) 이미 파싱해둔 로컬 CSV 직접 덮어쓰기 기능
    with st.expander("🛠️ (비상용) 과거에 파싱한 CSV 덮어쓰기"):
        meta_uploader = st.file_uploader("로컬 전처리 CSV (선택 사항)", type=["csv"], key="meta_csv_uploader")
        if meta_uploader is not None:
            st.session_state.uploaded_metadata_df = pd.read_csv(meta_uploader, encoding="utf-8-sig")
            st.success("외부 CSV가 성공적으로 인식되었습니다. 위의 화면이 업데이트됩니다.")
            st.rerun()

# -- 📊 분류 결과 탭
with tab_class:
    st.markdown("### 📊 AI 기술분류 결과")
    
    df_class = None
    if Path(classified_csv).exists():
        df_class = pd.read_csv(classified_csv, encoding="utf-8-sig")
    elif st.session_state.get("uploaded_classified_df") is not None:
        df_class = st.session_state.uploaded_classified_df

    if df_class is not None:
        st.markdown(f"총 **{len(df_class)}건**의 최종 매핑 데이터 보관 중")
        st.dataframe(df_class, use_container_width=True, height=400)
        st.download_button("📥 분류 결과 CSV 다운로드", data=df_class.to_csv(index=False).encode("utf-8-sig"), file_name=Path(classified_csv).name, mime="text/csv")
    else:
        st.info("💡 AI 분류 결과가 없습니다. 파이프라인 STEP 03을 실행하세요.")
