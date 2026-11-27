# PDS-Lite — 서버비 0원 멀티에셋 리밸런싱 대시보드

GitHub Actions(연산) → `docs/data/portfolio_latest.json` → GitHub Pages(`docs/index.html`).

> 운영 문서: [handbook/](handbook/README.md) — ① 시스템 분석·설계서, ② 사용자·운영자 매뉴얼, ③ 변경 이력 및 운영 정책서.
> 이 README와 실제 구현이 다른 곳은 handbook ③ 7장(차이 목록)에 정리돼 있습니다.

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
- **수동 해제(예외)**: 직접 확인한 뒤 Actions → update-portfolio → Run workflow(브랜치 `main`)에서 `release_month=YYYY-MM`과 `release_reason`(승인 사유, 필수). 실행은 보호 환경 `pds-manual-release`의 **검토자 승인**을 기다리고, 승인돼도 요청자가 admin이고 main에서 돌았으며 이 실행에 승인 기록이 있을 때만 다음 점검으로 넘어간다(아래 "GitHub 권한·설정"). 다음이 **모두** 맞을 때만 잠긴다(`origin: MANUAL_RELEASE`, 화면 분류 `RECONSTRUCTED`): 공식 F2R, 그 달 원작자 Core 일치, 원작자 Core 과거 이력 전부 일치, F2R·워크북 실시간 수신, UNRATE가 캐시가 아님, 가격 무결성 통과 및 최근 5일 이내 종가, 운영자 중지 아님. 하나라도 어긋나면 `MANUAL_RELEASE_REJECTED`. 이미 다음 달이 잠긴 뒤의 지난 달은 해제할 수 없다(건너뛴 달로 남음, `unlocked_gaps`).
- **승인 기록**: 수동 해제 요청은 성공·거부 모두 `manual_releases.json`에 요청자(GitHub 계정)·권한, 승인자·승인 코멘트, 환경 보호 설정, 실행 URL, 사유, 점검 결과와 함께 추가 전용으로 남는다. 잠긴 달의 항목에도 `manual_release`(요청자·승인자)가 붙는다.
- **감사 기록**: `official_events.json`에 PENDING·CORE_MISMATCH·BLOCKED·LOCKED·MANUAL_RELEASE(_REJECTED)·OPERATOR_HALT를 시각과 함께 추가 전용으로 남긴다. 매 실행 요약(최근 60회)은 `run_status.json`의 `recent`.
- **알림(Telegram)**: 상태가 바뀔 때만 보낸다 — 잠금 대기 시작(월 1회), 원작자 Core 불일치, 공식 확정 ✅, 10일 잠금 보류 ⛔, 수동 해제/거부, 운영자 중지, 실행·커밋 실패. 1~10일의 반복 확인은 알리지 않는다.
- **운영자 중지**: 저장소 변수 `PDS_HALT=1`. 중지된 실행은 **읽기 전용**이다: 신규 잠금·목표 비중·참고 원장·감사기록·UNRATE 캐시 등 `docs/data`의 어떤 파일도 바꾸지 않고 `run_status.json`(중지 보고)만 커밋한다. 화면 상단 ⛔ 배너, Telegram ⛔ 1회. 수동 해제도 거부된다. 값이 비었거나 `0/false/no/off`가 아니면 중지로 본다(오타는 중지 쪽). 절차는 아래 "운영자 중지 절차".
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

