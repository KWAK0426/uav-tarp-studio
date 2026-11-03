# GENERAL ELC 범위·제한

이 에디션은 **MX Dual native ELC 반사도 영상 산출까지만** 제공합니다. 실제 실행·검증 사실은 [VALIDATION.md](../VALIDATION.md)의 에디션·환경·항목을 확인합니다. 이 문서는 통과 선언이 아닙니다.

## 지원 경계

| 항목 | 계약·제한 |
|---|---|
| 환경 | Windows x64 소스+전용 Conda 환경. base 수정 없음 |
| 런타임 | 기본 `%LOCALAPPDATA%\ELCStudioShare\runtime-20261026`. 이번 `environment.yml`은 ExifTool 13.07 이상 요구 |
| 날짜·세션 | 실제 유효 ISO 날짜의 연도 제한 없음, 안전한 세션명. 날짜·회차 조작 금지 |
| 센서 | 실제 파장 기반 MX Dual native UInt16 10밴드. RedEdge-P·임의 센서 지원 아님 |
| Raw·대상 | 평면/임의 하위 폴더 전체 스캔, 중복·부분 밴드 차단. `shot` CSV로 사용자 등록 |
| 후보·ROI | native 후보 부분집합과 제한된 지연 미리보기. 편집·저장·추적은 승인 아님 |
| GPS | 태양각 기록용 실제 GPS만 선택 가능. 없어도 EXIF 시간·실측 Ed로 처리하며 태양각은 빈 값/`unavailable` |
| Ed | 최근접 실측값 5초 이하, 보간·평균·외삽 없음. 실측 범위 밖·시간대 불일치 차단 |
| 승인 | ROI 승인·계수 적용 승인 분리. `awaiting_review`에서 정지. 입력·대상·교정 변경 시 서명/승인 무효화 |
| 최종 산출물 | native 밴드별 Float32 ELC TIFF, 10밴드 계수 CSV, ROI·타프 독립 검증 HTML |
| 제외 범위 | 기하·AOI·BRDF 탭/CLI·추가 분석 버튼, Photos·모자이크·DEM 입력 및 정사화·모자이크 생성 |

최소 2촬영, 밴드별 반사도가 서로 다른 유효 타프 2개를 요구합니다. 기준 적합 잔차는 독립 정확도가 아닙니다. 정합은 ROI 전달용이며 최종 영상의 native 격자를 바꾸지 않습니다. 빈 실측 양식·타인 교정·가짜 좌표·자동 승인으로 차단을 우회하지 않습니다.

## ExifTool: 한글 경로와 긴 경로

1. 이번 환경은 `exiftool>=13.07`을 요구합니다. Windows 장경로 지원의 `WindowsLongPath` 기본 적용은 13.07 이상입니다. [ExifTool API 문서](https://exiftool.org/ExifTool.html#WindowsLongPath)
2. 기존 PC ExifTool 12.52에서는 240자 이상 경로의 메타데이터 처리가 실패했습니다. 이는 해당 구버전 실행의 관찰이며, 모든 Windows 도구의 보편적인 240자 제한이라는 뜻은 아닙니다.
3. Windows 사전 검사 `require_paths_supported`는 실제 절대 경로가 240자 이상이고 ExifTool이 13.07 미만이면 **래스터 쓰기 전에 중단**합니다. 새 전용 환경을 설치하거나 입력·결과 루트를 짧게 지정합니다. 원본을 임의 이동하거나 검사를 끄지 않습니다.
4. 기존 환경은 `00_INSTALL.bat`만 재실행해 자동 갱신되지 않습니다. 실제 버전을 확인하고 필요하면 `ELC_STUDIO_ENV`에 새 전용 환경 위치를 지정합니다. Share와 공유하는 기존 환경·base를 묵시적으로 변경하지 않습니다.
5. 한글 경로의 메타데이터 복사는 UTF-8 인자 파일 방식(`-charset filename=UTF8 -@ -`)과 제한시간을 사용합니다. Unicode 인코딩 지원과 장경로 지원은 별개입니다. [공식 Windows Unicode 파일명 안내](https://exiftool.org/exiftool_pod2.html#WINDOWS-UNICODE-FILE-NAMES)

구버전 도구의 짧은 경로 전체 흐름 재검사와 새 런타임 검증을 구분합니다. **새 환경의 전체 E2E 통과는 아직 선언하지 않습니다.** 진행·실행 결과는 검증 담당자의 `VALIDATION.md`만을 기준으로 합니다.

## 운영·외부 작업

1. ExifTool 누락·DLL 오류·시간초과·경로 차단은 메타데이터 실패입니다. `.local/doctor.json`과 로그를 확인하고 시각·파장·방사계수를 추정하지 않습니다.
2. 설치·해시·합성·빈 프로젝트 GUI 확인은 실자료 정확도 검증이 아닙니다. 처리 완료도 새 현장의 과학적 합격을 보장하지 않습니다.
3. 앱 내부 AI 로그인은 필요 없습니다. 외부 Codex는 로컬 소스 협업이며 자격 증명·실자료·과거 교정은 미포함입니다.
4. 나중에 외부 Metashape에서 ELC TIFF를 재연결하는 작업은 가능하지만, 소프트웨어·라이선스·정사화·모자이크는 이 패키지의 기능이나 산출물이 아닙니다.
5. 폴더 이동 후 절대 경로·서명·승인을 재확인합니다. 원본·완료 결과·MIT 공지·vendor 출처를 보존하며 해시 불일치를 숨기지 않습니다.
