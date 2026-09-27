from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator

STANDARD = [40, 45, 50, 55, 59, 64]  # low E to high E
TECHNIQUES = ['normal', 'hammer', 'pull', 'slide', 'harmonic', 'mute', 'slap', 'percussion', 'bend', 'vibrato']


class Note(BaseModel):
    id: str
    midi: int = Field(ge=0, le=127)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    string: int = Field(default=0, ge=0, le=6)
    fret: int = Field(default=0, ge=0, le=24)
    velocity: int = Field(default=80, ge=1, le=127)
    confidence: float = Field(default=0.5, ge=0, le=1)
    technique: Literal['normal', 'hammer', 'pull', 'slide', 'harmonic', 'mute', 'slap', 'percussion', 'bend', 'vibrato'] = 'normal'
    reviewed: bool = False
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def duration_valid(self):
        if self.end <= self.start:
            raise ValueError('음표의 끝은 시작보다 뒤여야 합니다.')
        return self


class Bar(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    numerator: int = Field(default=4, ge=1, le=12)
    denominator: Literal[2, 4, 8, 16] = 4
    tempo: float = Field(default=90, ge=20, le=300)
    # Tracked beat start times inside the bar (seconds). Empty, or edited out of shape, means equal beats.
    beats: list[float] = Field(default_factory=list)

    @model_validator(mode='after')
    def order(self):
        if self.end <= self.start:
            raise ValueError('마디 끝은 시작보다 뒤여야 합니다.')
        return self


class CapoSegment(BaseModel):
    start: float = Field(ge=0)
    capo: int = Field(ge=0, le=12)


class Project(BaseModel):
    id: str
    title: str = '새 채보'
    url: str = ''
    video_id: str = ''
    source: str = 'youtube'
    status: str = 'queued'
    stage: str = '대기 중'
    progress: float = 0
    error: str = ''
    created_at: str = ''
    updated_at: str = ''
    revision: int = 0
    duration: float = 0
    analysis_seconds: float = 0
    tuning: list[int] = Field(default_factory=lambda: STANDARD.copy(), min_length=6, max_length=6)
    capo: int = Field(default=0, ge=0, le=12)
    capo_segments: list[CapoSegment] = Field(default_factory=list)
    tempo: float = Field(default=90, ge=20, le=300)
    notes: list[Note] = Field(default_factory=list)
    bars: list[Bar] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_tuning(self):
        if any(n < 24 or n > 84 for n in self.tuning):
            raise ValueError('튜닝 음높이는 MIDI 24~84 사이여야 합니다.')
        if len({n.id for n in self.notes}) != len(self.notes):
            raise ValueError('음표 ID가 중복되었습니다.')
        return self

    def capo_at(self, time: float) -> int:
        capo = self.capo
        for segment in sorted(self.capo_segments, key=lambda x: x.start):
            if segment.start <= time:
                capo = segment.capo
        return capo