## 운영자 중지 절차 (실행 중인 작업 처리 포함)
저장소 변수는 실행이 시작될 때 한 번 읽힌다. 그래서 `PDS_HALT=1`을 넣기 전에 이미 대기·실행 중이던 update-portfolio 실행은 중지를 모른 채 잠금·커밋할 수 있다. 실행 중에 변수를 다시 읽는 방법도 없다(GITHUB_TOKEN은 변수 API 403, 직접 확인). 대신 update-portfolio 실행은 동시 실행 방지 그룹으로 **한 번에 하나씩** 돌고, 대기 중인 실행은 새 실행이 들어오면 취소된다. 이 성질을 이용한다.
1. Settings → Secrets and variables → Actions → Variables → `PDS_HALT` = `1`.
2. Actions → update-portfolio에서 대기·실행 중(노란 점) 실행을 확인한다. 막고 싶으면 **Cancel workflow**. 커밋은 마지막 단계의 push 한 번뿐이라, push 전에 취소되면 아무것도 남지 않는다. 그대로 두면 3의 실행이 그것이 끝날 때까지 기다린다.
3. update-portfolio를 **Run workflow**(입력 비움)로 한 번 실행한다. 이 실행은 앞선 실행이 모두 끝난 뒤에 시작하고 `PDS_HALT`를 읽으므로 `HALTED`로 끝난다. 그 순간 다른 update-portfolio 실행은 없다(보고서의 "대기·실행 중인 다른 실행"이 0건이어야 함).
4. ⛔ 알림이나 `run_status.json`의 `halt_report`로 **직전 실행의 시각·신규 잠금 수**와 **최근 72시간 잠금**을 확인한다. 중지 설정 뒤에 끝난 실행이 잠금을 만들었다면 그 달 목표를 실행할지 직접 판단한다(잠금은 되돌리지 않는다).
5. 중지 중에는 보유 자산을 그대로 둔다. 재개는 변수를 지우거나 `0`으로 바꾼 뒤 다음 실행부터.

## 롤백 절차 (자동 리밸런싱이 먼저 멈춘 상태에서만)
구버전(1.0)은 `PDS_HALT`를 모르고, BLOCKED로 비워 둔 달을 백필로 자동 채운다. 그대로 되돌리면 승인하지 않은 목표 비중이 생길 수 있으므로 아래 순서를 지킨다.
1. **운영자 중지**: 위 "운영자 중지 절차" 1~4를 끝내 `HALTED`와 다른 실행 0건을 확인한다. 보유 자산은 그대로 둔다.
2. **워크플로 끄기**: Actions → update-portfolio → ⋯ → Disable workflow (`gh workflow disable update-portfolio`). 되돌리기 커밋의 push 트리거와 예약 실행이 구버전을 돌리지 못하게 막는다(구버전은 `PDS_HALT`를 무시하므로 이 단계가 핵심).
3. **사전 점검(dry run)**: Actions → rollback-check → `target_ref`=되돌릴 커밋. 오늘 데이터로 그 코드를 현재 `docs/data` 사본에 돌려 새로 잠길 달·빈 달 채우기·기존 달 변경을 보고한다(저장소에 쓰지 않음). 로컬: `python tools/rollback_check.py --ref <commit>`. 종료 코드 2 = 수동 검토 필요.
4. 점검 결과를 확인하고 받아들일 수 있을 때만 `git revert` → push(워크플로가 꺼져 있어 자동 실행 없음).
5. 워크플로를 다시 켤 때는 예약 시각(매일 22:40 UTC = 07:40 KST, 금요일 포함)을 피해서 켜고 곧바로 `commit=false`로 한 번 실행해 결과를 확인한다. 구버전은 `PDS_HALT`를 모르므로, 켜는 순간부터 예약 실행이 실제로 잠그고 커밋한다는 점에 유의한다.
6. 점검에서 빈 달 채우기가 나오면 워크플로를 끈 채 보유를 유지하고, 그 달을 어떻게 처리할지 따로 결정한다.

## GitHub 권한·설정
개인 계정의 공개 저장소다. 현재 협업자는 소유자 1명(admin)이고, main에는 보호 규칙이 없다(2026-11-26 확인, `ops-audit`로 매월 재확인).

