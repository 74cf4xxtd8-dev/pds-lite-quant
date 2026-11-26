# ② PDS-Lite 사용자·운영자 매뉴얼

- 기준: main `4c747f3`(코드 = `00521d2`), 2026-11-26
- 근거 표기와 용어는 [handbook/README.md](README.md), 동작 원리는 [① 설계서](01-system-design.md)를 따른다.
- 이 매뉴얼의 설정 화면 경로는 GitHub 웹 기준이다. GitHub 화면 문구는 바뀔 수 있다.

---

## 0. 먼저 알아둘 세 가지

1. **실제 리밸런싱은 Official(공식 목표)만 기준으로 한다.** 공식 목표는 원작자 공식 자료가 공개되고 원작자 Core와 일치가 확인된 달만 잠긴다. 화면 상단에 ⏳ 또는 ⛔ 배너가 있으면 새 공식 목표가 없다는 뜻이다. 이때는 **아무것도 사고팔지 않고 지금 보유를 유지한다.**
2. **Preview는 공식 목표가 아니다. 매매에 쓰지 않는다.** Preview는 월중 추정이거나, 잠금 대기·보류 중인 달의 계산값이다. 공식 목표와 크게 다를 수 있다(예: 2026-11-25 Preview의 BIL 55% vs Official 16.9%).
3. **이 시스템은 주문을 내지 않는다.** 목표 비중을 보여 줄 뿐이다. 주문은 직접 증권사 앱에서 한다. 주문 시점·수량·호가는 사용자의 판단이다.

추가로 꼭 알아둘 것: **RB 100%는 방어 모드가 아니다.** "NORMAL · RB 100%"는 Adaptive = Core 그대로라는 뜻이다. 화면에 BIL이 크게 보여도 그것은 Core 규칙이 고른 것이다(RB 때문이 아님). DEFENSIVE(▼, RB < 100%)일 때만 Core를 줄이고 나머지를 BIL로 둔다.

**한 달 흐름 요약**: 월말 다음 날 아침 ⏳ 대기 → (보통 월말 2일 뒤 공개) → ✅ 공식 확정 알림·화면 → 그 목표로 리밸런싱 → 다음 달 말까지 보유. 11일 아침까지 확정이 안 되면 ⛔ 보류: 보유 유지, 필요하면 수동 해제(8장).

---

## 1. 대시보드 사용법

### 1.1 열기
- 주소: `https://74cf4xxtd8-dev.github.io/pds-lite-quant/`
- 휴대폰 브라우저에서 열고 "홈 화면에 추가"하면 앱처럼 쓸 수 있다.
- 다크 모드는 휴대폰 설정을 따른다.
- 데이터가 갱신된 뒤 화면에 반영되기까지 몇 분 걸릴 수 있다. 새로고침으로 다시 읽는다.

### 1.2 화면 구성 (위에서 아래로) [코드: `docs/index.html`]

| 영역 | 보이는 것 | 읽는 법 |
|---|---|---|
| 머리글 | "갱신 YYYY-MM-DD HH:MM KST · 시장 기준 YYYY-MM-DD", 오른쪽 배지 "자체 연산" | 갱신 = 화면 데이터를 만든 시각. 시장 기준 = 마지막 미국 종가 날짜 |
| 상단 배너 | 평소에는 없음. ⏳(잠금 대기) 또는 ⛔(보류·운영자 중지) | 배너가 있으면 **리밸런싱하지 않는다**(1.3절) |
| 상태 카드 | "공식 목표 (Official)" 또는 Preview 라벨, "신호 YYYY-MM → 보유 YYYY-MM", 상태 배지(● NORMAL / ▼ DEFENSIVE), RB %, 설명 줄, 자산군 막대·범례 | 1.4절 |
| ⚠ 확인 필요 | 노란 상자. 운영상 확인할 경고 | 1.5절 |
| ℹ 추정 근거 | 회색 상자. Preview 탭에서만. 추정에 쓴 입력 설명 | 정상 안내(경고 아님) |
| Official / Preview 전환 | 두 버튼 | 아래 표 전체가 고른 쪽으로 바뀐다 |
| 탭 | 미국 직투 / 국내 IRP / 추이 | 1.7~1.9절 |
| 하단 | 원작자 Core 대조 결과, 계산식, 면책 문구 | "116/116개월 일치"가 정상 |

### 1.3 상단 배너
| 배너 | 뜻 | 할 일 |
|---|---|---|
| 없음 | 공식 목표 유효(`FOLLOW_OFFICIAL`) | 공식 목표가 새로 확정된 달이면 그 목표로 리밸런싱, 아니면 보유 유지 |
| "⏳ YYYY-MM 잠금 대기: … 리밸런싱하지 말고 현재 보유를 유지하세요" | 신호월은 끝났지만 공식 자료가 아직 없음(`WAIT`) | 보유 유지. 매일 아침 자동 재확인 |
| "⛔ YYYY-MM 자동 리밸런싱 중지: … 현재 보유 자산을 그대로 유지하세요" | 10일 마감까지 공식 자료 미확보(`HALTED`, 보류) | 보유 유지. 8장 수동 해제 검토 |
| "⛔ 운영자 중지(PDS_HALT): …" | 운영자가 중지를 걸었음 | 보유 유지. 7장 |

