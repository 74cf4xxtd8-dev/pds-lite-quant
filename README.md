# PDS-Lite — 서버비 0원 멀티에셋 리밸런싱 대시보드

GitHub Actions(연산) → `docs/data/portfolio_latest.json` → GitHub Pages(`docs/index.html`).

## 설치 (10분)
1. 이 폴더로 **public** 저장소를 만든다 (무료 Pages는 public 저장소에서만 가능. JSON에는 비중만 있고 계좌 금액은 없음).
2. Settings → Pages → Source: *Deploy from a branch*, Branch `main`, Folder `/docs`.
3. Settings → Actions → General → Workflow permissions: **Read and write**.
4. Actions 탭 → `update-portfolio` → *Run workflow* (첫 실행은 2017-03부터 백필, 몇 분 걸림).
5. (선택) Telegram 알림: Secrets `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
6. 휴대폰에서 `https://<아이디>.github.io/<저장소>/` 열고 홈 화면에 추가.

첫 실행 로그의 `validation: N/M months exact` 줄이 **원작자 공개 Core와의 일치율**이다. 이것이 엔진 검증의 핵심 지표다.

## 실행 모드
| 명령 | 내용 |
|---|---|
| `python run_pipeline.py` | 자체 연산(Yahoo 가격 + FRED UNRATE + F2R 공개 JSON). Adaptive는 원작자 공개값 우선, 없으면 대체 규칙 |
| `python run_pipeline.py --mode reference` | 원작자 공개 워크북 그대로 미러링 (가격 불필요, 비상용) |
| `ADAPTIVE_MODE=proxy python run_pipeline.py` | Adaptive를 자체 규칙으로만 결정 |

## 알고리즘 요약 (역추적 결과)
```
ADAA = 0.25·HAA + 0.15·BAA공격형 + 0.175·ADM + 0.175·FAA(canonical) + 0.25·LAA
Core = 0.90·ADAA + 0.10·F2R(Top-4 × 25%)
Adaptive = RB·Core + (1−RB)·BIL        (NORMAL: RB=1, DEFENSIVE: 0.55 ≤ RB < 1)
```
- 모든 ADAA 비중은 1/2400 격자 위에 있다 (1/24, 7/240, 1/20 …).
- 0.05625 = 0.9 × 0.25(LAA) × 0.25(IEF/GLD 고정분)
- 0.08125 = 0.05625(LAA의 QQQ 스위치) + 0.1 × 0.25(F2R이 QQQ 선택)
- 0.154375 = 0.9 × (0.04375 LAA + 0.041667 HAA + 0.029167 ADM + 0.029167 FAA) + 0.025(F2R)

## 운영 원칙
- **Official 잠금**: `official_history.json`에 한 번 기록된 월은 다시 계산하지 않는다 (배당 조정가 소급 변경으로 순위가 뒤집히는 것 방지).
- **Fail-closed**: 다운로드·검증 실패 시 JSON을 덮어쓰지 않고 워크플로를 실패시킨다 → GitHub가 메일로 알려 줌.
- **Preview는 실행 금지**: 월중 데이터로 계산한 추정치.
- 60일간 저장소 활동이 없으면 GitHub가 예약 워크플로를 끌 수 있다. 메일이 오면 Actions 탭에서 다시 켠다.

## 원작자와 다른 부분 (반드시 인지)
| 항목 | 원작자 | PDS-Lite |
|---|---|---|
| Adaptive 상태/RB | 비공개 PPO 앙상블 + 변동성 확인 | 공개 워크북 값 사용, 없으면 자체 규칙: 원작자 12개월 추세 규칙(SPY 12개월 수익 ≤ BIL) AND 변동성 예산<1 → RB=max(0.55, 10%/σ) |
| F2R | Ridge·ElasticNet·XGBoost 순위 앙상블 + Chronos-2 20% (피처·기간 비공개) | 원작자 공개 JSON의 Top-4 사용, 없으면 모멘텀 대체(동점은 원작자 고정 순서) |
| 거래비용 | F2R 연구: 편도 10bp | 화면에 전월 목표 대비 회전율과 예상 비용(편도 10bp) 표시. 월중 비중 변화는 빠져 있어 실제보다 낮게 나옴 |
| IRP | 해당 없음 | 국내 ETF 대체 매핑 + 위험자산 68% 목표 |

연구용 재구성이며 투자 권유가 아니다.
