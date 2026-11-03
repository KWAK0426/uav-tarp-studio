# 배포용 앱 업데이트

1. 연구용 코드와 배포용 코드의 변경을 검토합니다. 현재 배포 범위는 native MX Dual 타프 ELC까지이며 Metashape·BRDF를 연결하지 않습니다.
2. `versions/<버전>/`의 배포 소스에 변경을 반영합니다. 기존 라이선스와 출처를 보존합니다. 실자료·교정값·승인 이력은 가져오지 않습니다.
3. 선택한 환경에서 `run_tests.py`, `test_desktop.py`, `test_general_e2e.py`와 실제 `UAV_Studio.exe`를 검사합니다. 실제 앱 창·Codex 인증/추론·새 PC 설치·실자료 정확도는 서로 다른 검증입니다.
4. `release-tools/build_release.py --version YYYY.MM.DD.N`으로 새 버전을 만듭니다. 현재 작업 버전을 기본 입력으로 사용합니다. 소스 목록 밖의 파일과 개인 경로를 차단합니다.
5. `release-output/<버전>/`의 소스 변경·ZIP 목록·해시를 검토합니다. `UAV-Tarp-Studio-Windows.zip`은 새 PC용, `studio-update.zip`은 기존 설치 업데이트용입니다.
6. GitHub Releases에 태그 `vYYYY.MM.DD.N`으로 위 두 ZIP과 `SHA256SUMS.txt`를 올립니다. 앱은 공개된 최신 정식 Release만 확인합니다. 임시/시험 버전은 prerelease로 표시하면 일반 업데이트에 잡히지 않습니다.
7. 다른 폴더에 설치한 앱의 업데이트 버튼으로 다운로드·검사·재시작·이전 버전 복원을 확인합니다.

GitHub 로그인과 앱의 Codex 로그인은 별개입니다. 배포용 파일에 토큰을 넣지 않습니다. 이 저장소에 원자료를 추가하거나 전체 연구 폴더를 Git에 등록하지 않습니다.

원래 연구 스튜디오와 배포용 GENERAL의 경로·입력·승인 계약이 다르므로 두 폴더를 통째로 동기화하지 않습니다. 배포된 버전 폴더를 고치며 실행 중인 처리를 바꾸지 말고 새 버전으로 발행합니다.

앱 내 업데이트는 `versions/<버전>`의 앱 코드만 교체합니다. 루트의 launcher·bootstrap·환경 검사 도구를 바꿀 때는 새 전체 배포 ZIP을 별도 폴더에 풀고 기존 프로젝트를 열어 사용합니다. 해당 변경은 Release 설명에 명시합니다. GitHub의 소스에는 빌드 도구가 포함되며, 사용자의 설치 ZIP에는 연구 경로·환경 기록·인증 정보가 포함되지 않습니다.