### 1.4 상태 카드
- **라벨**: Official 탭은 "공식 목표 (Official)". Preview 탭은 다음 셋 중 하나다.
  - "월중 추정 (Preview · 실행 금지)"
  - "잠금 대기 (공식 데이터 미확보 · 실행 금지)"
  - "잠금 보류 (10일 마감 경과 · 실행 금지)"
- **신호 → 보유**: 신호월은 그 달 말 종가로 계산한 달, 보유월은 그 목표를 들고 있는 달이다.
- **배지·RB**: ● NORMAL + 100%가 기본이다. ▼ DEFENSIVE면 RB가 100% 미만이다.
- **설명 줄**
  - NORMAL이면 "Adaptive = Core 그대로.", DEFENSIVE면 "Core 비중 × RB, 나머지는 BIL."
  - 상태 출처가 원작자 값이면 "상태 출처: 원작자 공개 워크북", 자체 대체 규칙이면 "상태 출처: 자체 대체 규칙(원작자 RL 아님)"이 붙는다. 기본값(RB = 1)이면 출처 문구가 없다.
  - 회전율·예상 비용은 직전 목표 대비 편도 회전율과 Σ|변화| × 10bp다. 월중 가격 변동·스프레드·환전·세금이 빠져 실제보다 낮다.
- **자산군 막대**: 주식·채권·실물/기타·현금(BIL) 비중. 막대는 추이 탭에서 고른 기준(Adaptive/Core)을 따른다.

### 1.5 경고(⚠)와 안내(ℹ)
- ⚠ 상자: 실행 단위 경고(대기·보류·중지·자료 수신 실패·원작자 값 변경 등)는 두 탭 모두에 나온다. Official 자신의 입력 메모(예: F2R이 공식이 아님)는 Official 탭에 경고로 나온다. 공식 목표는 F2R 공식값으로만 잠기므로 이 경고가 Official에 나오면 이상 신호다.
- ℹ 상자(Preview 탭만): Preview 자신의 입력 메모. 예: "F2R 공식 Top-4 미공개 → 원작자 Preview 순위로 추정. 월중 추정이라 정상이며…". 정상 안내다.
- 코드별 뜻은 2.6절.

### 1.6 Official / Preview
- Official: 지금 유효한 공식 목표. 매매 기준.
- Preview: 진행 중인 달의 월중 추정. 잠금 대기·보류 중에는 대기·보류 중인 그 달의 계산값이 들어간다. **실행 금지.** 전월比는 현재 Official 대비 차이다.
- Preview가 없으면 Preview 버튼이 비활성화된다.

### 1.7 미국 직투 탭
- 칸: 종목(티커·이름), Core, Adaptive, 전월比(%p, 직전 공식 목표 대비).
- **매매 기준은 Adaptive 칸이다.** RB = 100%면 Core와 같다.
- "총 평가액 USD"를 넣으면 종목별 목표 금액(평가액 × Adaptive 비중, 달러 정수 반올림)이 표시된다. 수량은 목표 금액 ÷ 현재가로 직접 계산한다.
- 이 탭 아래의 "월말 신호 후 첫 미국 거래일 종가 근처 체결 가정" 문구는 원작자 백테스트 가정이다. 이 시스템의 실제 운영에서는 **공식 확정 뒤에** 리밸런싱한다(③ 7장 차이 목록).

### 1.8 국내 IRP 탭
- 미국 ETF 목표를 국내 상장 ETF로 바꾼 표다. 같은 국내 ETF로 가는 비중은 합쳐져 있다(예: SPY + IWM → TIGER 미국S&P500).
- 위쪽 두 칸:
  - "위험자산 (상한 70%)": 상한 적용 뒤 위험자산 합계. 목표는 68% 이하다.
  - "상한 초과→안전자산 이동": 68%를 넘은 만큼 329750(TIGER 미국달러단기채권액티브)으로 옮긴 비중.
- "IRP 평가액 KRW"를 넣으면 종목별 금액(천 원 단위)이 표시된다.
- "대체 매핑 메모"를 펼치면 대체 사유(예: 원자재 → 금현물, TIPS → 미국10년국채)가 나온다.
- 주의
  - 대체 매핑과 위험/안전 구분은 PDS-Lite가 정한 것이다. 원작자 전략에는 IRP가 없다. 매수 가능 여부·규정상 분류는 증권사에서 확인한다 [미확인].
  - 이 탭의 Core/Adaptive 기준은 **추이 탭의 Adaptive/Core 토글**을 따른다. 평소에는 Adaptive로 둔다. RB = 100%면 같은 값이다.

### 1.9 추이 탭
- 기준(Adaptive/Core)과 기간(1Y/3Y/5Y/전체)을 고른다.
- "자산군 비중(보유월 기준)" 누적 그래프, Risk Budget 그래프(DEFENSIVE 달에 점), "표로 보기".
- **해석 주의**: 이 이력 116개월은 모두 사후 재구성(RECONSTRUCTED)이다. RB 이력에는 PDS-Lite 자체 대체 규칙 결과(2017-03~2021-03, DEFENSIVE 2020-03·04)와 원작자 공개값(2021-04 이후)이 섞여 있다. 실시간 성과 기록으로 읽으면 안 된다(① 6.2절).