### 누가 무엇을 할 수 있나
| 작업 | GitHub가 요구하는 권한 | 이 저장소에서 추가로 요구하는 것 |
|---|---|---|
| update-portfolio 수동 실행(수동 해제 요청 포함), 실행 취소, 워크플로 끄기 | 쓰기(write). 개인 저장소에서는 소유자와 모든 협업자 | – |
| 수동 해제가 **실제로 잠기는 것** | – | 요청자 admin(개인 저장소에선 소유자뿐), main에서 실행, 환경 `pds-manual-release` 검토자 승인, 사유, 데이터 점검 전부 통과 |
| `PDS_HALT` 설정·해제 | 개인 저장소: 저장소 협업자 | – |
| 환경·규칙·Actions 설정, 협업자 관리 | 소유자 | – |
| 쓰기 권한이 있는 GitHub App·토큰 | 앱/토큰 권한 범위 | 실행·push는 가능하지만 환경 승인은 검토자로 지정된 사람만 가능 → 앱 단독으로는 수동 해제를 잠글 수 없다 |

### 수동 해제 승인: 지금은 "본인 재확인"이지 독립 2인 승인이 아니다
실행 이력(`manual_releases.json`, Actions 기록)은 사후 기록이라 실행 자체를 막지 못한다. 수동 실행은 쓰기 권한만 있으면 누구나(협업자, 쓰기 권한 앱·토큰 포함) 할 수 있다. 그래서 요청과 승인을 나눴다: 요청(Run workflow + 사유) → 보호 환경에서 사람 검토자 승인 → 파이프라인이 GitHub API로 요청자 권한·승인 기록·환경 설정을 다시 확인 → 데이터 점검.

**현재(1인 저장소)의 성격과 한계**
- 요청자와 승인자가 **같은 사람(소유자)** 이다. 이것은 독립적인 2인 승인이 아니라 **실행 전 본인 재확인 절차**다. 기록에도 `approval_type: "self-confirmation"`(본인 재확인)으로 남고, 알림에 "본인 재확인(1인 승인)"으로 표시된다.
- 막아 주는 것: 실수로 누른 실행이 곧바로 잠금이 되는 것, 쓰기 권한 앱·토큰 **단독** 실행(승인은 검토자로 지정된 사람만 할 수 있다).
- 막지 못하는 것: 소유자 본인의 잘못된 판단, 소유자 계정 탈취(2FA 필수), 소유자 개인 토큰(PAT) 유출(API로 승인 가능 → PAT를 만들지 않거나 최소 권한·짧은 만료).

**나중에 별도 검토자를 둘 때 (독립 승인으로 바꾸기)**
1. 검토자는 본인 GitHub 계정(2FA)을 쓴다. 검토자는 저장소 읽기 이상 권한이 필요하다. 개인 계정 저장소에서 사람을 지정하려면 협업자로 추가해야 하는데, 개인 저장소의 협업자는 **쓰기 권한**을 받는다(실행·push·`PDS_HALT` 변경 가능). 읽기 전용 검토자가 필요하면 저장소를 무료 조직(Organization)으로 옮겨 Read 역할로 두는 방법이 있다.
2. Settings → Environments → `pds-manual-release` → Required reviewers에 검토자를 추가하고(소유자는 빼도 되고 둬도 된다), **Prevent self-review**를 켠다. 그러면 실행을 시작한 사람은 그 실행을 승인할 수 없다(GitHub가 막음).
3. 코드도 이를 다시 확인한다: 환경에 prevent self-review가 켜져 있으면 요청자 외 다른 사람의 승인이 있어야만 잠긴다. 기록은 `approval_type: "independent"`(독립 승인), 승인자 계정이 함께 남는다.
4. 요청은 계속 소유자(admin)만 할 수 있다(코드가 요청자 admin을 요구). 협업자 검토자는 승인만 한다.
5. 검토자가 1명뿐인데 Prevent self-review를 켠 경우, 그 검토자가 요청자가 되면 아무도 승인할 수 없다(요청자는 admin이어야 하므로 보통 해당 없음). `ops-audit`가 승인 형태를 보여 준다.

