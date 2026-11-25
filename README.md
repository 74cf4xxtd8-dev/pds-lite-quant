# PDS-Lite — 서버비 0원 멀티에셋 리밸런싱 대시보드

GitHub Actions(연산) → `docs/data/portfolio_latest.json` → GitHub Pages(`docs/index.html`).

## 설치 (10분)
1. 이 폴더로 **public** 저장소를 만든다 (무료 Pages는 public 저장소에서만 가능. JSON에는 비중만 있고 계좌 금액은 없음).
2. Settings → Pages → Source: *Deploy from a branch*, Branch `main`, Folder `/docs`.
3. Settings → Actions → General → Workflow permissions: **Read and write**.
4. Actions 탭 → `update-portfolio` → *Run workflow* (첫 실행은 2017-03부터 백필, 몇 분 걸림).
5. (선택) Telegram 알림: Secrets `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
6. 휴대폰에서 `https://<아이디>.github.io/<저장소>/` 열고 홈 화면에 추가.

실제 Yahoo 가격 기준 2017-03~2026-10 **116/116개월 원작자 Core와 정확히 일치** (2026-11-26 확인). 실행 로그의 `validation: N/M months exact` 줄이 **원작자 공개 Core와의 일치율**이다. 이것이 엔진 검증의 핵심 지표다.

## 실행 모드
| 명령 | 내용 |
|---|---|
| `python run_pipeline.py` | 자체 연산(Yahoo 가격 + FRED/BLS UNRATE + F2R 공개 JSON). `ADAPTIVE_MODE=reference_only`(기본): 원작자 Adaptive 공식값이 **잠금 시점에 이미 공개**돼 있고 검증을 통과하면 반영, 아니면 RB=1.0(Core 100%) |
| `ADAPTIVE_MODE=core_only python run_pipeline.py` | 운용 모드. 원작자 값은 참고로만 기록하고 항상 RB=1.0 |
| `ADAPTIVE_MODE=reference_then_proxy` / `proxy` | **진단 전용**(1.0 동작 재현·대체 규칙 확인). `docs/data/diagnostic/`에만 쓰고 운용 파일·잠금은 건드리지 않음 |
| `python run_pipeline.py --mode reference` | 원작자 공개 워크북 미러(진단용, `docs/data/diagnostic/`에만 기록) |
| `python -m pytest tests -q` | 오프라인 테스트(합성 데이터, main 대비 전후 비교, fail-safe, 워크플로 커밋 로직). `PDS_LIVE_TEST=1`이면 실데이터 116개월 전후 비교 |

## 알고리즘 요약 (역추적 결과)
```
ADAA = 0.25·HAA + 0.15·BAA공격형 + 0.175·ADM + 0.175·FAA(legacy v0.11) + 0.25·LAA
Core = 0.90·ADAA + 0.10·F2R(Top-4 × 25%)
Adaptive = RB·Core + (1−RB)·BIL        (NORMAL: RB=1, DEFENSIVE: 0.55 ≤ RB < 1)
```
- 모든 ADAA 비중은 1/2400 격자 위에 있다 (1/24, 7/240, 1/20 …).
- 0.05625 = 0.9 × 0.25(LAA) × 0.25(IEF/GLD 고정분)
- 0.08125 = 0.05625(LAA의 QQQ 스위치) + 0.1 × 0.25(F2R이 QQQ 선택)
- 0.154375 = 0.9 × (0.04375 LAA + 0.041667 HAA + 0.029167 ADM + 0.029167 FAA) + 0.025(F2R)

