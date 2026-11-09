# Cloud work completion, 2026-11-05

## Round 2 (2026-11-07)

통합 1단계입니다. 제품은 khs0927/Ontology-platform이 되고, 이 저장소는 그 수집·어댑터 패키지(`packages/aec`)가 됩니다.
계획은 [CONSOLIDATION_PLAN.md](CONSOLIDATION_PLAN.md)에 있으며, Ontology-platform에도 같은 파일이 있습니다.

- 생산자 쪽 계약 테스트 `tests/test_consumer_contract_aec_cair_snapshot.py`를 추가했습니다. 소비자(Ontology-platform `sion_cair.ingest`)와 똑같이 `python -m aec_intelligence.cli ingest-dxf|ingest-file`을 실행한 뒤, 결과 `project-cair.json`을 소비자 스키마 사본(`tests/contracts/aec-cair-snapshot-1.schema.json`)으로 검증합니다. 객체 id의 고유성·재실행 안정성과 원본 해시도 확인합니다.
- 코드는 삭제하지 않았습니다. 드리프트(이 저장소와 `packages/aec`가 따로 수정된 부분)는 계획 문서에 정리했습니다. 2단계에서 `packages/aec`로 이식한 뒤 이 저장소의 `master`를 동결할 것을 권장합니다.
- 열린 PR #80, #83, #84, #86–#91은 이미 #100에 통합되어 있어 닫아도 됩니다(직접 닫지는 않았습니다). #101과 #102는 2단계에서 `packages/aec`로 이식하는 것을 권장합니다.

## Round 3 (2026-11-09)

데스크톱 런타임 수용 테스트를 Linux에서 실제 프로그램으로 실행했습니다(Ubuntu 24.04 공식 패키지 Blender 4.0.2, QGIS 3.34.4).

- **QGIS 어댑터 버그 수정 (제품 코드)**: 가상환경(venv)에서 `QGISApplicationAdapter`를 쓰면 `qgis_process`의 내장 Python이
  PATH의 venv 인터프리터를 잡아 `No module named 'qgis'` 후 SIGSEGV(-11)로 죽었습니다. 이제 자식 프로세스 환경에서
  호출자 venv(`VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`, venv `bin`)를 뺍니다. 디스플레이가 없는 Linux/macOS에서는
  `QT_QPA_PLATFORM=offscreen`을 기본으로 둡니다(없으면 Qt가 시작 전에 중단). 실제 QGIS로 `native:buffer` GeoPackage 처리 통과.
- **Blender**: DXF·IFC 기하 → `.blend`/`.glb`/미리보기 내보내기를 실제 Blender로 통과. FreeCAD 없이 도는 Blender 단독 수용 테스트 2건 추가.
- **테스트 이식성**: FreeCAD/Blender 경로를 `AEC_FREECAD_EXECUTABLE` / `AEC_BLENDER_EXECUTABLE` → PATH → 기존 Windows 경로 순으로
  찾습니다(`tests/_runtimes.py`). `probe-apps` 테스트가 호스트에 QGIS가 깔려 있으면 실패하던 문제도 고쳤습니다.
- **CI**: `desktop-runtimes` 잡이 Blender·QGIS를 설치해 위 수용 테스트를 건너뜀 없이 실행합니다.
- 결과: 전체 테스트(실제 Postgres) 822 통과 / 0 실패, E2E 43/43.
- 이 환경에서 못 한 것: FreeCAD(Ubuntu 24.04 저장소에 없음. 공식 AppImage 실행은 이 세션의 권한 정책으로 차단),
  LibreDWG(저장소에 없음. 소스 빌드 차단), ODA·PowerShell 테스트(Windows 전용). 통합 계획 2단계(`packages/aec`로 이식)는
  Ontology-platform 저장소 접근이 필요해 진행하지 못했습니다.
