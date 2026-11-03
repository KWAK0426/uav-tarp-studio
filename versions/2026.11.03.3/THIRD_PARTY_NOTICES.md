# 외부 소프트웨어와 자료

## MicaSense SDK

`vendor/micasense`는 기존 설치에서 사용하던 공식 SDK 소스 스냅샷입니다.

- 원본: https://github.com/micasense/imageprocessing
- commit: `3a903861b2f38c1717a5b6d918a2d9196db507ae`
- 설치 메타데이터 version: `0.0.1` (최신 릴리스라는 의미 아님)
- 라이선스: MIT, `vendor/MICASENSE_LICENSE.txt`와 각 소스 저작권 고지 유지
- 원본 소스 SHA256: `SOURCE_PROVENANCE.json`

## 별도로 설치하는 프로그램

Conda-forge 패키지와 PyExifTool은 `environment.yml`에 따라 받는 PC에서 설치합니다.
그 바이너리와 라이선스를 이 ZIP 안에 재포장하지 않았습니다. 각 패키지의 라이선스는 해당 배포를 따릅니다.
Miniforge 설치 프로그램도 ZIP에 포함하지 않습니다.
Metashape와 Codex는 포함하지 않으며, 사용 권한과 로그인은 수신자가 별도로 갖추어야 합니다.

## 연구 코드·데이터

기존 스튜디오 연구 소스는 사용자의 요청으로 이 전달용 사본에 포함했습니다. 전체 프로젝트를 새 MIT 라이선스로 재허가한 것이 아닙니다.
실제 원영상, 현장 ROI/승인 기록, 실측 타프·센서 계수, 계정·API 키, 기존 결과물은 포함하지 않습니다.
외부 공개 배포나 기관 외 재배포 전에는 연구 코드와 원자료의 권한을 별도로 확인하세요.
