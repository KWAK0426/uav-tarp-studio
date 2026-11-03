# 방사보정·센서 간 교정·Ed 정규화 통합 처리

2026-10-24. 현재 작업실의 04 단계에 추가한 선택 실행 경로이다.
기존 단계별 실행은 유지한다. 이 기능 추가 과정에서 실제 5·6회차를 재처리하지 않았다.

## 사용 순서

1. `01_START_STUDIO.bat`으로 업데이트된 작업실을 연다. 이미 열린 창은 작업이 없을 때 다시 연다.
2. 04의 `통합 처리 설정`에서 실제 해당 회차의 원본·GPS, 파장별 gain CSV, 실측 10밴드 Ed CSV와 기준영상을 확인한다.
3. 04의 `촬영시간·태양각 CSV 생성`으로 유효 GPS 영상의 원본 EXIF 시각을 검증·저장한다. 통합 검사는 이 서명된 CSV를 재사용하며 별도 시각을 추정하지 않는다.
4. `중간 센서교정 radiance 저장`을 선택한다. 기본 OFF. ON은 새로운 중간 파일을 추가로 저장하며, OFF여도 기존 파일을 삭제하지 않는다.
5. `입력·용량 검사`를 실행한다. 실행 중에는 같은 백그라운드 슬롯을 사용하므로 다른 대용량 단계와 중복 실행하지 않는다.
6. 검사에 통과한 설정으로 `통합 처리 실행`을 누르고 수량·중간 저장·권장 용량·새 폴더를 확인한다.
7. 최종 검증 후 `완료 결과 → ELC 입력 연결`을 선택하면 05의 보정 영상 입력이 해당 통합 결과로 연결된다. 자동 연결·ROI 승인·ELC 계수 승인은 하지 않는다. 계수 적용 기준 승인은 false로 초기화한다.
8. 단계별 경로로 돌아가려면 `기존 단계별 입력으로 복귀`를 선택한다. 연결을 해제해도 통합 결과는 보존된다.

## 실행 전 검사

- GPS 유효 촬영번호의 실제 원본을 재귀적으로 찾는다. 여러 SET를 이동·복사하지 않고 읽는다.
- 같은 촬영번호·native 밴드 파일이 두 위치에 있으면 자동 선택하지 않고 중단한다. 10밴드 누락도 중단한다.
- 실제 `CentralWavelength`로 파장순 gain과 Ed 열을 연결한다. native 접미 번호를 파장순 밴드 번호로 간주하지 않는다.
- 원본 UInt16 단일밴드, 노출·ISO·방사보정 계수·비네팅 메타데이터를 확인한다.
- 검증된 촬영시간 CSV의 날짜·회차·GPS·원본 서명을 재확인한다.
- 해당 날짜·회차의 최근접 실측 Ed를 사용한다. 허용차는 0~5초이며 경계 5초를 포함한다. 보간·평균·외삽은 하지 않는다.
- 실측 Ed 기록 시간 범위 밖의 촬영은 5초 이내여도 허용하지 않는다. 각 밴드의 Ed는 유한한 양수여야 한다.
- 기준영상도 동일한 처리 목록과 시간·Ed 검사를 통과해야 한다.
- 검사 후 기준영상·Ed·gain·시간 CSV·저장 옵션·입력·코드가 바뀌면 기존 계획으로 실행하지 않고 재검사를 요구한다.
- 이미 보정된 radiance를 원본으로 재투입하지 않는다. 통합 입력은 DN 원본이며 sensor gain은 한 번만 적용한다.

## 계산과 산출물

`L_norm(t,b) = [radiometric(DN(t,b)) × sensor_gain(b)] × Ed(t0,b) / Ed(t,b)`

기존 방사보정 계산부를 공유한다. 센서 교정 후 Float32 배열에 Ed 비율을 적용하고 최종 Float32 TIFF를 한 번 쓴다.
중간 저장 ON이면 Ed를 곱하기 직전의 센서교정 radiance도 별도로 쓴다.
클리핑·정합·정사투영·ELC 반사도 변환·BRDF 보정은 추가하지 않는다.
통합 실행은 기존 Ed 비율 적용의 물리적 가정을 바꾸거나 독립적으로 검증한 것이 아니다.

기본 폴더는 해당 회차의 `*_Integrated_Runs/<KST 실행시각_UUID>/`이다.
직접 지정한 결과 루트도 그 아래 새로운 실행 폴더를 만든다. 원본과 기존 Radiance/Ed/Ref/QC 폴더 안에는 통합 루트를 지정할 수 없다.

| 파일/폴더 | 내용 |
|---|---|
| `Ed_Normalized/` | 최종 센서교정·Ed 정규화 radiance. native 상대 경로 유지 |
| `Radiance/` | 선택 시에만 저장하는 센서교정 radiance |
| `plan.json` | 설정·원본 대응·gain·Ed 비율·입력 및 코드 SHA256·용량 계획 |
| `image_time_solar.csv` | 기존 검증 촬영시간 CSV의 해당 처리 목록 |
| `image_ed_matching.csv` | 영상별 실측 Ed 매칭 |
| `image_band_factors.csv` | 영상·실제 파장별 gain, Ed 값·기준값·시각·비율·입출력 대응 |
| `ed_input_snapshot.csv`, `gain_input_snapshot.csv` | 실제 사용한 입력표 사본 |
| `files.csv`, `QC/progress_state.json`, `state.json` | 파일별 결과·진행률·실행 상태 |
| `final_validation.json` | 전체 출력 헤더·provenance·원본 크기/수정시각 검증 |

