# 구조와 동작 과정

기준: 2026-09-27 구현. [README](../README.md) · [알고리즘](algorithms.md) · [개발 안내](development.md)

TabSight는 한 사람이 자신의 Mac에서 사용하는 웹앱이다. 브라우저는 편집과 재생을 담당하고, 로컬 Python 서버는 모델 추론·프로젝트 저장·파일 생성을 담당한다. 현재 계정, 클라우드 작업 큐, 여러 사용자를 위한 서비스 운영 기능은 없다.

## 1. 전체 구성

```mermaid
flowchart TD
    U[브라우저: React] -->|HTTP /api| A[FastAPI: 127.0.0.1:8787]
    A --> D[(SQLite: 프로젝트와 편집 이력)]
    A --> Q[분석 큐: 한 번에 한 작업]
    Q --> M[yt-dlp / FFmpeg: 영상과 16kHz 음성]
    M --> G[GAPS: 음표 추론]
    G --> B[비트 / 튜닝 / 카포 / 운지]
    B --> F[주법 후보]
    F --> D
    A --> E[PyGuitarPro: GP5 생성]
    E --> N[Node와 alphaTab: GP7 변환]
    E --> S[브라우저 alphaTab: 악보와 합성음]
    A --> P[브라우저 video / audio: 원본 재생]
```

왼쪽 영상은 YouTube iframe이 아니라 내려받은 영상을 재생하는 HTML `video`다. 영상은 재생에만 쓰고 채보를 위해 분석하지 않는다. 음성 파일만 가져오면 `audio`를 사용한다. 원본 YouTube 페이지는 별도 링크로 열 수 있다.

배포용 화면은 Vite로 `dist/`에 빌드하고 FastAPI가 제공한다. 개발 중에는 Vite의 5173 포트에서 화면을 열며 `/api` 요청을 8787로 전달한다. Python 서버가 시작할 때 `dist/`가 존재해야 배포용 화면이 연결된다.

## 2. URL 입력부터 초안 완성까지

1. **프로젝트 생성**: 서버가 YouTube 주소에서 11자리 영상 ID를 검증하고 프로젝트를 `queued` 상태로 저장한다. 브라우저에 프로젝트 ID를 돌려주고 분석 큐에 넣는다.
2. **미디어 준비**: yt-dlp로 영상을 내려받고 제목·채널·설명을 보관한다. FFmpeg로 16kHz 모노 WAV와 미리보기 이미지를 만든다.
3. **음표 추론**: GAPS 체크포인트를 처음 필요할 때 내려받고 메모리에 로드한다. 음성을 나누어 음높이, 시작·끝, 세기를 추론한다.
4. **악보 골격 생성**: 비트를 추적해 4/4 마디 초안을 만들고, 설명과 연주 가능 음역으로 튜닝·카포를 추정한다.
5. **운지와 주법 정리**: 음높이를 유지한 채 연주 가능한 줄·프렛을 고르고, 신호·운지 규칙으로 주법 후보를 표시한다.
6. **완료**: 결과를 저장하고 `ready`로 바꾼다. 브라우저가 미리보기 GP5를 요청해 alphaTab으로 표시한다. `ready`는 초안 생성 완료를 뜻하며 정답 검증 완료를 뜻하지 않는다.

화면에서는 이 과정을 영상 준비, 음표 추론, 운지 정리 세 단계로 묶어 진행률과 함께 보여 준다.

![분석 중 화면. 음표 추론 단계에서 진행률 43%가 표시되어 있다.](images/analyzing.png)

```mermaid
sequenceDiagram
    participant B as 브라우저
    participant A as FastAPI
    participant W as 분석 작업
    participant D as SQLite
    B->>A: POST /api/projects {url}
    A->>D: queued 프로젝트 저장
    A->>W: 작업 제출
    A-->>B: 프로젝트 ID
    loop 분석 중 약 1.2초마다
        W->>D: 단계와 진행률 저장
        B->>A: GET /api/projects/{id}
        A-->>B: 현재 상태
    end
    W->>D: 음표·마디 결과, ready 저장
    B->>A: GET /score/gp5?preview=true
    A-->>B: 렌더링용 GP5
    B->>B: alphaTab 렌더링
```

