# 알뜰폰 정책 트래커 상태 보고

생성 기준: 2026-07-31 KST

## 수집 범위

- 네이버 카페: `gray9de5o`
- 게시판: 후불유심정책
- 기간: 2025-01-01 ~ 2026-06-04
- 정책글: 201개
- 대상 통신사: 유모비, 헬로, KTM, 스카이, SK7
- 제외: 이야기 모바일

## 적재 현황

- 원본 정책 이미지: 1,000장
- 자동 후보 정책값: 1,718개
- 수동 확정값: 220개
- 자동 후보 추출 범위: 2025-01-01 ~ 2026-06-04
- 원본 대조가 더 필요한 이미지: 7장

## 통신사별 검수 현황

- hello: 이미지 204장, 후보값 있음 204장, 원본 검수 필요 0장
- ktm: 이미지 199장, 후보값 있음 199장, 원본 검수 필요 0장
- sk7: 이미지 197장, 후보값 있음 190장, 원본 검수 필요 7장
- skylife: 이미지 198장, 후보값 있음 198장, 원본 검수 필요 0장
- umobile: 이미지 202장, 후보값 있음 202장, 원본 검수 필요 0장

## 산출물

- 대시보드: `dashboard/index.html`
- 게시글별 요약: `data/exports/policy_summary_by_post.csv`
- 이전 차수 대비 변동: `data/exports/policy_changes.csv`
- 우선 검수 후보: `data/exports/review_priority.csv`
- 일별 후보값: `data/exports/policy_values_daily.csv`
- 월별 요약: `data/exports/policy_values_monthly.csv`
- 커버리지: `data/exports/extraction_coverage.csv`
- 자동 후보값 원본 대조: `dashboard/candidate_review.html`
- 우선 검수 화면: `dashboard/priority_review.html`
- 자동 추출 실패 이미지 검토: `dashboard/review.html`
- 수동 확정 입력 양식: `data/exports/manual_values_template.csv`
- 미추출 이미지 수동 입력 양식: `data/exports/missing_image_manual_template.csv`

## 남은 검증 작업

- 자동 후보값은 `needs_review` 상태이며, 원본 이미지 대조 후 확정해야 함.
- `policy_summary_by_post.csv`에서 빈 칸은 해당 통신사 이미지에서 7GB/11GB 후보값을 자동 확정하지 못한 항목임.
- 후보값 보정은 `import-manual`, 미추출 이미지 확정값은 `import-summary-manual` 명령으로 반영 가능.
