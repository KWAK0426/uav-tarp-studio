# GENERAL ELC 배포 변경 범위

`ELC_Studio_General_20261026`은 **ELC 반사도 산출 전용**입니다. 원래 Share 패키지·원본 설치·실자료는 그대로 유지합니다. 아래는 배포 계약이며 검사 통과 목록이 아닙니다. 실행 사실은 [VALIDATION.md](VALIDATION.md)를 참조합니다.

| 항목 | 변경·유지 범위 |
|---|---|
| UI·CLI | ELC 5단계와 ELC 전용 runner만. 기하·AOI·BRDF 탭/버튼/CLI 제거 |
| 첫 설정 | 원본 스튜디오 테마 재사용, 항목별 한국어 설명·예시, 실제 저장 경로 미리보기, 입력 검증·기존 프로젝트 열기 |
| 진행·오류 안내 | 주요 동작 버튼의 동적 툴팁·차단 사유, 비활성 버튼 클릭 안내, 단계별 사유 요약, 원문 오류·다음 조치·실패 로그 경로. 안내만으로 처리·승인을 변경하지 않음 |
| 입력 | native UInt16 MX Dual 10밴드, `shot` CSV, 타프 실측값·gain·Ed. Photos·모자이크·DEM 입력 없음 |
| 선택 GPS | 실제 GPS를 태양각 기록에만 사용. GPS 없이 EXIF 시간·Ed 정규화 가능 |
| 프로젝트 | `작업루트/현장/YYYY-MM-DD/세션/project.json`, 절대 `general.project_root`. 실제 유효 ISO 날짜·안전한 세션 사용 |
| 대상·후보 | 평면/임의 폴더 전체 스캔, 중복·부분 밴드 차단, 명시적 대상 등록·native 후보 부분집합 저장 |
| 승인·처리 | ROI·화소 검토와 승인 → 방사·gain 1회·실측 Ed 최근접 5초 이하 → 계수 `awaiting_review` → 사용자 적용 승인 |
| 최종 산출물 | native 밴드별 Float32 ELC TIFF, 10밴드 계수 CSV, ROI·타프 독립 검증 HTML |
| 결과 보존 | 정상 결과 경로 키·영구 ROI와 프로젝트 직접 하위 `Easy_ELC_*`. 입력·대상·교정 변경 시 서명·승인 무효화 |
| 환경 | `environment.yml`에 `exiftool>=13.07` 요구. 기본 전용 위치 `%LOCALAPPDATA%\ELCStudioShare\runtime-20261026`, base 수정 없음 |
| 한글·긴 경로 | 메타데이터 복사에 UTF-8 인자 파일+제한시간. `require_paths_supported`가 구버전의 240자 이상 경로를 래스터 쓰기 전에 차단 |

## 운영·검증 경계

1. 실행 순서는 `00_INSTALL` → `02_CHECK` → 합성 검사 → `01_START`이며 추가 생성은 `03_NEW_PROJECT`입니다. 기존 전용 환경은 자동 갱신되지 않으므로 ExifTool 버전을 확인합니다.
2. 구버전 경로 차단 시 새 전용 환경을 설치하거나 짧은 루트를 사용합니다. 원본 이동·가짜 자료·승인 우회·자동 전체 처리를 하지 않습니다. 자세한 버전·공식 근거는 [범위·제한](docs/KNOWN_LIMITS.md)을 참조합니다.
3. 기존 PC ExifTool 12.52의 긴 경로 실패와 짧은 경로 E2E 재검사는 새 환경의 전체 검증이 아닙니다. **새 런타임 전체 통과는 아직 선언하지 않습니다.**
4. 앱 내부 AI 로그인은 불필요하며 외부 Codex는 로컬 소스 협업입니다. 실제 영상·자격 증명·과거 교정·승인 이력은 배포하지 않습니다.
5. 나중에 외부 Metashape에서 산출 ELC TIFF를 재연결할 수는 있지만, 정사화·모자이크 생성·해당 라이선스는 패키지 범위 밖입니다. 임의 센서 지원으로 확대하지 않습니다.

## 문서·출처

이 문서 작업은 지정된 안내 9개만 수정하며 소스·테스트·`VALIDATION.md`·외부 skill 파일은 변경하지 않습니다. MIT 공지·`THIRD_PARTY_NOTICES.md`·vendor 출처를 보존합니다. `SOURCE_PROVENANCE.json`은 출처, `PACKAGE_MANIFEST.json`은 배포 해시 기준이며 최종 갱신은 배포 담당자의 검토 사항입니다. 이번 문서 작업에서 매니페스트를 재생성하지 않습니다.