### 권장 설정 체크리스트 (소유자, Settings)
1. **계정**: 2단계 인증(패스키 권장). Settings → Applications에서 이 저장소에 쓰기 권한이 있는 GitHub App·OAuth 앱을 점검하고, 쓰지 않을 때는 저장소 접근을 빼거나 제거한다. classic PAT는 쓰지 않는다.
2. **협업자**: 추가하지 않는다. 개인 저장소의 협업자는 쓰기 권한이라 실행·변수 변경이 가능하다.
3. **Rules → Rulesets → New branch ruleset** `main-protect`: Enforcement **Active**, Target **Default branch**, Rules **Restrict deletions**와 **Block force pushes**, Bypass list 비움.
   - "Require a pull request before merging"와 "Require status checks"는 켜지 않는다. 매일의 데이터 커밋은 GITHUB_TOKEN 봇이 main에 직접 push하는데, 이 봇은 우회 목록에 넣을 수 없어 운영이 멈춘다. 코드 변경은 PR로만 하고(관행), PR마다 `tests` CI와 main 기준 `data-guard`가 돈다(아래 "운영 데이터 보호").
   - **한계**: 이 두 규칙은 이력 삭제·덮어쓰기만 막는다. 일반 push(빨리 감기)는 막지 못한다. 소유자, 협업자, 쓰기 권한 GitHub App·토큰·배포 키, `contents: write`를 가진 워크플로는 main에 직접 코드를 올릴 수 있다. 그래서 아래 "쓰기 경로 최소화"로 줄이고, `ops-audit`로 사후에 찾아낸다.
4. **Actions → General**
   - Actions permissions: "Allow 소유자, and select non-소유자, actions and reusable workflows" + "Allow actions created by GitHub"(checkout·setup-python·upload-artifact만 사용).
   - Workflow permissions: **Read repository contents and packages permissions**(기본 읽기). update.yml만 `contents: write`를 직접 선언하고, 나머지 워크플로는 읽기 권한만 쓴다.
   - "Allow GitHub Actions to create and approve pull requests" **끔**.
   - Fork pull request workflows: **Require approval for all external contributors**.
5. **Environments → New environment** `pds-manual-release` (없으면 수동 해제는 항상 거부된다)
   - Required reviewers: 소유자. Prevent self-review: 혼자면 끔(켜면 아무도 승인 못 함). 이 상태는 본인 재확인이다(위 설명). 별도 검토자를 두면 켠다.
   - Deployment branches and tags: Selected branches → `main`.
   - "Allow administrators to bypass configured protection rules" **끔**(우회하면 승인 기록이 없어 어차피 거부된다).
6. **Secrets and variables → Actions**: Secrets는 `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`만. Variables는 `PDS_HALT`(평소에는 없음).
7. **점검**: Actions → ops-audit(매월 11일 자동, 수동 실행 가능)이 협업자·main 규칙·환경 설정·승인 형태·워크플로 권한·최근 35일 main 커밋 출처를 위 권장값과 비교해 보여 준다. Actions 기본 권한 등 GITHUB_TOKEN으로 읽을 수 없는 항목은 직접 확인한다.
8. **실패 알림**: 계정 Settings → Notifications → Actions에서 "실패한 워크플로만" 이메일 알림을 켠다. 예약 실행의 알림은 워크플로를 처음 만든 사람(또는 다시 켠 사람)에게 간다. Telegram이 실패해도 이 경로로 실패가 보인다.
9. **Environments → New environment** `pds-lock-restore` (잠금 복구 PR 승인. 없으면 복구 PR은 항상 실패하고 일반 PR에는 영향 없음)
   - Required reviewers: 소유자. Prevent self-review: 혼자면 끔(위 5와 같은 이유).
   - "Allow administrators to bypass configured protection rules" **끔**(켜져 있으면 검사 코드가 복구를 거부한다).
   - Deployment branches and tags: Selected branches → `main`. data-guard는 `pull_request_target`이라 main 기준으로 실행되므로 이것으로 충분하고, 다른 브랜치의 워크플로는 이 환경 승인을 요청할 수 없다.