### 1.10 금액 입력의 저장
USD·KRW 입력값은 그 휴대폰 브라우저에만 저장된다(localStorage). 서버나 저장소로 보내지 않는다. 다른 기기에서는 다시 입력해야 한다.

---

## 2. 지표·상태값 사전

### 2.1 action 코드 (`portfolio_latest.json` → `action`, `run_status.json` → `action`)
| 코드 | 뜻 | 화면 |
|---|---|---|
| `FOLLOW_OFFICIAL` | 공식 목표 유효. 이번 실행에서 새로 잠갔거나 잠글 달이 없음 | 배너 없음 |
| `WAIT` | 신호월 종료, 공식 자료 대기(10일 마감 전) | ⏳ 배너 |
| `HALTED` | 10일 마감 보류 **또는** 운영자 중지. `reason`이 "operator halt (PDS_HALT)"면 중지 | ⛔ 배너 |

`in_force`는 지금 유효한 공식 신호월, `month`는 판정 대상 달, `final_check_utc`는 그 달의 마지막 확인 시각(10일 22:00 UTC)이다.

### 2.2 블록 status
`OFFICIAL`(잠긴 공식) · `PENDING`(잠금 대기) · `BLOCKED`(잠금 보류) · `PREVIEW`(월중 추정)

### 2.3 state와 RB
| state | RB | 뜻 |
|---|---|---|
| NORMAL | 100% | Adaptive = Core. 방어 아님 |
| DEFENSIVE | 55% ~ 100% 미만 | Core × RB + BIL × (1 − RB) |

### 2.4 Adaptive 출처 (`sources.adaptive`)
| 값 | 뜻 |
|---|---|
| `REFERENCE_OFFICIAL_AT_LOCK` 등 `REFERENCE_<상태>_AT_LOCK` | 잠글 때 이미 공개된 원작자 값을 검사 후 적용(현재 규칙) |
| `CORE_DEFAULT_REFERENCE_NOT_PUBLISHED` | 잠글 때 원작자 값이 아직 없어 RB = 1 |
| `CORE_DEFAULT_REFERENCE_REJECTED` | 원작자 값이 검사를 통과하지 못해 RB = 1 |
| `CORE_DEFAULT_BACKFILL` | 과거 재구성 달, RB = 1 |
| `CORE_DEFAULT_PREVIEW` | Preview, RB = 1 |
| `CORE_ONLY` | `core_only` 모드, RB = 1 |
| `REFERENCE_HISTORY`, `REFERENCE_FORWARD_MONITOR`, `REFERENCE_OFFICIAL` | **v1.0이 잠근 기존 116개월의 표기.** 원작자 공개값 |
| `PROXY_VOL_TREND` | **v1.0이 잠근 기존 116개월의 표기**(2017-03~2021-03), 또는 진단 모드. PDS-Lite 자체 대체 규칙 |

### 2.5 F2R 출처 (`sources.f2r`)
`F2R_PUBLISHED`(원작자 공식 Top-4. 잠금은 이것만) · `F2R_PREVIEW`(원작자 월중 Preview) · `F2R_PROXY_MOMENTUM`(원작자 값 없음, 자체 모멘텀 대체)

### 2.6 경고 코드 (`flags`)
| 코드 | 뜻 | 대응 |
|---|---|---|
| `OFFICIAL_PENDING <월>: <사유>` | 잠금 대기 | 보유 유지 |
| `REBALANCE_HALTED <월>: <사유>` | 10일 마감 보류 | 보유 유지, 8장 |
| `OPERATOR_HALT: …` | 운영자 중지 | 7장 |
| `MANUAL_RELEASE_REJECTED <월>: <사유>` | 수동 해제 거부 | 사유 확인 |
| `UNRATE_FROM_CACHE` | 실업률을 FRED·BLS에서 못 받아 캐시 사용 | 다음 실행에서 대개 해소. 수동 해제는 거부됨 |
| `F2R_UNAVAILABLE` | 원작자 F2R JSON 수신 실패 | 잠금 불가. 계속되면 원작자 주소·형식 변경 의심 |
| `REFERENCE_UNAVAILABLE` | 원작자 워크북 수신 실패 | 잠금 불가(원작자 Core 없음) |
| `REFERENCE_REVISED <월|상태> -> …` | 원작자가 이미 공개한 Adaptive 값을 수정 | 참고. 잠금은 그대로 |
| `<월>: AUTHOR_REFERENCE <상태> RB … (published after lock, not applied)` | 잠근 뒤 공개된 원작자 값이 잠금과 다름 | 참고. 잠금은 그대로 |
| `<월>: F2R_PREVIEW` / `F2R_PROXY_MOMENTUM` | 그 블록의 F2R이 공식이 아님 | Preview면 정상(ℹ). Official이면 이상 |
| `<월>: UNRATE_CARRIED` | 실업률 최신 달 미발표, 직전 값 사용 | 참고 |
| `<월>: ADAPTIVE_PROXY` | 자체 대체 규칙 사용(진단 모드) | 운영 모드에서는 나오지 않아야 함 |
| `BACKFILL_SKIPPED <월>: …` | 백필 중 F2R 공식이 없는 달을 건너뜀 | 최초 설치 때만 |
| `PREVIEW_UNAVAILABLE: …` | Preview 계산 실패 | 참고(공식 목표와 무관) |

