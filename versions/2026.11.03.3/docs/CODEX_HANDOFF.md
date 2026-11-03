# 받는 PC의 Codex 인수인계

패키지 전체를 로컬 작업 폴더로 엽니다. 외부 Codex가 이 PC의 소스를 읽고 협업하는 방식이며, 원격 PC 자동 접속·계정 공유 기능이 아닙니다. 앱 내부 AI 로그인이나 API 키는 설치·처리에 필요하지 않습니다.

## 첫 메시지

```text
이 폴더는 ELC Studio GENERAL의 ELC 반사도 산출 전용 소스 배포본이다.
AGENTS.md, README.md, docs/FIRST_START.md, docs/REQUIRED_INPUTS.md,
docs/KNOWN_LIMITS.md와 VALIDATION.md부터 읽어라.
디스크 공간, Conda/Python, imports, ExifTool 실행·래퍼, 배포 해시를 확인해라.
environment.yml로 ExifTool 13.07 이상 전용 환경을 설치하고 base는 변경하지 마라.
기존 환경의 실제 버전을 확인해라. 한글 경로·긴 경로 사전 차단을 우회하지 마라.
기본 런타임은 %LOCALAPPDATA%\ELCStudioShare\runtime-20261026이며 Share와 같다.
02_CHECK.bat와 합성 검사를 먼저 실행한 뒤 실제 실행기로 빈 GENERAL 프로젝트를 확인해라.
실제 날짜·세션·현장·외부 입력은 내가 지정한다. 날짜·GPS·교정값·승인을 만들지 마라.
shot CSV 대상 등록 → native 후보 → ROI/화소 검토·승인 → 방사·gain 1회·실측 Ed
→ 계수 awaiting_review → 사용자 적용 승인 흐름을 유지해라.
실제 입력을 내가 지정하고 처리에 동의하기 전에는 실자료 대량 처리를 하지 마라.
확인한 사실·실패·미확인 항목을 구분해 짧게 보고하고 다른 사람의 수정은 되돌리지 마라.
```

## 설치·합성 검사·빈 프로젝트

배포 루트 PowerShell에서 실행합니다. 런타임 위치를 바꿨다면 아래 기본값 대신 실제 전용 경로를 사용합니다.

```powershell
.\00_INSTALL.bat
.\02_CHECK.bat
$runtime = if ($env:ELC_STUDIO_ENV) { $env:ELC_STUDIO_ENV } else {
    Join-Path $env:LOCALAPPDATA 'ELCStudioShare\runtime-20261026'
}
$py = Join-Path $runtime 'python.exe'
& $py .\run_tests.py
```

실패하면 다음 단계로 자동 진행하지 않습니다. 기존 환경은 자동 업그레이드되지 않으므로 필요하면 `ELC_STUDIO_ENV`로 새 전용 위치를 정해 설치합니다. ExifTool 12.52의 긴 경로 실패와 짧은 경로 재검사를 새 런타임 전체 통과로 해석하지 않습니다. 합성 검사의 모의 처리 범위·전체 재검증 상태는 [VALIDATION.md](../VALIDATION.md)에서 확인합니다. [ExifTool 경로 제한](KNOWN_LIMITS.md)도 확인합니다.

1. 합성 검사 후 `01_START.bat`으로 사용자 지정 실제 날짜·세션·현장의 빈 프로젝트를 만듭니다. 추가 생성 창은 `03_NEW_PROJECT.bat`입니다.
2. ELC UI 5단계와 미연결·미승인 상태를 확인합니다. 선택 GPS는 태양각 기록용뿐입니다. 파일 존재·앱 실행과 과학 품질 검증을 구분합니다.
3. CLI가 필요하면 사용자에게 받은 값을 입력합니다. 아래 명령은 빈 프로젝트만 만들며 실자료 처리를 시작하지 않습니다.