분석 작업은 `ThreadPoolExecutor(max_workers=1)`에서 순차 실행한다. 진행률은 단계별 고정 비중이며 남은 시간 예측값이 아니다. `analysis_seconds`는 작업 실행 시작부터 완료까지의 시간으로 큐에서 기다린 시간은 제외한다.

## 3. 코드의 역할

| 파일 | 책임 |
|---|---|
| [src/App.tsx](../src/App.tsx) | 프로젝트 선택, 분석 상태 조회, 원본 재생, A/B 반복, 저장, 실행 취소 |
| [src/Score.tsx](../src/Score.tsx) | alphaTab 연결, 악보 렌더링, 합성음, 시간↔tick 변환, 음표 클릭 |
| [src/Editors.tsx](../src/Editors.tsx) | 음표·마디·튜닝·카포 편집 UI |
| [src/types.ts](../src/types.ts) | 프런트엔드 데이터 타입 |
| [server/app.py](../server/app.py) | HTTP API, 요청 검증, 가져오기·내보내기 연결 |
| [server/pipeline.py](../server/pipeline.py) | 작업 큐, 미디어 준비, GAPS 실행, 단계 연결, 취소·오류 처리 |
| [server/music.py](../server/music.py) | 튜닝·카포 후보, 운지 탐색, 마디, 주법 규칙 |
| [server/models.py](../server/models.py) | 프로젝트·음표·마디의 데이터 형식과 기본 검증 |
| [server/store.py](../server/store.py) | SQLite 저장, revision과 편집 이력, 데이터 경로 |
| [server/export.py](../server/export.py) | 시간 양자화, 트랙·타이·주법 구성, GP5 쓰기 |
| [scripts/score-bridge.mjs](../scripts/score-bridge.mjs) | alphaTab의 GP 읽기와 GP7 변환을 Node에서 실행 |

## 4. 데이터가 의미하는 것

프로젝트의 기준 데이터는 GP 파일이 아닌 `Project` JSON이다. GP5/GP와 화면 악보는 이 데이터에서 생성한다.

| 항목 | 의미와 단위 |
|---|---|
| `notes[].midi` | 실제 울리는 음높이. 일반 음에서는 튜닝+카포+프렛과 일치해야 함 |
| `notes[].start / end` | 원본 미디어 시작부터의 초. `end > start` |
| `notes[].string` | 1번=가장 가는 줄, 6번=가장 굵은 줄. 0은 운지 미정 또는 타격음 |
| `notes[].fret` | 카포 기준 상대 프렛 0~24. 하모닉스에서는 터치 프렛 |
| `tuning` | **6번→1번 줄** 순서의 개방현 MIDI. Standard는 `[40,45,50,55,59,64]` |
| `capo / capo_segments` | 기본 카포 0~12와 특정 시각부터 적용할 카포. 해당 시각 이전의 가장 최근 구간 사용 |
| `bars` | 각 마디의 실제 시작·끝 초, 박자, 템포 |
| `confidence / reviewed` | 검토 우선순위를 위한 값 / 사용자가 검토했는지 여부 |
| `evidence` | `audio`, `technique-candidate`, `manual` 등 추정·수정 근거. 이전 버전 분석에는 `vision`이 남아 있을 수 있음 |
| `metadata / metrics` | 원본 설명과 설정 출처 / 분석 실행 시의 집계 |
| `revision` | 편집 충돌 방지와 화면 갱신에 쓰는 버전 번호 |

일반 음의 관계는 `midi = tuning[6 - string] + capo_at(start) + fret`다. 같은 음높이의 여러 운지를 탐색하는 과정은 [알고리즘 문서](algorithms.md)에 설명한다.

`metrics`와 `analysis-report.json`은 분석 완료 당시의 집계다. 이후 음표를 수정하거나 튜닝·카포를 바꿔도 집계를 다시 계산하지 않는다. 현재 편집 결과를 확인할 때는 `notes`, `bars`, 설정값을 기준으로 한다.

## 5. 편집·저장·실행 취소

음표·마디 입력란은 로컬 초안을 편집한다. 저장 버튼을 누르면 전체 프로젝트 JSON을 `PUT`으로 보내며 서버가 음높이/운지 일치와 마디 중복 등을 검증한다. 튜닝·카포 변경은 별도 API에서 운지를 재계산한 뒤 저장한다.

