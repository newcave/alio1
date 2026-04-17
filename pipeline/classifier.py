"""
STEP 3. K-water 기술분류체계 자동 분류 (OpenAI API)

- K-water 대분류 5개 × 중분류 각각으로 매핑
- OPENAI_API_KEY 는 환경변수 또는 `api_key` 파라미터로 주입
"""
from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

CLASSIFICATION_SYSTEM = """당신은 K-water 연구보고서를 기술분류체계에 따라 분류하는 전문가입니다.
아래 분류체계를 기반으로 보고서의 대분류와 중분류를 판단하세요.

[K-water 기술분류체계]
대분류: 수자원 (Water Resources)
  - 댐/보/하천 (Dam & Weir & River)
  - 물환경 (Water Environment)
  - 지하수 (Ground Water)
  - 공통 및 기타 (Common & Other)

대분류: 수도 (Water Supply)
  - 상수도 (WaterWorks)
  - 맞춤형용수 (Customized Water)
  - 하폐수 처리 및 재이용 (Sewage & Waste Water Treatment)
  - 공통 및 기타 (Common & Other)

대분류: 에너지 (Clean Energy)
  - 수력발전 (Hydropower)
  - 수열에너지 (Hydrothermal Energy)
  - 조력발전 (Tidal Power Generation)
  - 태양광발전 (Solar Energy Generation)
  - 공통 및 기타 (Common & Other)

대분류: 수변 (Waterfront)
  - 수변도시조성 (Waterfront Development)
  - 생태/경관 (Ecological & Landscape)
  - 공간/건축 (Architecture)
  - 공통 및 기타 (Common & Other)

대분류: 디지털 (Digital)
  - 빅데이터관리 (Bigdata)
  - 융합분석서비스 (Convergence Technology)
  - 공통 및 기타 (Common & Other)

반드시 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.
{"대분류": "수자원", "중분류": "댐/보/하천", "근거": "한 줄 이내 근거"}"""


def classify_report(
    client,
    row: dict,
    *,
    model: str = "gpt-4o",
) -> dict:
    """단일 보고서 분류. client는 openai.OpenAI 인스턴스."""
    title_kr = row.get("title_kr", "")
    title_en = row.get("title_en", "")
    summary = str(row.get("summary_kr", ""))[:1500]
    user_msg = f"제목(KR): {title_kr}\n제목(EN): {title_en}\n요약문: {summary}"

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFICATION_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        result = json.loads(resp.choices[0].message.content)
        return {
            "대분류": result.get("대분류", ""),
            "중분류": result.get("중분류", ""),
            "분류근거": result.get("근거", ""),
            "분류오류": "",
        }
    except Exception as e:
        return {"대분류": "", "중분류": "", "분류근거": "", "분류오류": str(e)}


def run(
    input_csv: str | Path,
    output_csv: str | Path,
    *,
    api_key: Optional[str] = None,
    model: str = "gpt-4o",
    sleep_sec: float = 0.3,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> pd.DataFrame:
    """분류 실행. input_csv 는 preprocessor.run 결과물."""
    from openai import OpenAI  # lazy import

    log = on_log or (lambda s: print(s, flush=True))
    progress = on_progress or (lambda c, t, m: None)

    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OpenAI API 키가 없습니다. `st.secrets` 또는 환경변수 OPENAI_API_KEY 설정."
        )
    client = OpenAI(api_key=key)

    log(f"{input_csv} 읽는 중...")
    df = pd.read_csv(input_csv, encoding="utf-8-sig")
    total = len(df)
    log(f"TOTAL:{total}")
    progress(0, total, "시작")

    results: list[dict] = []
    for i, row in df.iterrows():
        cur = i + 1
        fname = str(row.get("file", f"row_{i}"))[:40]
        log(f"PROGRESS:{cur}/{total} {fname}")
        progress(cur, total, fname)

        res = classify_report(client, row.to_dict(), model=model)
        results.append(res)
        log(f"STATUS:{res['대분류']} / {res['중분류']}")
        time.sleep(sleep_sec)

    df_out = pd.concat([df, pd.DataFrame(results)], axis=1)
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(output_csv, index=False, encoding="utf-8-sig")
    log(f"\n완료! {output_csv} ({len(df_out)}건)")
    if "대분류" in df_out.columns:
        log("\n[분류 결과 요약]")
        log(df_out["대분류"].value_counts().to_string())
    return df_out


if __name__ == "__main__":
    from datetime import datetime as _dt
    from dotenv import load_dotenv
    load_dotenv()
    _today = os.environ.get("RUN_DATE", _dt.now().strftime("%Y%m%d"))
    input_csv = os.environ.get("METADATA_CSV", f"2_extracted_metadata_{_today}.csv")
    output_csv = os.environ.get("CLASSIFIED_CSV", f"3_classified_reports_{_today}.csv")
    run(input_csv, output_csv)
