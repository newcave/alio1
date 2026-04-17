# 💧 Alio 보고서 파이프라인

공공기관 경영정보 공개시스템([ALIO](https://alio.go.kr))에서 연구보고서 PDF를 자동 수집하고,
메타데이터를 추출한 뒤 K-water 기술분류체계에 따라 자동 분류하는 Streamlit 대시보드입니다.

```
  크롤링 → PDF 전처리(메타데이터 추출) → AI 기술분류 → 결과 조회/다운로드
```

---

## 📂 프로젝트 구조

```
streamlit-alio-pipeline/
├── streamlit_app.py              # 메인 대시보드 (Streamlit 진입점)
├── pipeline/
│   ├── __init__.py
│   ├── crawler.py                # STEP 1. ALIO 크롤링 (Selenium)
│   ├── preprocessor.py           # STEP 2. PDF 메타데이터 추출
│   ├── classifier.py             # STEP 3. GPT-4o 분류
│   └── utils.py                  # 공통 유틸 (ZIP, 파일 유틸)
├── data/
│   └── org_list.json             # ALIO 등록 공공기관 목록 (~300개)
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example
├── requirements.txt              # Python 의존성
├── packages.txt                  # Cloud 배포용 apt 패키지
├── .gitignore
└── README.md
```

---

## 🚀 빠른 시작 (로컬)

### 1) 설치

```bash
git clone <your-repo-url>
cd streamlit-alio-pipeline
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2) OpenAI API 키 설정

`.streamlit/secrets.toml` 파일을 만들고 키를 넣으세요:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# 편집기로 열어서 sk-... 를 실제 키로 교체
```

또는 간단하게 `.env` 파일 사용:

```bash
echo "OPENAI_API_KEY=sk-..." > .env
```

### 3) Chrome 필요

크롤링에는 **Chrome 또는 Chromium**이 PC에 설치되어 있어야 합니다.
Selenium 4.10+ 부터는 드라이버 자동 관리라서 chromedriver는 따로 안 받아도 됩니다.

### 4) 실행

```bash
streamlit run streamlit_app.py
```

기본 브라우저가 자동으로 열리며 `http://localhost:8501` 로 접속됩니다.

---

## ☁️ Streamlit Cloud 배포

### 1) GitHub 저장소에 푸시

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<YOUR_ID>/<REPO_NAME>.git
git push -u origin main
```

> ⚠️ `.streamlit/secrets.toml` 과 `.env` 는 `.gitignore` 에 있어 자동으로 제외됩니다. 안심하세요.

### 2) Streamlit Cloud 앱 생성

1. [share.streamlit.io](https://share.streamlit.io) 접속 → **New app**
2. Repository: `<YOUR_ID>/<REPO_NAME>` 선택
3. Branch: `main`, Main file path: `streamlit_app.py`
4. **Advanced settings → Secrets** 에 아래 붙여넣기:
   ```toml
   OPENAI_API_KEY = "sk-실제키"
   ```
5. **Deploy** 클릭

배포가 완료되면 `https://<앱이름>.streamlit.app` 형태의 URL이 발급됩니다.

### 3) 중요한 주의사항

- ☁️ **파일시스템 휘발성**: Streamlit Cloud 컨테이너는 재시작 시 파일을 유지하지 않습니다.
  세션 중에는 `/tmp` 내에 작업되며, 결과물은 **반드시 CSV/ZIP으로 다운로드** 해두세요.
- 🧠 **메모리 1GB 제한**: 너무 많은 기관을 한 번에 크롤링하면 OOM 가능성이 있습니다.
  기관당 100-200건 이하로 테스트하시길 권장합니다.
- 🐢 **크롤링 속도**: Cloud에서는 헤드리스 Chromium이 자동 사용되지만, ALIO 사이트 구조상
  페이지당 20-30초가 걸립니다. 대용량 작업은 로컬 실행을 권장합니다.
- 🔒 **Chrome/Selenium 실패 시 대안**: "📥 PDF 다운로드" 탭의 **직접 업로드** 기능으로 우회 가능.
  로컬에서 크롤링 → Cloud에서 분류만 하는 하이브리드 방식도 유효합니다.

---

## 🧩 각 단계 상세

### STEP 1. 크롤링 (`pipeline/crawler.py`)

- ALIO의 기관 검색 API(`itemOrganListSusi.json`)로 `apbaId` 조회
- Selenium이 페이지를 넘기며 각 보고서의 상세 팝업에서 다운로드 링크를 수집
- 지원 포맷: `.pdf`, `.hwp`, `.hwpx`, `.doc/.docx`, `.xlsx`, `.pptx`
- 외부 링크(기관 자체 사이트)의 PDF도 스캔 (옵션)

```python
from pipeline import crawler
result = crawler.run(
    organ_name="한국수자원공사",
    out_dir="./downloads",
    headless=True,
    max_pages=5,          # 테스트 시 페이지 제한
)
print(f"{len(result.downloaded)}개 다운로드")
```

### STEP 2. PDF 전처리 (`pipeline/preprocessor.py`)

`pdfplumber`로 각 PDF의 앞 30페이지에서 다음을 추출:

| 필드 | 추출 방식 |
|---|---|
| `title_kr` / `title_en` | 표지 페이지(가장 짧고 깔끔한 쪽)에서 정규식 기반 분리 |
| `date` | `YYYY.MM` 패턴 매칭 |
| `authors` | "제출문" 페이지의 "연구책임자/연구수행자" 섹션 파싱 |
| `summary_kr` / `summary_en` | "요약문" ~ "SUMMARY" 구간 텍스트 |

```python
from pipeline import preprocessor
preprocessor.run("./downloads", "./metadata.csv")
```

### STEP 3. AI 분류 (`pipeline/classifier.py`)

OpenAI GPT-4o 로 K-water 기술분류체계(대분류 5 × 중분류 총 20개) 매핑:

- 수자원 (댐/보/하천, 물환경, 지하수, 공통)
- 수도 (상수도, 맞춤형용수, 하폐수 처리·재이용, 공통)
- 에너지 (수력·수열·조력·태양광, 공통)
- 수변 (수변도시조성, 생태/경관, 공간/건축, 공통)
- 디지털 (빅데이터관리, 융합분석서비스, 공통)

```python
from pipeline import classifier
classifier.run(
    input_csv="./metadata.csv",
    output_csv="./classified.csv",
    model="gpt-4o",
)
```

---

## 💡 기능 요약

- ✅ 공공기관 300개 드롭다운 선택
- ✅ 크롤링 / 전처리 / 분류 개별 또는 일괄 실행
- ✅ 실시간 진행률 + 컬러 로그 박스
- ✅ **PDF 개별 다운로드 + 전체 ZIP 다운로드** (NEW)
- ✅ **PDF 직접 업로드** (크롤링 실패 시 대체, Cloud 친화적)
- ✅ Plotly 차트로 분류 통계 시각화
- ✅ 자연어 AI 채팅으로 결과 질의
- ✅ CSV 내보내기 (메타데이터 / 분류 결과)

---

## ⚙️ 의존성

| 패키지 | 용도 |
|---|---|
| streamlit | 대시보드 |
| pandas | 데이터 처리 |
| plotly | 시각화 |
| pdfplumber | PDF 파싱 |
| selenium | 크롤링 |
| openai | LLM 분류·채팅 |
| python-dotenv | 로컬 환경변수 |
| requests | HTTP API 호출 |

---

## 🙋 문제 해결

| 증상 | 해결 |
|---|---|
| Cloud 배포 후 크롤링 실패 | `packages.txt`가 커밋되어 있는지 확인. 로그에 `chromium` 설치 여부 체크 |
| API 키 오류 | 로컬: `.streamlit/secrets.toml` 또는 `.env`. Cloud: 웹 UI → Secrets |
| `기관을 찾지 못했습니다` | ALIO 검색 기능에서 실제 등록 명칭을 확인. `data/org_list.json` 업데이트 |
| 이름이 4자인 저자 누락 | `preprocessor.py` 의 마지막 3글자 잘림 로직을 수정 (원 작성자 주석 참조) |
| Cloud에서 OOM | `max_pages` 파라미터로 페이지 제한, 또는 로컬 실행으로 전환 |

---

## 📜 크레딧

- **Original pipeline**: wjelly@kwater.or.kr (로컬 버전 최초 설계)
- **Cloud 배포 리팩터링**: GitHub + Streamlit Community Cloud 대응