### 2.7 감사 사건 (`official_events.json`, Telegram)
| 사건 | 언제 | 알림 |
|---|---|---|
| `PENDING` | 신호월 잠금 대기 시작(월 1회) | ⏳ |
| `CORE_MISMATCH` | 자체 Core가 원작자 Core와 1e-6 넘게 다름(월 1회) | ⚠️ |
| `LOCKED` | 공식 확정 | ✅ |
| `BLOCKED` | 10일 마감 보류(월 1회) | ⛔ |
| `MANUAL_RELEASE` / `MANUAL_RELEASE_REJECTED` | 수동 해제 잠금 / 거부(요청마다) | 🔓 / ✋ |
| `OPERATOR_HALT` | 중지 시작. **파일에는 남지 않고** `run_status.json`의 `notify`·`recent`에만 | ⛔ |

### 2.8 잠금 기록 분류
- `origin`: `LIVE`(예약 실행이 실시간 잠금), `BACKFILL_RECOMPUTED`(과거 재구성), `MANUAL_RELEASE`(수동 해제).
- `record_class`(화면 JSON `history`): `LIVE`(origin LIVE + 컷오프 14일 이내 잠금), `RECONSTRUCTED`(그 외). 대시보드에는 표시되지 않는다.

---

## 3. 자동 실행 일정

| 실행 | UTC | KST |
|---|---|---|
| 월말 확인 | 매월 28~31일 22:40 | 29일~다음 달 1일 07:40 |
| 1~10일 매일 확인 | 매월 1~10일 22:40 | 2~11일 07:40 (11일 07:40이 **마지막 확인**) |
| 주간 Preview 갱신 | 금요일 22:40 | 토요일 07:40 |
| 권한 점검(ops-audit) | 11일 03:17 | 11일 12:17 |
| 코드 변경 시 | main에 `run_pipeline.py`·`update.yml`·`requirements.txt`가 바뀌면 즉시 1회 | – |

- 예약 실행은 GitHub 사정으로 몇 분~수십 분 늦게 시작될 수 있다 [미확인: 실제 지연 폭].
- 1~10일은 보유월 기준이다. 예: 11월 신호월이면 12월 1~10일(UTC).

### 3.1 12월 예시 (2026-11 신호월) [추정 포함]
| 시각(KST) | 예상 상황 |
|---|---|
| 11/29~11/30 07:40 | 11월이 아직 안 끝남. Official 10월 유지, Preview 11월 갱신 |
| **12/1(화) 07:40** | 11월 종료(11/30 마지막 거래일). F2R 공식 미공개 → ⏳ 잠금 대기 시작 알림 1회, 배너 ⏳ |
| 12/2~ | 매일 아침 재확인. 대기가 이어지는 동안 알림 없음 |
| 원작자 공개 다음 날 아침 | ✅ 11월 공식 확정 알림. 9·10월 사례로는 월말 2일 뒤 공개 → **12/3 07:40 KST 전후 확정 예상** [추정] |
| 확정 이후 | 새 Official(11월→12월 보유)로 리밸런싱 |
| **12/11(금) 07:40** | 마지막 확인. 이때까지 미확정이면 ⛔ 보류 알림 1회. 보유 유지 |
| 12/11 12:17 | ops-audit 자동 실행 |

### 3.2 첫 실제 운영 검증(12/1~12/11)에서 볼 것
- [ ] 12/1 아침 ⏳ 알림이 한 번만 왔는가(Telegram 설정 후). 배너와 Preview 라벨 "잠금 대기"가 보이는가.
- [ ] 대기 중 매일 실행이 성공하고 추가 알림이 없는가(`run_status.json` `recent`).
- [ ] 확정 아침 ✅ 알림, `official_history.json`에 2026-11이 `origin: LIVE`로 추가됐는가. 기존 116개월은 그대로인가.
- [ ] 화면 하단 원작자 Core 대조가 117/117인가.
- [ ] 확정된 달의 `sources.adaptive`가 무엇인가(`CORE_DEFAULT_REFERENCE_NOT_PUBLISHED`이면 RB 100% = Core 그대로).
- [ ] 확정 뒤 `record_class`가 LIVE인가(컷오프 14일 이내 잠금).
- [ ] 원작자 F2R·Core 공개 시각과 확정 시각의 차이(다음 달 운영 판단에 사용).

---

## 4. 상태별 대응