10. **Actions → Policies** (2026-12-02부터 필요): GitHub는 이날부터 공개 저장소의 `pull_request_target` 실행을 기본 차단한다. 새 정책을 만들고 Enforcement **Active**, 대상 워크플로는 `.github/workflows/data-guard.yml` **하나만**, Restrict events에서 `pull_request_target`만 허용한다(모든 워크플로·모든 이벤트 허용은 하지 않는다. 화면의 표기는 다를 수 있다). 정책이 없으면 data-guard는 실행되지 않고(통과로 표시되지 않음), PR에는 `tests`의 보조 검사만 남는다. 설정 뒤 아무 PR이나 열어 data-guard 실행 요약의 "검사 코드 출처" 표를 확인한다.

### main 쓰기 경로 최소화 (운영 기준)
main에 쓸 수 있는 경로는 두 가지만 둔다.
| 경로 | 무엇을 | 통제 |
|---|---|---|
| 소유자의 PR 병합 | 코드·워크플로·README | 브랜치 → PR → `tests` CI 통과 → diff 확인 → 병합. main에 직접 push하지 않는다 |
| update-portfolio 봇(GITHUB_TOKEN) | `docs/data`만 | 워크플로 기본 권한 `{}`, `run` 작업만 `contents: write`. 커밋 단계가 `docs/data` 밖 파일이 스테이징되면 커밋을 거부한다 |

그 밖의 쓰기 수단은 없애거나 줄인다.
- **다른 워크플로**: 모두 읽기 권한만(`tests`, `rollback-check`, `ops-audit`). 새 워크플로를 추가할 때도 `contents: write`를 주지 않는다(`ops-audit`가 검사).
- **GitHub App**(예: Claude 연동): 설치 범위를 이 저장소로 한정하고, 작업이 없을 때는 이 저장소 접근을 빼거나 앱을 제거한다. 매월 Settings → Applications 확인.
- **개인 토큰(PAT)**: 만들지 않는다. 꼭 필요하면 fine-grained, 이 저장소만, Contents 읽기 전용, 만료 30~90일. classic PAT(repo·workflow 범위)는 쓰지 않는다.
- **배포 키(Deploy keys)**: 두지 않는다(필요하면 읽기 전용).
- **협업자**: 두지 않는다(개인 저장소 협업자는 쓰기 권한).
- **사후 탐지**: `ops-audit`가 최근 35일 main 커밋에서 ① 봇 커밋이 `docs/data` 밖을 바꿨는지, ② 봇 외 커밋이 PR 병합 없이 들어왔는지를 표시한다. 저장소를 만들 때의 초기 직접 push는 처음 한 번 경고로 보일 수 있다.
- **더 강한 대안(나중에)**: 봇 데이터를 별도 브랜치(`data`)에 쓰고 Pages를 그 브랜치에서 배포하면, main에 "PR 필수 + CI 필수"를 걸 수 있다. Pages 설정과 화면 데이터 경로를 바꿔야 하므로 이번 범위에서는 하지 않았다.

## 실행 결과 기록·전달
매 실행 결과는 다음 네 곳에 남는다. ① `docs/data/run_status.json`(매 실행 커밋, 최근 60회 `recent`, git 이력에 영구 보존) ② Actions 작업 요약(Validation summary) ③ Actions 아티팩트 `pds-lite-run-<실행 ID>`(90일: `docs/data`, `run_result.json`, `run.log`, `release_auth.json`) ④ 필요할 때 Telegram. `run_result.json`은 파이프라인이 이번 실행에서 한 일을 워크플로에 넘기는 파일로, 커밋하지 않고 아티팩트에만 남는다.