```powershell
$observedDate = Read-Host '실제 관측일 YYYY-MM-DD'
$session = Read-Host '실제 세션 식별자'
$site = Read-Host '현장 코드'
$workRoot = Read-Host '결과 작업루트 절대 경로'
& $py .\distribution.py new --date $observedDate --session $session --site $site --work-root $workRoot
$project = Join-Path (Join-Path (Join-Path (Join-Path $workRoot $site) $observedDate) $session) 'project.json'
.\01_START.bat -Smoke -Project $project
```

실제 반환된 경로가 `작업루트/현장/YYYY-MM-DD/세션/project.json`인지 확인합니다. 기존 프로젝트가 있으면 새로 만들지 말고 엽니다. GENERAL은 유효한 ISO 날짜의 연도를 고정하지 않으며 세션을 소수의 회차 목록에 맞춰 바꾸지 않습니다.

`ELC_STUDIO_PYTHON`은 개발자가 호환 Python을 명시적으로 선택할 때만 사용합니다. 설치 전에는 해제해야 하며 수신 PC 기본 경로가 아닙니다. `--skip-exif`는 부분 개발 진단일 뿐 정상 설치 확인을 대체하지 않습니다. GUI smoke는 테스트 소유 창만 실행·종료합니다.

## 소스 위치

아래 `Studio`는 `UAV_Preprocessing_Studio_20260924/`를 뜻하며 실제 폴더명은 유지합니다.

| 역할 | 위치 |
|---|---|
| 설치·진단·빈 프로젝트 | 루트 `launcher.ps1`, `distribution.py` |
| GENERAL 첫 화면 | `Studio/code/general_studio.py` |
| 프로젝트·명시적 대상·native 후보 | `Studio/engine/uav_pipeline/general_project.py` |
| 경로·단계 설정 | `Studio/engine/uav_pipeline/preparation.py` |
| EXIF 시각·Ed 매칭·경로 차단 | `Studio/engine/uav_pipeline/time_ed.py`, `exif_support.py` |
| 방사·gain·Ed 통합 | `Studio/engine/uav_pipeline/integrated.py` |
| 계수·승인·독립 검증 | 같은 패키지의 `elc_fit.py`, `easy_elc.py`, `elc_priority.py` |
| 최신 ROI 편집·추적 | 루트 `tarp_center_roi_tool/`, `Studio/engine/uav_pipeline/roi_studio_bridge.py` |
| 기존 처리 어댑터 | 루트 `outputs/nongwon_preprocess_gui/`, `ed_sensor_correction_tools/` |
| SDK·라이선스 | `vendor/micasense/`, `THIRD_PARTY_NOTICES.md` |

기본 결과 키는 `preparation.locations()`, 실제 보고서 경로는 `easy_status.json`·`quality_report.html`을 확인합니다. 최종 산출물은 native 밴드별 ELC TIFF·10밴드 계수 CSV·ROI/타프 독립 검증 HTML입니다. `runner.py` CLI도 ELC 작업만 허용합니다. 이전 Share 실행기로 우회하거나 제거된 공간 분석 기능을 다시 연결하지 않습니다.

## 보존과 전달

1. 원본 Share 패키지·실자료·다른 사람의 변경을 보존합니다. 사용자 허락 없이 입력·경로·CSV·로그·영상을 외부 업로드하지 않습니다.
2. 자격 증명·로그인 토큰·API 키·과거 실측 교정·승인 이력은 배포하지 않습니다. Codex 인증을 앱 내부 AI로 복사하지 않습니다.
3. `SOURCE_PROVENANCE.json`은 출처, `PACKAGE_MANIFEST.json`은 배포 해시 기준입니다. 정당한 변경도 해시 불일치를 만들므로 배포 담당자가 변경 내역을 검토하고 최종 매니페스트를 갱신해야 합니다. 불일치를 숨기지 않습니다.
4. 문서는 사용 계약을 설명하며 통과 사실을 만들지 않습니다. 검증 기록 담당자의 `VALIDATION.md`를 임의 수정하거나 Share 검사 결과를 GENERAL 전체 검증으로 확대하지 않습니다. MIT 공지와 vendor 원본·출처를 유지합니다.