서버는 수정 전 JSON을 SQLite `history`에 넣고 `revision`을 올린다. 일반 저장 요청의 revision이 현재 값과 다르면 HTTP 409로 거절하므로 오래된 화면이 최신 변경을 덮어쓰지 않는다. 분석 진행률 저장은 편집 이력을 만들거나 revision을 올리지 않는다.

화면의 실행 취소/다시 실행은 브라우저 메모리에 둔 프로젝트 스냅샷으로 동작한다. 취소도 이전 스냅샷을 새 변경으로 저장한다. SQLite에 이력이 남아 있어도 **새로고침 후 실행 취소 목록을 복원하는 UI는 구현되어 있지 않다.** 저장된 최신 프로젝트는 유지된다.

카포·튜닝 재계산은 기존 MIDI를 유지한다. 화면은 적용 전에 새 설정의 음역을 벗어나는 음의 수와 원인을 보여 주고, 적용하면 그 음들을 운지 미정으로 남긴다. 음표 편집기에서 프렛을 직접 바꾸는 작업은 해당 음의 MIDI도 바꾸므로 목적이 다르다.

## 6. 영상과 악보의 동기화

GP 악보는 tick, 원본 영상은 초를 사용한다. [Score.tsx](../src/Score.tsx)는 각 마디 안에서 다음처럼 선형 변환한다.

```text
ratio = (영상 시간 - 마디 시작 초) / (마디 끝 초 - 마디 시작 초)
tick  = alphaTab 마디 시작 tick + ratio × alphaTab 마디 길이 tick
```

역변환으로 합성음의 위치를 원본 시간으로 바꾼다. 원음 모드에서는 영상의 시간 갱신이 악보 커서를 움직인다. 합성음 모드에서는 alphaTab이 재생을 주도하고 영상은 일시정지한 채 해당 위치로 탐색된다. 두 오디오를 동시에 재생하는 방식은 아니다.

원음 모드에서는 alphaTab의 합성기가 일시정지 상태이므로 내장 재생 강조·스크롤만으로는 충분하지 않다. `tickCache.findBeat`와 렌더러의 마디 좌표를 사용해 두 재생 모드에 공통으로 음표 강조와 자동 넘김을 적용한다. 파란 배경은 현재 마디, 진한 세로선은 재생 위치이며 상단에 마디·박을 표시한다. 악보 줄이 바뀔 때 `.score-scroll` 내부만 이동한다. 자동 넘김을 끌 수 있고, `현재 위치로` 버튼은 현재 줄로 즉시 복귀한다.

![원음 재생을 멈춘 화면. 영상은 0:06이고 악보에는 3마디 3박 위치에 파란 마디 배경과 재생선이 표시되어 있다.](images/workspace.png)

