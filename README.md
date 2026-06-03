# 알뜰폰 정책 트래커

네이버 카페 `gray9de5o`의 후불유심 정책글을 수집하고, 첨부 이미지에서 7GB/11GB 정책금액 후보를 누적합니다.

## 기준

- 대상 통신사: 유모비, 헬로, KTM, 스카이, SK7
- 제외: 이야기 모바일/선불/후불
- 금액 표기: `신규 / 번호이동`
- 대표 금액: 내국인 `010신규`, `MNP`
- 자동 추출값은 `needs_review`이며, 원본 이미지로 확인한 값은 `manual_fixed`로 저장합니다.

## 실행

```bash
python3 scripts/policy_tracker.py daily-update --since 2025-01-01 --extract-limit 50
```

## 카카오톡 정책 이미지 가져오기

카카오톡에서 저장한 정책 이미지는 로컬 정책글처럼 누적할 수 있습니다. 원본 이미지는 `inbox/`와 `data/images/`에만 두고 GitHub Pages에는 올리지 않습니다.

```bash
mkdir -p inbox/kakao
python3 scripts/policy_tracker.py import-local-images inbox/kakao --source kakao --subject "카카오톡 정책 이미지" --write-date "2026-06-04 11:00:00"
python3 scripts/policy_tracker.py extract --limit 50 --missing-only
python3 scripts/policy_tracker.py dashboard
python3 scripts/policy_tracker.py export-csv
scripts/publish_pages.sh
```

카카오톡 앱 자체를 자동 조작해 이미지를 저장하려면 macOS `개인정보 보호 및 보안 > 손쉬운 사용`에서 Codex/터미널/osascript가 카카오톡을 제어할 수 있도록 허용해야 합니다. 카카오톡 내부 캐시 이미지는 암호화되어 있으므로, 앱에서 저장된 원본 이미지 또는 열린 이미지 뷰어를 통해 가져오는 방식이 필요합니다.

## 주요 산출물

- `dashboard/index.html`: 일별/월별 그래프 대시보드
- `docs/index.html`: GitHub Pages 공개용 대시보드
- `dashboard/priority_review.html`: 우선 검수 후보와 원본 이미지
- `dashboard/review.html`: 자동 추출 실패 이미지
- `data/exports/policy_summary_by_post.csv`: 게시글별 7GB/11GB 요약
- `data/exports/policy_changes.csv`: 전차수 대비 변동
- `data/exports/policy_values_daily.csv`: 일별 누적값
- `data/exports/policy_values_monthly.csv`: 월별 평균/최대/최소
- `data/exports/status_report.md`: 현재 적재/검수 상태

## 수동 확정값 반영

이미지로 확인한 값은 아래 CSV에 추가합니다.

```text
data/manual/confirmed_values.csv
```

반영 명령:

```bash
python3 scripts/policy_tracker.py import-manual data/manual/confirmed_values.csv
python3 scripts/policy_tracker.py export-csv
python3 scripts/policy_tracker.py dashboard
python3 scripts/policy_tracker.py review
python3 scripts/policy_tracker.py priority-review
```

## 자동화

Codex heartbeat 자동화 `automation`이 매일 11:00 KST에 `daily-update`를 실행하도록 설정되어 있습니다. 정책 데이터나 대시보드 산출물이 바뀌면 GitHub Pages 공개 사이트도 자동으로 갱신합니다.

## GitHub Pages 배포

GitHub Pages 공개용 파일은 `docs/` 폴더에 모아둡니다. GitHub 저장소의 Settings > Pages에서 Source를 `Deploy from a branch`, Branch를 `main`, Folder를 `/docs`로 선택하면 됩니다.

공개용 대시보드를 최신 상태로 다시 만들 때는 아래 순서로 실행합니다.

```bash
scripts/publish_pages.sh
```