정상 오류 종료 후에는 같은 서명 계획의 완료 파일을 태그 확인 후 건너뛸 수 있다.
강제 종료 후 남은 `RUNNING.lock`은 임의로 제거하지 않는다. 기존 작업을 확인하거나 새 검사로 별도 실행 폴더를 만든다.

## TIFF 메타데이터

원본의 비구조적 EXIF·GPS·카메라 XMP를 보존한다. 원본 촬영시각을 기준영상 시각으로 바꾸지 않는다.
다음 항목은 추가로 **GeoTIFF의 GDAL metadata**에 기록하고, 실제 저장 후 다시 읽어 검사한다.
일반 탐색기의 EXIF 속성창에는 사용자 정의 항목이 모두 보이지 않을 수 있다.

- `PROCESSING_STAGE`, `SOURCE_FILE`, `SOURCE_SIZE_BYTES`, `SOURCE_MTIME_NS`
- `CENTRAL_WAVELENGTH`, `SENSOR_INTERCALIBRATION_APPLIED`, `SENSOR_INTERCALIBRATION_SPECTRAL_BAND`, `SENSOR_INTERCALIBRATION_GAIN`
- `CAPTURE_TIME_KST`, `SOURCE_TIMEZONE`, `ED_NORMALIZATION_APPLIED`
- `ED_REFERENCE_SHOT`, `ED_REFERENCE_CAPTURE_TIME_KST`, `ED_REFERENCE_MATCH_TIME_KST`, `ED_MATCH_TIME_KST`, `ED_TIME_DELTA_SECONDS`, `ED_REFERENCE_TIME_DELTA_SECONDS`, `ED_MAX_TIME_DELTA_SECONDS`
- `ED_VALUE`, `ED_REFERENCE_VALUE`, `ED_NORMALIZATION_FACTOR`, `ED_MATCH_METHOD`
- `ED_SOURCE_FILE`, `ED_SOURCE_SHA256`, `SENSOR_GAIN_SOURCE_SHA256`, `INTEGRATED_RUN_ID`, `INTEGRATED_PIPELINE_SCHEMA`, `PROVENANCE_MANIFEST_SHA256`

Ed 값의 단위는 입력 Ed CSV와 같으며, 정규화 비율은 무차원이다.
중간 radiance에는 `ED_NORMALIZATION_APPLIED=false`, 적용 비율 `1.0`을 기록한다.
향후 적용할 Ed 비율은 `PLANNED_ED_NORMALIZATION_FACTOR`로 구분한다.
출력 메타데이터의 ED·gain 표시는 다른 소프트웨어가 자동으로 재보정하라는 지시가 아니다.

## 추가 용량

아래는 5·6회차를 **새 통합 실행으로 다시 만든다면 추가로 생기는** 비압축 Float32 래스터 본문이다.
기존 결과는 이미 디스크에 있으며 삭제·교체하지 않는다.

| 회차 | 중간 저장 OFF: 최종 Ed만 | 중간 저장 ON: 중간+최종 |
|---|---:|---:|
| 5회차, 7,830개 | 35.8 GiB | 71.7 GiB |
| 6회차, 6,960개 | 31.9 GiB | 63.7 GiB |
| 합계 | **67.7 GiB** | **135.4 GiB** |
| 20% 여유와 실행별 소량 기록 포함 권장 | **약 81.3 GiB** | **약 162.5 GiB** |

`파일 수 × 실제 폭 × 실제 높이 × 4 bytes × 저장 영상 종류 수`로 검사 때 계산한다.
압축이 적용되면 실제 파일은 작아질 수 있으나 Ed 적용 후 압축률을 측정한 수치는 아니다.
20%는 운영 여유율이지 정확도 기준이 아니다. ELC·정합·정사영상·모자이크 용량은 포함하지 않았다.

이미 완료된 5·6회차의 센서교정 radiance는 그대로 사용해 기존 Ed 단계만 추가할 수 있다.
통합 옵션을 시험하려고 이 두 회차를 다시 방사보정할 필요는 없다.

## 검증 범위

- 이전에 저장한 실제 10밴드 표본과 공유 방사보정 계산부의 전체 화소값이 정확히 일치.
- 소형 합성 원본 2촬영분×10밴드, 두 SET·한글 경로에서 중간 저장 OFF/ON을 각각 실제 CLI 처리.
- 두 모드의 최종 화소값이 단계별 Float32 저장·Ed 곱셈과 정확히 일치. 중간 저장 ON의 radiance도 정확히 일치.
- 모든 시험 출력의 실제 파장·gain·Ed·EXIF/XMP·계획 hash 재읽기 확인, ELC 입력 연결 및 승인 초기화 확인.
- 기준영상·저장 옵션·Ed 내용 변경 차단, 원본 중복 및 범위 밖 Ed 거부 확인.
- 실제 BAT 실행, 메인→04 연결, 중간 저장 옵션 저장, 1280×790 및 980×700 화면을 확인했다. GUI 저장 설정은 CP949 시작 환경에서도 실제 소형 TIFF 처리로 검증했다.
- 이 기능으로 실제 5·6회차 전체를 새로 돌리거나 통합/분리 방식의 대량 처리 시간을 비교 측정한 것은 아니다.
