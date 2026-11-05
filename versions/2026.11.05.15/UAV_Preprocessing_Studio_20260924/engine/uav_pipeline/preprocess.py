from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


STAGE_TITLES = {
    "elc_gui": "1. 타프 위치·후보·원영상 ROI",
    "radiometric_priority": "2A. 선택 후보 우선 방사보정",
    "radiometric": "2B. 전체 방사보정",
    "ed": "3. Ed 정규화·센서 교정",
    "elc_fit": "4. ELC a·b 산출·검증",
    "elc_apply_priority": "5A. 기준·독립 검증 영상 우선 반사도",
    "elc_apply": "5. ELC 계수 적용·반사도 영상",
}


def tool_status(cfg: dict) -> list[dict]:
    prep = cfg["preprocess"]
    rows = []
    managed_issues = None
    if cfg.get("dataset"):
        from .preparation import build
        try:
            _, managed_issues, _ = build(cfg)
        except (ValueError, OSError) as exc:
            managed_issues = {s: [str(exc)] for s in ("radiometric", "ed", "elc_fit", "elc_apply")}
    for stage, title in STAGE_TITLES.items():
        tool_stage = "radiometric" if stage == "radiometric_priority" else "elc_apply" if stage == "elc_apply_priority" else stage
        script = Path(__file__).with_name("elc_fit.py") if stage == "elc_fit" else Path(prep["tools"].get(tool_stage, ""))
        config_key = f"{stage}_config"
        config = prep.get(config_key, "")
        rows.append(
            {
                "stage": stage,
                "title": title,
                "script": str(script),
                "script_exists": script.is_file(),
                "config": config,
                "config_exists": bool(cfg.get("dataset") and stage in ("elc_gui", "radiometric_priority", "elc_fit", "elc_apply_priority")
                                      or config and Path(config).is_file()),
                "blockers": (managed_issues or {}).get(stage, []),
            }
        )
    return rows


def _effective_ed_config(cfg: dict, config_path: Path) -> Path:
    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if cfg["preprocess"].get("allow_nonunity_sensor_gains", False):
        return config_path
    data["sensor_gains"] = {str(index): 1.0 for index in range(1, 11)}
    output_text = cfg.get("output", {}).get("directory", "")
    root = Path(output_text) / "00_실행설정" if output_text else config_path.parent / "generated_configs"
    root.mkdir(parents=True, exist_ok=True)
    effective = root / f"{config_path.stem}_unity_sensor_gains.json"
    effective.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ED_SENSOR_GAINS unity override: {effective}", flush=True)
    return effective


def command_for_stage(cfg: dict, stage: str) -> list[str]:
    if stage not in STAGE_TITLES:
        raise ValueError(f"지원하지 않는 전처리 단계: {stage}")
    if cfg.get("preparation", {}).get("elc_only") and stage in ("radiometric", "elc_apply"):
        raise ValueError("ELC 영상만 처리 모드에서는 전체 영상 단계를 실행할 수 없습니다.")
    prep = cfg["preprocess"]
    python = prep.get("python_executable") or sys.executable
    if cfg.get("dataset") and stage == "elc_fit":
        return [python, str(Path(__file__).with_name("preparation_runner.py")),
                "--config", cfg["_config_path"], "--stage", stage]
    tool_stage = "radiometric" if stage == "radiometric_priority" else "elc_apply" if stage == "elc_apply_priority" else stage
    script = Path(prep["tools"].get(tool_stage, ""))
    if not script.is_file():
        raise FileNotFoundError(f"기존 도구를 찾을 수 없습니다: {script}")
    if cfg.get("dataset"):
        if stage in ("radiometric_priority", "elc_apply_priority"):
            return [python, str(Path(__file__).with_name("preparation_runner.py")),
                    "--config", cfg["_config_path"], "--stage", stage]
        if stage == "elc_gui":
            return [python, str(Path(__file__).with_name("preparation_runner.py")),
                    "--config", cfg["_config_path"], "--stage", stage]
        from .preparation import build
        _, issues, _ = build(cfg)
        if issues[stage]:
            raise ValueError("\n".join(issues[stage]))
        return [python, str(Path(__file__).with_name("preparation_runner.py")),
                "--config", cfg["_config_path"], "--stage", stage]
    if stage == "elc_gui":
        return [python, str(script)]
    config_key = f"{stage}_config"
    config_path = Path(prep.get(config_key, ""))
    if not config_path.is_file():
        raise FileNotFoundError(f"{STAGE_TITLES[stage]} 설정 파일을 지정하세요: {config_path}")
    if stage == "ed":
        config_path = _effective_ed_config(cfg, config_path)
    if stage == "radiometric":
        return [python, str(script), "--config", str(config_path), "--stage", "radiometric"]
    return [python, str(script), "--config", str(config_path)]


def run_stage(cfg: dict, stage: str, wait: bool = True):
    command = command_for_stage(cfg, stage)
    cwd = str(Path(command[1]).parent)
    if wait:
        return subprocess.run(command, cwd=cwd, check=True)
    return subprocess.Popen(command, cwd=cwd)