| 결과 | 기록되는 곳 | 전달(Telegram) |
|---|---|---|
| 정상(새 잠금 없음, FOLLOW_OFFICIAL) | `portfolio_latest.json`, `run_status.json` | 없음 |
| 대기 WAIT | 첫날 `official_events.json` PENDING. 매일 `run_status.json` | 대기 시작 ⏳ 1회. 반복 확인은 없음 |
| 원작자 Core 불일치 | `official_events.json` CORE_MISMATCH | ⚠️ 1회 |
| 잠금 LOCKED | `official_history.json`, `official_events.json` LOCKED, `portfolio_latest.json` | ✅ (커밋 성공 후에만) |
| 10일 마감 BLOCKED (HALTED) | `official_events.json` BLOCKED, `portfolio_latest.json` action, 화면 배너 | ⛔ 1회 |
| 수동 해제·거부 | `manual_releases.json`, `official_events.json` | 🔓 / ✋ |
| 운영자 중지 HALTED | `run_status.json`의 `halt_report`만(다른 파일 무변경), 화면 배너 | ⛔ 1회 |
| 파이프라인 실패 | `run_status.json`(ok:false, 로그 끝 40줄)만, 데이터 무변경 | 실패 알림 |
| 커밋·push 실패 | 저장소에는 남지 않음 → 아티팩트·작업 요약·실행 로그, 실행이 실패로 표시 | "커밋 실패: 결과 저장 안 됨" 알림(이번 판단 포함) |
| Telegram 전달 실패 | `run_status.json`의 `delivery`(실패 사유)·`notify_pending`을 추가 커밋, 실행은 실패로 표시(GitHub 실패 알림) | 다음 성공 실행이 "(재전송)"으로 다시 보냄 |
| Telegram 미설정 | `run_status.json`·아티팩트의 `delivery: skipped_no_telegram_secret` | 없음 |
| 새 잠금 저장 → 월간 외부 백업 | `run_status.json`의 `lock_manifest`(매 실행: 개월 수·범위·SHA-256·git blob, 이번 실행 저장 여부), 작업 요약 "📦 잠금 백업", 매월 `ops-audit` | ✅·🔓 알림 끝에 백업 체크리스트(대상 파일, 개월 수, 마지막 신호월, 전체 SHA-256, 데이터 커밋, 내려받기 링크). 외부 매니페스트 마지막 SHA-256이 다르면 백업 필요. `backup_due`는 "새 잠금이 저장돼 백업할 때가 됐다"는 뜻이지 **백업 완료가 아니다**(시스템은 외부 백업을 볼 수 없음) |

- 잠금 알림은 커밋이 성공한 뒤에만 보낸다. 저장되지 않은 잠금을 "확정"으로 알리는 일을 막기 위해서다.
- 커밋과 Telegram이 둘 다 실패해도 ③ 아티팩트, ② 작업 요약, 실행 로그, GitHub 실패 알림으로 결과와 오류를 확인할 수 있다.