| 상황 | 어떻게 알 수 있나 | 할 일 | 하지 말 것 |
|---|---|---|---|
| 정상, 새 확정 없음 | 배너 없음, Official 그대로 | 보유 유지 | – |
| **공식 확정** | ✅ 알림, 배너 없음, Official 신호월이 새 달 | 5장 절차로 리밸런싱 | Preview 값 사용 |
| 잠금 대기 | ⏳ 알림(시작 때 1회), ⏳ 배너 | 보유 유지, 기다림 | Preview나 다음 달 추정으로 미리 매매 |
| 원작자 Core 불일치 | ⚠️ 알림, `CORE_MISMATCH` 사건 | 보유 유지. 원인 확인(9.1절). 계속되면 10일 보류로 간다 | 잠금을 강제하려고 설정 변경 |
| 10일 보류 | ⛔ 알림·배너, `BLOCKED` | 보유 유지. 원작자 자료가 늦게라도 공개됐는지 확인 → 8장 수동 해제 검토. 다음 달 공식 목표가 잠기면 리밸런싱 재개 | 직접 계산한 값으로 매매 |
| 수동 해제 거부 | ✋ 알림, `manual_releases.json` `problems` | 사유를 고칠 수 있으면 고친 뒤 다시 요청, 아니면 보유 유지 | – |
| 실행 실패 | "실행 실패: … 직전 Official 유지" 알림, GitHub 실패 메일, `run_status.json` `ok: false` | 직전 Official 유지. 9.1절 | – |
| 커밋 실패 | "커밋 실패: 이번 실행 결과(잠금 포함)가 저장되지 않았습니다" | 결과는 저장되지 않았다. 1~10일이면 다음 실행이 다시 판단한다. 급하면 Run workflow로 재실행 | 알림의 "이번 판단"을 근거로 매매 |
| 알림 전달 실패 | 실행이 실패로 표시(GitHub 메일), `run_status.json` `delivery.status: failed` | 6.4절. 다음 성공 실행이 "(재전송)"으로 보낸다 | – |
| 운영자 중지 중 | ⛔ 운영자 중지 배너·알림 | 보유 유지, 7장 | – |

---

## 5. 월간 리밸런싱 절차

1. ✅ 공식 확정 알림을 받거나, 화면 상단에 배너가 없고 Official의 신호월이 막 끝난 달인지 확인한다.
2. 화면 하단의 원작자 Core 대조가 "N/N개월 일치"인지 확인한다(불일치가 있으면 멈추고 9.1절).
3. Official 탭 → 미국 직투: 총 평가액(USD)을 넣고 Adaptive 칸의 목표 금액을 확인한다.
4. Official 탭 → 국내 IRP(추이 탭 기준이 Adaptive인지 확인): IRP 평가액(KRW)을 넣고 국내 ETF 목표 금액을 확인한다.
5. 증권사 앱에서 현재 보유와 목표의 차이만큼 **직접** 주문한다. 소액 차이는 거래비용을 고려해 생략할 수 있다(사용자 판단).
6. 다음 공식 확정까지 보유한다. 월중에 Preview가 바뀌어도 매매하지 않는다.

---

## 6. Telegram 알림 설정과 확인

### 6.1 무엇이 오나
상태가 바뀔 때만 보낸다: ⏳ 잠금 대기 시작(월 1회), ⚠️ 원작자 Core 불일치, ✅ 공식 확정, ⛔ 10일 보류, 🔓/✋ 수동 해제·거부, ⛔ 운영자 중지(중지 시작 때), 실행·커밋 실패. 1~10일의 반복 확인, 상태 변화 없는 실행은 보내지 않는다. 메시지 끝에 대시보드 주소가 붙는다.

