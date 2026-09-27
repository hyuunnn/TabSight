# 연구 검토와 적용 결정

조사일: 2026-09-26. 영상 기능 제거와 레퍼런스 악보 평가·Beat This! 적용(2026-09-27)을 반영했다. 논문의 성능, 공개 구현, 실제 실행 가능 여부를 구분했다. 논문 수치는 이 앱의 성능 수치가 아니다.

코드에 적용된 계산식과 임계값은 [알고리즘 상세](algorithms.md), 작업 실행·저장·파일 생성 과정은 [구조와 동작 과정](architecture.md), 이 앱으로 잰 수치는 [검증과 한계](validation.md)에 정리했다.

| 연구·도구 | 공개 자료 | 적용 상태 | 판단 |
|---|---|---|---|
| GAPS / High Resolution Guitar Transcription (2024) | [논문](https://arxiv.org/abs/2408.08653), [모델](https://huggingface.co/xavriley/midi-transcription-models), [코드](https://github.com/xavriley/piano_transcription_inference) | **실제 적용** | 다성 기타 onset/offset/frame/velocity CRNN. 공개 기타 전용 약 99MB 체크포인트를 MPS에서 실행했다. GuitarSet을 학습에 쓰지 않고 F1 88.1%(논문 표 4)로, 확인한 범위에서 이를 넘는 공개 모델은 없다. 줄·프렛은 별도 추정해야 한다. |
| Beat This! (ISMIR 2024) | [논문](https://arxiv.org/abs/2407.21658), [코드·가중치](https://github.com/CPJKU/beat_this) | **실제 적용 (2026-09-27)** | 비트와 마디 첫 박을 함께 추론한다. 코드·가중치 MIT이며 학습 데이터에 GuitarSet 반주가 들어 있다. 레퍼런스 악보 22곡에서 마디 첫 박 F1이 12.6%에서 60.9%로 올랐다. |
| MediaPipe Hand Landmarker | [공식 문서](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker) | **제거 (2026-09-27)** | 양손 21개 랜드마크. 현 접촉, 프렛, 카포를 자체 인식하는 모델은 아니다. 지판 좌표와 결합한 운지 근거가 정확도를 높이지 못해 제거했다. 측정 결과는 [알고리즘 문서](algorithms.md) 6절. |
| Basic Pitch (Spotify) | [공식 코드](https://github.com/spotify/basic-pitch) | 적용하지 않음 | GAPS 논문의 FrançoisLeduc 테스트에서 F1 66.1%로 GAPS(84.8%)보다 낮다. 교체 후보에서 뺐다. |
| MIDI-to-Tab (2024), Fretting-Transformer (2025) | [MIDI-to-Tab](https://arxiv.org/abs/2408.05024), [Fretting-Transformer](https://arxiv.org/abs/2506.14223) | 운지 방법 참고 | 대규모 타브로 학습한 음표→줄 모델. MIDI-to-Tab은 표준 튜닝만 다루고, Fretting-Transformer는 튜닝·카포를 입력으로 받는다. 공개 가중치를 확인하지 못해 현재는 곡 전체 비용 최적화(Viterbi)를 쓴다. |
| GuiFiR (2026) | [연구 페이지](https://ailab.kookmin.ac.kr/publications/2026-07-15-guifir-weakly-supervised-learning-of-han/), [데이터셋](https://github.com/kmu-ee-ailab/guifir_dataset) | 데이터·방법 참고 | 손가락과 지판 관계를 학습하는 방향이 적합하다. 즉시 배포 가능한 추론 체크포인트는 확인하지 못했다. 데이터는 비상업 연구 용도 조건을 별도로 검토해야 한다. |
| TART (2026) | [논문](https://arxiv.org/abs/2609.11904) | 구조·주법 분석 참고 | 음표 전사, CNN-BiLSTM 주법 인식, AudioFret 운지 생성 구성. 논문의 end-to-end Tab F1 54.08%는 해당 평가의 값이다. 1단계는 GAPS와 같은 구조를 잡음 증강으로 다시 학습해 잡음·잔향 조건에서 더 강하다. 파일 이름이 TART의 학습 데이터와 같은 체크포인트가 Hugging Face(`shamakg`)에 있으나, 라이선스 표기가 없고 논문에서 연결하지 않아 쓰지 않는다. |
| Noise2Fret (2026) | [공식 코드](https://github.com/RiccardoVib/Noise2Fret) | 후속 운지 모델 후보 | 코드와 문서의 weights 경로는 있으나 즉시 내려받아 실행할 체크포인트 파일은 확인하지 못했다. 표준 튜닝·카포 없는 곡만으로 학습했다. |

## 실제 파이프라인

YouTube/로컬 영상 → FFmpeg 16kHz 모노 → 설명의 튜닝·카포로 채운 채보 전 사용자 확인 → GAPS 음표 이벤트 → Beat This! 비트·첫 박 → 박자·마디선 Viterbi → 곡 전체 운지 Viterbi → 주법 후보 → 사용자 수정 → 박 단위 양자화(셋잇단·두 성부) → GP5/GP7.

GAPS는 `guitar-gaps-paper-version-12200_iterations.pth`를 실행한다. 피아노 기본 가중치나 비어 있는 HF wrapper state로 대체하지 않는다. `weights_only=True`로 읽고, numpy 메타데이터에 필요한 타입만 허용하며 구조는 `strict=True`로 검증한다.

20초 단위 추론에 좌우 2초 문맥을 추가하고, 담당 onset 구간으로 잘라 중복 음표를 제거한다. 기타 음역과 지나치게 짧은 음을 필터링한다. MPS에서 실행하고 torch 스레드는 4개로 제한했다.

영상은 원본 재생에만 쓰고 분석하지 않는다.

Beat This!는 `final0` 체크포인트를 `.data/models/beat-this/`에 받아 MPS에서 실행하고, `weights_only=True`로 읽는다. 선택 사항인 madmom DBN 후처리는 쓰지 않는다. madmom의 모델 파일이 CC BY-NC-SA이고, PyPI 판은 Python 3.10 이상에서 설치되지 않기 때문이다.

## 레퍼런스 악보로 확인한 것

정성하·Masaaki Kishibe의 GP 악보 22곡을 해당 YouTube 연주와 맞춰 처음으로 이 앱의 결과를 쟀다([검증](validation.md)). 음표 F1은 61.1%였다. 조건이 달라 논문 수치(GuitarSet 88.1%)와 직접 비교할 수는 없다. 운지를 곡 전체에서 고르게 바꾸자 악보 음 기준 줄 정확도가 87.2%에서 91.2%로 올랐다. 비트 모델과 박 단위 양자화로 마디 첫 박 F1은 12.6%에서 60.9%, 악보와 같은 박 안 위치에 적힌 음은 11.4%에서 96.7%가 되었다. 지금 가장 큰 손실은 음표 인식 단계다.

## 후속 개선 방향

1. **음표 인식**: 같은 저장소의 다른 GAPS 계열 체크포인트(`guitar-gaps.pth`, `guitar-fl.pth`)와 임계값을 평가셋으로 비교한다. 그다음 TART처럼 잡음·잔향 증강으로 fine-tuning한다. 이때 GuitarSet·Guitar-TECHS 같은 CC BY 데이터를 쓴다.
2. **운지**: 남은 오류는 대부분 높은 포지션의 멜로디를 낮은 포지션으로 고른 경우다. 튜닝·카포를 입력으로 받는 학습된 운지 모델(Fretting-Transformer 계열)의 점수를 Viterbi 비용에 더하는 방향이 있다. 공개 가중치가 없어 학습이 필요하고, 학습 데이터(DadaGP 등)의 이용 조건을 따로 검토해야 한다.
3. **박자**: 반 마디 밀린 마디선, 6/8을 3/4로 읽는 경우, 템포를 절반으로 잡는 경우가 남았다. 비트 모델에 16kHz 대신 22.05kHz 음성을 넣는 것부터 시험할 수 있다.
4. 영상을 다시 쓰려면 배포 가능한 기타 전용 지판·프렛·카포 모델이 필요하다. 손 landmark와 두 점 보정만으로는 어느 줄을 누르는지, 개방현인지 판별하지 못했다.
5. 카포·튜닝 가설별 오디오/비디오 적합도와 구간별 변화 검출. 설명과 연주 가능 음역만으로는 답이 유일하지 않다.
6. **평가셋 확장**: 지금은 두 연주자 22곡이고, 운지·첫 박 설정을 같은 곡으로 맞췄다. 다른 편곡자의 곡을 따로 떼어 두고 확인해야 한다.

## 악보 라이브러리

[alphaTab](https://alphatab.net/docs/) 1.8.4는 렌더링·커서·합성음·GP 읽기와 GP7 export에 사용한다. [PyGuitarPro](https://pyguitarpro.readthedocs.io/)는 GP5 쓰기에 사용한다. 라이선스는 패키지에 포함되어 있다. GAPS 저장소는 MIT로 표시되어 있고, Beat This!의 코드와 가중치도 MIT다. GuiFiR 자료를 앱에 포함하거나 학습하는 단계는 현재 구현에 포함하지 않았다.