alphaTab의 재생선은 CSS transform으로 크기가 조정되므로 `width: 2px !important`처럼 너비를 강제하면 화면에서는 선이 거의 사라진다. 라이브러리가 계산한 너비를 유지하고 색상만 지정한다. [공식 커서 스타일 문서](https://docs.alphatab.net/docs/guides/styling-player)를 참고했다. 창 크기가 바뀌면 악보 폭 500px 미만에서는 한 줄에 한 마디, 그 이상에서는 두 마디로 재배치하며 재생 위치를 복원한다.

악보 클릭 시 해당 마디의 시간을 계산하고 같은 줄에서 시작 시간이 가까운 원본 음표를 찾는다. 현재 허용 범위는 0.3초다. 렌더링된 타이 조각이 원본 음표 하나와 완전히 일대일로 대응하지는 않는다.

마디 템포·박자를 수정하면 마디 경계를 다시 계산한다. 이때 음표의 원본 시작·끝 초는 그대로 유지한다. GP 출력의 리듬 양자화와 정수 BPM 때문에 원본과 출력 파일 길이가 조금 달라질 수 있다.

## 7. 저장 위치

기본 위치는 프로젝트 루트의 `.data/`다. 서버 시작 전에 `TABSIGHT_DATA_DIR`를 설정하면 바꿀 수 있다. 입력 종류나 분석 성공 여부에 따라 파일 일부만 존재할 수 있다.

```text
.data/
├── tabsight.sqlite3             # 프로젝트 JSON과 revision별 편집 이력
├── models/
│   └── gaps/                   # GAPS 체크포인트와 다운로드 메타데이터
└── projects/<project-id>/
    ├── source.mp4 / audio.wav / poster.jpg
    ├── source.info.json        # YouTube 다운로드 정보
    ├── import.<확장자>          # 가져온 원본; MP4는 source.mp4로 이동
    └── analysis-report.json    # 완료 시 실행시간과 집계
```

이전 버전으로 분석한 프로젝트 폴더에는 `audio-only.json`, `vision-result.json`, `vision.log`가, `models/`에는 `hand_landmarker.task`가 남아 있을 수 있다. 현재 앱은 이 파일들을 읽지 않는다.

편집된 최신 음표는 SQLite 프로젝트에 저장된다. 위 분석 산출물을 모두 최신 편집본으로 덮어쓰지는 않는다. `test-results/`는 개발 중 검사 결과이며 앱 실행의 필수 입력은 아니다.

백업은 서버를 종료한 뒤 `.data/` 전체를 복사한다. SQLite가 WAL을 사용하므로 실행 중 `.sqlite3` 한 파일만 복사하는 방식은 피한다. JSON 내보내기를 앱에 다시 불러오는 기능은 아직 없다.

## 8. API 요약

서버 실행 후 [자동 생성 API 문서](http://127.0.0.1:8787/api/docs)에서 정확한 요청 스키마를 볼 수 있다. 아래의 `{id}`는 프로젝트 ID다.

| 메서드·경로 | 동작 |
|---|---|
| `GET /api/health` | 서버 상태, 튜닝 프리셋, GAPS 파일 캐시 여부 |
| `GET /api/projects` | 프로젝트 요약 목록 |
| `POST /api/projects` | `{url}`로 분석 작업 생성 |
| `GET /api/projects/{id}` | 전체 프로젝트 조회 |
| `PUT /api/projects/{id}` | revision을 포함한 프로젝트 편집 저장 |
| `DELETE /api/projects/{id}` | 프로젝트·편집 이력·`projects/<id>/` 폴더 삭제. 분석 중이면 409 |
| `POST /api/projects/{id}/cancel` | 취소 요청 |
| `POST /api/projects/{id}/retry` | 다시 분석 |
| `POST /api/projects/{id}/revoice` | 튜닝·카포·구간별 카포 변경과 운지 재계산. 배치할 수 없는 음은 `allow_unplayable`이 참이면 운지 미정으로 남기고, 아니면 원인과 함께 422 |
| `GET /api/projects/{id}/media/{kind}` | `video`, `audio`, `poster` 제공 |
| `GET /api/projects/{id}/score/{fmt}` | `gp5`, `gp`, `json` 생성 |
| `POST /api/import` | multipart `file`로 악보·미디어 가져오기, 최대 1GB |

`preview=true`인 GP 요청은 미정 운지를 제외하고 일부 충돌 검사를 생략하여 편집 중 화면을 표시한다. 따라서 화면에 악보가 보이는 것만으로 실제 내보내기 검증을 통과했다고 볼 수 없다.

## 9. 실패·재시작·가져오기

- 정상 상태는 `queued → processing → ready`, 중단은 `cancelled`, 실패는 `error`다. 취소는 다운로드 훅·추론 청크 경계 등 다음 확인 지점에서 적용되므로 즉시 끝나지 않을 수 있다.
- MPS에서 추론 중 `RuntimeError` 또는 `NotImplementedError`가 발생하면 같은 모델을 CPU로 옮겨 해당 청크를 다시 계산한다. 모든 종류의 실패를 복구한다는 뜻은 아니다.
- 서버를 재시작할 때 남아 있는 `queued`·`processing` 작업은 오류 상태로 전환한다. 다시 분석하면 남은 미디어를 활용하지만 **추론 청크부터 이어서 재개하지 않고 다시 계산**한다. 재분석은 기존 초안과 편집 내용을 대체할 수 있다.
- GP/GP5/GP4/GP3 가져오기는 모델 추론 없이 기존 악보를 읽는다. 첫 기타 트랙을 편집 모델로 변환하며 여러 트랙·반복 구조를 완전히 보존하는 왕복 편집기는 아니다. 원본과 AI 결과를 구분해 표시한다.
- 영상·음성 가져오기는 파일 변환 후 분석 큐에 넣는다. 변환 일부는 가져오기 HTTP 요청 안에서 실행되므로 큰 파일에서는 응답이 늦을 수 있다.