## 운영 원칙
- **Official 잠금**: `official_history.json`에 한 번 기록된 월은 다시 계산하지 않는다 (배당 조정가 소급 변경으로 순위가 뒤집히는 것 방지). 매 실행마다 기존 항목이 바이트 단위로 그대로인지 검사하고, 다르면 아무것도 쓰지 않고 중단한다.
- **원작자 값은 참고 원장에 분리**: `reference_adaptive.json`에 원작자 공개 Adaptive 행과 처음 확인한 시각·수정 이력을 추가 전용으로 기록한다. 잠금 이후 공개·수정된 값은 잠금을 바꾸지 않는다(화면 flags에 `AUTHOR_REFERENCE … not applied`로 표시).
- **월간 목표 확정 조건**: 그 달의 원작자 **F2R 공식 Top-4**가 공개되고, 원작자가 공개한 그 달 **Core 행이 자체 계산 Core와 일치(오차 ≤1e-6)**할 때만 잠근다. 2026-09·10월은 둘 다 월말 2일 뒤 같은 날 공개됐다. Adaptive는 잠그는 순간 이미 공개된 원작자 공식값만 반영하고, 이후 공개값은 참고 원장에만 남는다.
- **매월 1~10일 매일 확인**: 조건이 안 갖춰지면 `OFFICIAL_PENDING`(⏳ 잠금 대기). 직전 Official이 유효하고 리밸런싱하지 않는다. Preview로 대체하지 않는다.
- **10일 마감 → BLOCKED**: 10일 22:40 UTC 실행(마지막 확인)에도 미확보면 그 달은 잠그지 않는다. `action.code = HALTED`, 화면 상단 배너와 Telegram ⛔: **현재 보유 자산 유지, 자동 리밸런싱 중지**. 마감 뒤에 데이터가 들어와도 자동으로 잠그지 않는다.
- **수동 해제(예외)**: 직접 확인한 뒤 Actions → update-portfolio → Run workflow에서 `release_month=YYYY-MM`과 `release_reason`(승인 사유, 필수). 다음이 **모두** 맞을 때만 잠긴다(`origin: MANUAL_RELEASE`, 화면 분류 `RECONSTRUCTED`): 공식 F2R, 그 달 원작자 Core 일치, 원작자 Core 과거 이력 전부 일치, F2R·워크북 실시간 수신, UNRATE가 캐시가 아님, 가격 무결성 통과 및 최근 5일 이내 종가, 운영자 중지 아님. 하나라도 어긋나면 `MANUAL_RELEASE_REJECTED`. 이미 다음 달이 잠긴 뒤의 지난 달은 해제할 수 없다(건너뛴 달로 남음, `unlocked_gaps`).
- **승인 기록**: 수동 해제 요청은 성공·거부 모두 `manual_releases.json`에 실행자(GitHub 계정), 실행 URL, 사유, 점검 결과와 함께 추가 전용으로 남는다. 잠긴 달의 항목에도 `manual_release`가 붙는다.
- **감사 기록**: `official_events.json`에 PENDING·CORE_MISMATCH·BLOCKED·LOCKED·MANUAL_RELEASE(_REJECTED)·OPERATOR_HALT를 시각과 함께 추가 전용으로 남긴다. 매 실행 요약(최근 60회)은 `run_status.json`의 `recent`.
- **알림(Telegram)**: 상태가 바뀔 때만 보낸다 — 잠금 대기 시작(월 1회), 원작자 Core 불일치, 공식 확정 ✅, 10일 잠금 보류 ⛔, 수동 해제/거부, 운영자 중지, 실행·커밋 실패. 1~10일의 반복 확인은 알리지 않는다.
- **운영자 중지**: 저장소 변수 `PDS_HALT=1`(Settings → Secrets and variables → Actions → Variables). 다음 실행부터 어떤 달도 잠그지 않고 `HALTED`(보유 유지). 변수를 지우면 정상 동작.
- **Fail-closed**: 가격 결측·급변(±25%)·고착, API 실패, Core 불일치, 잠금 변경 시도 시 어떤 파일도 쓰지 않는다. 출력 5개(잠금·참고 원장·감사기록·해제 기록·화면 JSON)는 모든 검사가 끝난 뒤 한꺼번에 기록된다. 실패한 실행은 `run_status.json`만 커밋하고(Telegram 실패 알림), 진단 모드는 커밋하지 않는다.
- **Preview는 실행 금지**: 월중 데이터로 계산한 추정치.
- 60일간 저장소 활동이 없으면 GitHub가 예약 워크플로를 끌 수 있다. 메일이 오면 Actions 탭에서 다시 켠다.

## 원작자와 다른 부분 (반드시 인지)
| 항목 | 원작자 | PDS-Lite |
|---|---|---|
| Adaptive 상태/RB | 비공개 PPO 앙상블 + 변동성 확인 | 잠금 시점에 공개된 원작자 공식값만 반영, 그 외 RB=1.0(Core 100%). 자체 대체 규칙(SPY 12개월 추세 AND 변동성 예산<1)은 진단값으로만 기록 |
| F2R | Ridge·ElasticNet·XGBoost 순위 앙상블 + Chronos-2 20% (피처·기간 비공개) | 원작자 공개 JSON의 Top-4 사용, 없으면 모멘텀 대체(동점은 원작자 고정 순서) |
| 거래비용 | F2R 연구: 편도 10bp | 화면에 전월 목표 대비 회전율과 예상 비용(편도 10bp) 표시. 월중 비중 변화는 빠져 있어 실제보다 낮게 나옴 |
| IRP | 해당 없음 | 국내 ETF 대체 매핑 + 위험자산 68% 목표 |

연구용 재구성이며 투자 권유가 아니다.

## 롤백 절차 (자동 리밸런싱이 먼저 멈춘 상태에서만)
구버전(1.0)은 `PDS_HALT`를 모르고, BLOCKED로 비워 둔 달을 백필로 자동 채운다. 그대로 되돌리면 승인하지 않은 목표 비중이 생길 수 있으므로 아래 순서를 지킨다.
1. **운영자 중지**: 저장소 변수 `PDS_HALT=1` → update-portfolio를 한 번 수동 실행해 `HALTED`와 ⛔ 알림 확인. 보유 자산은 그대로 둔다.
2. **워크플로 끄기**: Actions → update-portfolio → ⋯ → Disable workflow (`gh workflow disable update-portfolio`). 되돌리기 커밋의 push 트리거와 예약 실행이 구버전을 돌리지 못하게 막는다(구버전은 `PDS_HALT`를 무시하므로 이 단계가 핵심).
3. **사전 점검(dry run)**: Actions → rollback-check → `target_ref`=되돌릴 커밋. 오늘 데이터로 그 코드를 현재 `docs/data` 사본에 돌려 새로 잠길 달·빈 달 채우기·기존 달 변경을 보고한다(저장소에 쓰지 않음). 로컬: `python tools/rollback_check.py --ref <commit>`. 종료 코드 2 = 수동 검토 필요.
4. 점검 결과를 확인하고 받아들일 수 있을 때만 `git revert` → push(워크플로가 꺼져 있어 자동 실행 없음).
5. 워크플로를 켜기 전에 update-portfolio를 `commit=false`로 수동 실행해 결과 확인 → 이상 없으면 켜고 `PDS_HALT` 해제.
6. 점검에서 빈 달 채우기가 나오면 워크플로를 끈 채 보유를 유지하고, 그 달을 어떻게 처리할지 따로 결정한다.