### 6.2 설정 (최초 1회)
1. Telegram에서 `@BotFather` → `/newbot` → 이름 지정 → **봇 토큰**을 받는다. 토큰은 비밀번호처럼 다룬다.
2. 만든 봇과 대화를 열어 아무 메시지나 보낸다(봇이 먼저 말을 걸 수 없기 때문).
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates`를 열어 `"chat":{"id": …}` 숫자를 찾는다. 이것이 **채팅 ID**다.
4. GitHub 저장소 → Settings → Secrets and variables → Actions → **Secrets** 탭 → New repository secret:
   - `TELEGRAM_TOKEN` = 봇 토큰
   - `TELEGRAM_CHAT_ID` = 채팅 ID

1~3은 Telegram 일반 절차이고 이 저장소에서 검증한 것이 아니다 [미확인]. 시크릿 이름은 코드와 같아야 한다 [코드: `update.yml`].

### 6.3 알림 시험 (운영자 중지를 이용한 방법)
상태 변화가 없으면 알림이 오지 않으므로, 운영 데이터를 바꾸지 않는 운영자 중지로 시험한다. **잠금 대기가 시작되기 전(12/1 07:40 KST 이전)**, 예약 시각(07:40 KST)을 피해서 한다.
1. Settings → Secrets and variables → Actions → **Variables** 탭 → New repository variable: `PDS_HALT` = `1`.
2. Actions → update-portfolio → Run workflow → 브랜치 `main`, 입력은 기본값 그대로 → 실행.
3. 확인
   - Telegram에 "⛔ 운영자 중지(PDS_HALT) 확인: …"과 직전 실행·최근 72시간 잠금·다른 실행 0건이 온다.
   - 실행이 초록색(성공)이다. 'Telegram alert (state changes…)' 단계가 실행됐다.
   - `docs/data/run_status.json`에 `"delivery": {"status": "sent"}`가 있다.
   - 대시보드에 ⛔ 운영자 중지 배너가 보인다.
4. Variables에서 `PDS_HALT`를 **삭제**한다.
5. 다시 Run workflow(기본값). `FOLLOW_OFFICIAL`로 끝나고 배너가 사라진다. 이 실행은 상태 변화가 없어 알림이 없다.

### 6.4 알림이 안 올 때
| 증상 | 원인과 조치 |
|---|---|
| Telegram 단계가 회색(skipped) | 시크릿 `TELEGRAM_TOKEN`이 비어 있음. 이름·값 확인. 이 경우 `delivery` 기록도 남지 않는다 |
| Telegram 단계가 빨간색, 실행 실패 | `run_status.json` `delivery.error` 확인. 예: `HTTP 401` = 토큰 오류, `HTTP 400 … chat not found` = 채팅 ID 오류 또는 봇과 대화를 시작하지 않음. 고치면 다음 성공 실행이 "(재전송)"으로 보낸다 |
| `delivery.status: skipped_no_telegram_secret` | 토큰은 있고 `TELEGRAM_CHAT_ID`가 비어 있음 |
| 단계는 성공인데 메시지 없음 | 알릴 사건이 없었음(`notify` 빈 목록). 정상 |

- Telegram이 안 와도 실패는 GitHub 실패 메일로 알 수 있다. 계정 Settings → Notifications → Actions에서 실패한 워크플로 메일을 켠다.
- `run_status.json` `recent[].notified`는 알림 대상 목록이지 전달 증거가 아니다. 전달 여부는 `delivery`로 본다.

---

## 7. PDS_HALT(운영자 중지) 사용법

### 7.1 언제 쓰나
- 잠금이나 공식 목표가 예상과 다르거나 데이터가 이상해 보일 때.
- 롤백·장애 대응의 첫 단계(9.2절).
- 알림 시험(6.3절).

### 7.2 중지하면 일어나는 일 [코드]
- 어떤 달도 새로 잠기지 않는다. 수동 해제도 거부된다.
- `docs/data`에서 `run_status.json`만 바뀐다. 잠금·목표 비중·참고 원장·감사 기록·실업률 캐시는 그대로다.
- 화면에 ⛔ 운영자 중지 배너. 중지가 시작될 때 Telegram ⛔ 1회.
- 화면의 공식 목표와 "갱신" 시각은 중지 직전 상태로 멈춘다.

### 7.3 거는 절차 (실행 중인 작업 처리 포함)
변수는 실행이 시작될 때 한 번 읽힌다. 그래서 변수를 넣기 전에 이미 대기·실행 중이던 실행은 중지를 모른 채 잠글 수 있다. 다음 순서로 한다.
1. Settings → Secrets and variables → Actions → Variables → `PDS_HALT` = `1`.
2. Actions → update-portfolio에서 노란 점(대기·실행 중) 실행을 확인한다. 막고 싶으면 그 실행의 **Cancel workflow**. 커밋은 마지막 push 한 번뿐이라, push 전에 취소되면 아무것도 남지 않는다. 그대로 두면 3의 실행이 그것이 끝날 때까지 기다린다.
3. Run workflow(입력 기본값)로 한 번 실행한다. 이 실행은 앞선 실행이 모두 끝난 뒤 시작하고 `PDS_HALT`를 읽어 HALTED로 끝난다.
4. ⛔ 알림이나 `run_status.json` → `halt_report`에서 다음을 확인한다.
   - 직전 실행의 시각과 신규 잠금 수, 최근 72시간 잠금
   - "대기·실행 중인 다른 실행" 0건
   - 중지 설정 뒤에 끝난 실행이 잠금을 만들었다면, 그 목표를 실행할지 직접 판단한다. 잠금은 되돌리지 않는다.
5. 중지 중에는 보유를 그대로 둔다.

### 7.4 해제
Variables에서 `PDS_HALT`를 삭제하거나 `0`으로 바꾼다. 다음 실행부터 정상 동작한다. 바로 확인하려면 Run workflow를 한 번 실행한다. `false`, `no`, `off`, 빈 값도 해제로 본다. 그 밖의 값(오타 포함)은 중지로 본다.

---

## 8. 수동 해제와 승인

### 8.1 언제 쓰나
**10일 보류(⛔ BLOCKED)된 달**을, 원작자 공식 자료가 마감 뒤에라도 공개된 것을 확인하고 잠그고 싶을 때만 쓴다. 평소에는 쓰지 않는다.
- **다음 달이 먼저 잠기면 보류된 달은 영원히 해제할 수 없다**(빈 달). 해제하려면 다음 달 공식 목표가 잠기기 전에 해야 한다.
- 해제하지 않아도 된다. 그 달은 보유 유지로 지나가고, 다음 달 공식 목표부터 정상 운영된다.

### 8.2 사전 설정 (최초 1회, 소유자)
Settings → Environments → New environment `pds-manual-release`
- Required reviewers: 소유자 계정. Prevent self-review: 혼자 쓸 때는 **끔**(켜면 아무도 승인 못 함).
- Deployment branches and tags: Selected branches → `main`.
- "Allow administrators to bypass configured protection rules": **끔**.

환경이 없거나 검토자가 없으면 수동 해제는 항상 거부된다(안전 쪽). 1인 저장소에서 승인은 **본인 재확인**이지 독립 2인 승인이 아니다. 기록에 `approval_type: self-confirmation`으로 남는다. 별도 검토자를 두는 방법은 README "GitHub 권한·설정"을 따른다.

### 8.3 해제 전 확인
- `run_status.json`의 `action.reason`이나 화면 경고 `REBALANCE_HALTED <월>: <사유>`를 본다.
  - 사유가 **"official data complete only after the lock deadline"**이면 지금은 공식 자료가 갖춰졌다는 뜻이다(해제 가능성 있음).
  - 사유가 "F2R official not published …", "author's Core … not published", "Core differs …"이면 아직 자료가 없거나 맞지 않는다. 해제해도 거부된다.
- 이 사유는 마지막 실행 시점 기준이다. 필요하면 먼저 Run workflow(입력 기본값)로 한 번 실행해 최신 사유를 본다.

### 8.4 절차
1. Actions → update-portfolio → Run workflow
   - Use workflow from: `main`
   - `release_month`: 보류된 신호월(예: `2026-11`)
   - `release_reason`: 확인한 내용(예: "원작자 F2R 공식 Top-4와 Core 12/12 공개 확인, 사유 complete only after the lock deadline")
   - 나머지 기본값(mode `engine`, adaptive_mode `reference_only`, commit 체크)
2. 실행 화면에 "Waiting for review"가 뜨면 **Review deployments** → `pds-manual-release` 선택 → 코멘트 → **Approve and deploy**. 거부하면 아무것도 실행되지 않는다(데이터 무변경).
3. 승인 뒤 파이프라인이 승인 정보와 데이터를 다시 검사한다(① 6.5절 표).
4. 결과
   - **LOCKED**: 🔓 알림. `official_history.json`에 그 달이 `origin: MANUAL_RELEASE`로 추가되고, `manual_releases.json` `outcome: LOCKED`, 배너가 사라진다. 새 Official로 5장 절차를 따른다.
   - **REJECTED**: ✋ 알림. `manual_releases.json` `problems`에 사유가 남는다. 고칠 수 있는 사유면 고친 뒤 다시 요청한다(요청마다 기록이 남고 지워지지 않는다).
5. 수동 해제로 잠긴 달은 `RECONSTRUCTED`로 분류되어 실시간 기록과 구분된다.

---

## 9. 장애 대응과 롤백

### 9.1 장애 유형별 대응
| 장애 | 확인 위치 | 대응 |
|---|---|---|
| 가격 무결성 실패(결측·±25% 급변·고착), 수집 실패 | 실행 로그, `run_status.json` `log_tail` | 대개 일시적. 다음 예약 실행이 다시 시도. 1~10일이라 급하면 Run workflow. 계속되면 로그의 종목·날짜 확인 |
| 실업률 FRED 실패 | 로그 `[warn] UNRATE via FRED failed` | BLS로 자동 대체. 둘 다 실패하면 캐시(`UNRATE_FROM_CACHE`) |
| 원작자 F2R·워크북 수신 실패 | `F2R_UNAVAILABLE`, `REFERENCE_UNAVAILABLE` | 잠금이 안 되고 대기 → 10일 보류로 갈 수 있음. 원작자 공개 주소·형식이 바뀌었는지 확인. 코드 수정은 별도 PR(③ 5장) |
| 원작자 Core 불일치 | ⚠️ 알림, `CORE_MISMATCH` 사건, 화면 하단 불일치 | 잠그지 않음(정상 동작). 원작자 워크북의 그 달 Core와 `portfolio_latest.json` `preview`(대기 중인 달)를 비교. Yahoo 가격 차이 또는 원작자 정의 변경일 수 있음. 판단이 어려우면 PDS_HALT |
| 커밋·push 실패 | "커밋 실패" 알림, 실행 실패 | 결과 미저장. 아티팩트 `pds-lite-run-<ID>`에 이번 결과가 있음. 다음 실행이 다시 판단. 다른 커밋과 동시에 push한 경우가 흔함 |
| 10일 실행 실패 | 실패 알림 | 10일 23:59:59 UTC(11일 08:59 KST) 전에 Run workflow로 다시 실행하면 정상 판정(자료가 갖춰졌으면 잠금)된다. 그 뒤에는 자동 잠금이 없고, 다음 예약 실행(금요일 또는 28일)에서 BLOCKED로 기록된다. 그 사이 보유 유지 |
| 화면이 안 열림·"데이터를 불러오지 못했습니다" | Actions의 pages-build-deployment, 저장소 `docs/data` | 공식 목표는 저장소 `docs/data/portfolio_latest.json`과 `official_history.json`에 그대로 있다 |
| 예약 실행이 멈춤 | Actions 탭에 최근 실행 없음, GitHub 메일 | 60일 무활동 등으로 꺼졌으면 Actions → update-portfolio → Enable workflow |
| **잠금이 이상해 보임** | 화면, `official_history.json` | 매매하지 않는다 → PDS_HALT(7장) → 기록 보존 → 보고. 파이프라인은 잠금을 고치지 않는다. 잠금 수정은 명시 승인된 별도 커밋으로만(③ 6장) |

### 9.2 롤백 절차 (코드를 이전 버전으로 되돌릴 때)
구버전(v1.0)은 `PDS_HALT`를 모르고, 보류로 비워 둔 달을 백필로 자동으로 채운다. 그대로 되돌리면 승인하지 않은 목표가 생길 수 있으므로 순서를 지킨다.
1. **운영자 중지**: 7.3절 1~4를 끝내 HALTED와 다른 실행 0건을 확인한다. 보유는 그대로.
2. **워크플로 끄기**: Actions → update-portfolio → ⋯ → Disable workflow. 되돌리기 커밋의 push 트리거와 예약 실행이 구버전을 돌리지 못하게 막는다. 구버전은 `PDS_HALT`를 무시하므로 이 단계가 핵심이다.
3. **사전 점검**: Actions → rollback-check → Run workflow → `target_ref` = 되돌릴 커밋 SHA. 오늘 데이터로 그 코드를 현재 데이터 사본에 돌려 새로 잠길 달, 빈 달 채우기, 기존 달 변경을 보고한다(저장소에 쓰지 않음). 결론이 "수동 검토 필요"(종료 코드 2)면 멈춘다.
4. 점검 결과를 받아들일 수 있을 때만 `git revert` → PR → 병합(워크플로가 꺼져 있어 자동 실행 없음).
5. 워크플로를 다시 켤 때는 예약 시각(22:40 UTC = 07:40 KST; 매월 28~31일, 1~10일, 금요일)을 피해 켜고, 곧바로 Run workflow를 `commit` 해제(false)로 한 번 실행해 결과를 본다. 구버전은 `PDS_HALT`를 모르므로 켜는 순간부터 예약 실행이 실제로 잠그고 커밋한다.
6. 점검에서 빈 달 채우기가 나오면 워크플로를 끈 채 보유를 유지하고 그 달 처리를 따로 결정한다.
7. 새 코드로 다시 돌아올 때 `PDS_HALT`를 지운다.

로컬 점검: `python tools/rollback_check.py --ref <커밋>` (종료 코드 0 통과, 2 수동 검토, 1 점검 실패).

### 9.3 Fail-closed가 지켜 주는 것과 아닌 것
- 지켜 주는 것: 잘못된 데이터·계산으로 공식 목표가 바뀌는 것, 기존 잠금이 바뀌는 것.
- 지켜 주지 못하는 것: 원작자 공식값 자체의 오류(원작자와 같으면 잠긴다), 사람이 승인한 수동 해제의 판단 오류, 저장소 쓰기 권한자의 직접 수정.

---

## 10. 정기 점검과 설정

### 10.1 매월
| 시점(KST) | 할 일 |
|---|---|
| 월말 다음 날 아침 | ⏳ 대기 시작 확인 |
| 확정 아침 | ✅ 확인 → 리밸런싱 |
| 11일 아침 | 확정 안 됐으면 ⛔ 보류 → 보유 유지, 8장 검토 |
| 11일 점심 이후 | Actions → ops-audit 최신 실행 → 작업 요약의 ✅/⚠️/❔ 확인 |
| 아무 때나 | Settings → Applications에서 이 저장소에 쓰기 권한이 있는 앱 점검 |

### 10.2 최초 설정 체크리스트 (소유자)
2026-11-26 기준 상태다. 이후 변경은 [미확인]이므로 설정 후 `ops-audit`로 확인한다.

| # | 설정 | 위치 | 2026-11-26 |
|---|---|---|---|
| 1 | Telegram 시크릿 2개 + 시험(6장) | Settings → Secrets and variables → Actions | 미설정 |
| 2 | Actions 실패 이메일 | 계정 Settings → Notifications → Actions | 미확인 |
| 3 | Ruleset `main-protect`: Active, 기본 브랜치, Restrict deletions + Block force pushes, 우회 없음. PR 필수·상태 검사 필수는 켜지 않음(봇 커밋이 막힘) | Settings → Rules → Rulesets | 미설정 |
| 4 | 환경 `pds-manual-release`(8.2절) | Settings → Environments | 미설정 |
| 5 | Actions 기본 권한 "Read repository contents", PR 생성·승인 허용 끔, 허용 액션 제한, 포크 PR 승인 필요 | Settings → Actions → General | 미확인 |
| 6 | 임시 브랜치 삭제: `probe-token-permissions`, `reference-only-failsafe`, `fix-flag-scope` | Branches | 남아 있음 |
| 7 | `ops-audit` 수동 실행 → 모두 ✅ 확인 | Actions → ops-audit | – |
| 8 | 계정 2단계 인증, classic PAT 사용 안 함 | 계정 설정 | 미확인 |

5번 뒤에는 다음 운영 실행에서 봇 커밋이 정상인지 확인한다(① 9.5절 미확인 항목).

README 설치 3단계의 "Workflow permissions: Read and write"는 이 체크리스트와 다르다. 이 체크리스트(읽기 전용)를 따른다(③ 7장).

---

## 11. 기록 찾는 곳

| 알고 싶은 것 | 위치 |
|---|---|
| 지금 유효한 공식 목표 | 화면 Official, `docs/data/portfolio_latest.json` → `official` |
| 확정된 모든 달 | `docs/data/official_history.json` |
| 대기·불일치·보류·확정·해제 이력 | `docs/data/official_events.json` |
| 수동 해제 요청과 승인자 | `docs/data/manual_releases.json` |
| 최근 60회 실행 결과, 알림 대상, 중지 보고, 전달 결과 | `docs/data/run_status.json`(`recent`, `notify`, `halt_report`, `delivery`) |
| 원작자 Adaptive 값과 공개·수정 이력 | `docs/data/reference_adaptive.json` |
| 한 실행의 전체 기록(로그·결과·승인 정보) | Actions → 해당 실행 → Artifacts `pds-lite-run-<실행 ID>`(90일) |
| 실행 요약(원작자 대조, action, DEFENSIVE 달) | Actions → 해당 실행 → Summary |
| 권한 점검 결과 | Actions → ops-audit → Summary |
| 과거 어느 시점의 데이터 | git 이력(`docs/data` 봇 커밋 `data: <시각>`) |
