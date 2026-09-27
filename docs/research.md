# 연구 검토와 적용 결정

조사일: 2026-09-26. 영상 기능 제거(2026-09-27)와 악보 싱크 추가(2026-09-28)를 반영했다. 논문의 성능, 공개 구현, 실제 실행 가능 여부를 구분했다. 논문 수치는 이 앱의 성능 수치가 아니다.

코드에 적용된 계산식과 임계값은 [알고리즘 상세](algorithms.md), 작업 실행·저장·파일 생성 과정은 [구조와 동작 과정](architecture.md)에 정리했다.

| 연구·도구 | 공개 자료 | 적용 상태 | 판단 |
|---|---|---|---|
| GAPS / High Resolution Guitar Transcription (2024) | [논문](https://arxiv.org/abs/2408.08653), [모델](https://huggingface.co/xavriley/midi-transcription-models), [코드](https://github.com/xavriley/piano_transcription_inference) | **실제 적용** | 다성 기타 onset/offset/frame/velocity CRNN. 공개 기타 전용 약 99MB 체크포인트를 MPS에서 실행했다. 줄·프렛은 별도 추정해야 한다. |
| MediaPipe Hand Landmarker | [공식 문서](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker) | **제거 (2026-09-27)** | 양손 21개 랜드마크. 현 접촉, 프렛, 카포를 자체 인식하는 모델은 아니다. 지판 좌표와 결합한 운지 근거가 정확도를 높이지 못해 제거했다. 측정 결과는 [알고리즘 문서](algorithms.md) 6절. |
| Basic Pitch (Spotify) | [공식 코드](https://github.com/spotify/basic-pitch) | 후속 오디오 비교 후보 | 가벼운 다성 음표 추론의 대안. 현재 GAPS보다 좋은 결과를 확인한 상태는 아니다. |
| GuiFiR (2026) | [연구 페이지](https://ailab.kookmin.ac.kr/publications/2026-07-15-guifir-weakly-supervised-learning-of-han/), [데이터셋](https://github.com/kmu-ee-ailab/guifir_dataset) | 데이터·방법 참고 | 손가락과 지판 관계를 학습하는 방향이 적합하다. 즉시 배포 가능한 추론 체크포인트는 확인하지 못했다. 데이터는 비상업 연구 용도 조건을 별도로 검토해야 한다. |
| TART (2026) | [논문](https://arxiv.org/abs/2609.11904) | 구조·주법 분석 참고 | 음표 전사, CNN-BiLSTM 주법 인식, AudioFret 운지 생성 구성. 논문의 end-to-end Tab F1 54.08%는 해당 평가의 값이다. 모든 가중치가 확인되지 않아 앱에서 주법 모델을 실행했다고 표시하지 않았다. |
| Noise2Fret (2026) | [공식 코드](https://github.com/RiccardoVib/Noise2Fret) | 후속 운지 모델 후보 | 코드와 문서의 weights 경로는 있으나 즉시 내려받아 실행할 체크포인트 파일은 확인하지 못했다. |

## 실제 파이프라인

YouTube/로컬 영상 → FFmpeg 16kHz 모노 → 설명의 튜닝·카포로 채운 채보 전 사용자 확인 → GAPS 음표 이벤트 → 비트·마디 초안 → 동시 음표의 현 중복을 제한한 beam search → 주법 후보 → 사용자 수정 → GP5/GP7.

사람이 만든 악보를 올리면 확인 단계 다음이 바뀐다: alphaTab으로 반복을 펼친 연주 순서 → 악보·음성 크로마 → 반음 이동·템포 탐색 → subsequence DTW → `[tick, 초]` 고정점. 오디오와 악보를 맞추는 크로마 DTW는 음악 정보 검색에서 널리 쓰는 방법이며, 특정 논문의 모델을 실행하는 것은 아니다.

GAPS는 `guitar-gaps-paper-version-12200_iterations.pth`를 실행한다. 피아노 기본 가중치나 비어 있는 HF wrapper state로 대체하지 않는다. `weights_only=True`로 읽고, numpy 메타데이터에 필요한 타입만 허용하며 구조는 `strict=True`로 검증한다.

20초 단위 추론에 좌우 2초 문맥을 추가하고, 담당 onset 구간으로 잘라 중복 음표를 제거한다. 기타 음역과 지나치게 짧은 음을 필터링한다. MPS에서 실행하고 torch 스레드는 4개로 제한했다.

영상은 원본 재생에만 쓰고 분석하지 않는다.

## 후속 개선 방향

1. 영상을 다시 쓰려면 배포 가능한 기타 전용 지판·프렛·카포 모델이 필요하다. 손 landmark와 두 점 보정만으로는 어느 줄을 누르는지, 개방현인지 판별하지 못했다.
2. 카포·튜닝 가설별 오디오/비디오 적합도와 구간별 변화 검출. 설명과 연주 가능 음역만으로는 답이 유일하지 않다.
3. 실제 악보의 voice·tuplet·pickup 구조로 리듬 정리. 현재 정밀 시간 양자화는 복잡한 타이와 짧은 음가를 만든다.
4. 연주와 정확히 대응하는 정답 악보를 수작업으로 확인한 평가 세트로 여러 곡을 평가.

## 악보 라이브러리

[alphaTab](https://alphatab.net/docs/) 1.8.4는 렌더링·커서·합성음·GP 읽기와 GP7 export에 사용한다. [PyGuitarPro](https://pyguitarpro.readthedocs.io/)는 GP5 쓰기에 사용한다. 라이선스는 패키지에 포함되어 있다. GAPS 저장소는 MIT로 표시되어 있으며, GuiFiR 자료를 앱에 포함하거나 학습하는 단계는 현재 구현에 포함하지 않았다.