## 테스트·회귀 검증 구조 (모든 PR 공통)
`tests` 워크플로가 브랜치 push와 PR마다 돈다(저장소에 쓰지 않음).
- **비교 기준 고정**: 첫 작업(`baseline`)이 대상 브랜치 커밋과 merge-base(PR 분기점)를 한 번 정해 이후 작업에 넘기고, 작업 요약에 SHA를 표시한다. 실행 도중 main이 바뀌어도 기준은 그대로다. PR 실행은 "PR을 대상에 합친 결과"를 시험하므로 대상 커밋과, 브랜치 push 실행은 분기점과 비교한다.
- **필수 통과(실패 시 병합 불가)**: Fail-safe, 10일 마감, 수동 해제·승인, 운영자 중지, 잠금·감사기록 추가 전용, 커밋된 잠금 파일 **바이트 불변**, 실데이터에서 원작자 공개 Core **116/116 일치**, 워크플로 안전 배선, 알림 전달.
- **참고 정보(실패 조건 아님)**: 비교 기준 코드 대비 Core·Adaptive·공식 목표·잠금 항목 차이, 화면 JSON 키 변화(`regression_*.md`, 작업 요약). 원작자 데이터가 바뀌면 116/116 검증이 실패하고, 작업 요약과 아티팩트에 워크북 지문(행 수·해시)과 불일치 달이 남아 따로 검토할 수 있다.
- **롤백 검증**: 파이프라인 경로(`run_pipeline.py`, `requirements.txt`, `tools/`, `update.yml`)가 바뀐 PR에서만 돈다. "이 PR을 되돌리면 대상 커밋의 코드가 이 브랜치의 파일을 읽고, 잠긴 달을 바꾸거나 비워 둔 달을 채우지 않는가"를 합성 데이터와 실데이터로 확인한다.
- **운영 데이터 보호(`data-guard`, 판정 기준)**: PR은 `docs/data`를 바꿀 수 없다(봇만 쓴다). 위의 "잠금 바이트 불변"은 파이프라인이 커밋된 파일을 보존하는지를, 이 검사는 PR이 파일 자체를 바꾸지 않았는지를 본다. 검사는 `.github/workflows/data-guard.yml`이 `pull_request_target`으로 실행한다. GitHub는 이 이벤트의 워크플로 파일을 PR이 아니라 **main에서** 읽고, 워크플로는 main 커밋의 `tools/lock_guard.py`를 꺼내 실행한다. 그래서 PR이 이 워크플로·`tests.yml`·검사 코드를 고치거나 지워도 그 PR의 판정은 바뀌지 않는다(고친 내용은 병합 뒤부터 적용). 보안 경계: PR 코드는 체크아웃하지도 실행하지도 않는다(작업 트리 없는 bare 저장소에 git 객체만 받아 `git diff/show/log`로 읽음). 외부 액션·pip 설치 없음, 비밀값 없음, 토큰은 읽기 전용이고 복구 PR의 승인 조회에만 넘긴다. 이벤트 값(브랜치 이름 등)은 환경 변수로만 받아 검증·인용한다. 실행 요약 맨 위 **검사 코드 출처** 표가 매번 워크플로 출처(`refs/heads/main`과 커밋), 실행한 검사 코드 blob = main의 blob, PR이 바꾼 검사 코드·워크플로가 쓰이지 않았음을 보여 준다. `tests` 워크플로의 `lock-guard`는 보조 신호다(PR 자신의 워크플로라 우회 가능, 일반 PR만). 유일한 예외는 손상된 기록을 과거 봇 버전으로 되돌리는 **잠금 복구 PR**(브랜치 `restore/RESTORE-YYYYMMDD-N`, 라벨 `lock-restore`, `docs/data/lock_restores.json` 기록 1건, `LOCK_RESTORED` 사건 1건, `PDS_HALT` 중, 보호 환경 `pds-lock-restore` 승인 — data-guard 실행마다 1회)이며, 기록의 해시·개월 수·전후 비교를 모두 다시 계산해 대조한다. **병합 판단**: main에 "Require status checks"를 켜지 않으므로(봇 직접 push, 체크리스트 3) GitHub가 병합을 막지는 않는다. 소유자는 `data-guard`가 통과(초록)인 PR만 병합한다. `ops-audit`가 data-guard 최근 실행과, main에 사람이 낸 `docs/data` 변경이 복구 PR 경유인지를 매월 확인한다. 2026-12-02부터 공개 저장소의 `pull_request_target`은 허용 정책이 없으면 막힌다(체크리스트 10). 막힌 실행은 통과로 표시되지 않는다.
- **v1.0 전환 검증(PR #1 근거, 7개)**: 기본 실행에서 빠져 있다. PR에 라벨 `v10-migration`을 붙이거나, Actions → tests → Run workflow에서 `v10_migration`을 켜거나, 로컬에서 `PDS_V10_MIGRATION=1 python -m pytest tests -m v10_migration`으로 실행한다.

