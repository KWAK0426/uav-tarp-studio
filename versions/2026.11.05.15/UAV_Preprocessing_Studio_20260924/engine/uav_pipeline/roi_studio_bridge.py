"""Project-scoped handoff between stage 02, the native ROI editor, and ELC."""
from __future__ import annotations

import csv
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import tifffile
from PIL import Image, ImageDraw, ImageFont

from .preparation import latest_candidate_selection
from .elc_fit import _band_for_wavelength, _general_provenance, _library, _library_key, _read_values
from .general_project import is_general, scan_raw


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # A Windows reader may briefly hold the progress JSON without delete sharing.
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5,32) or attempt == 5:raise
                time.sleep(.025 * (attempt+1))
    finally:
        temp.unlink(missing_ok=True)


def pixel_mask(points, shape):
    """Integer pixel centers; polygon vertices are in OpenCV center coordinates."""
    p = np.asarray(points, dtype="float64")
    if p.shape != (4, 2) or not np.isfinite(p).all() or not cv2.isContourConvex(p.astype("float32")):
        raise ValueError("유효한 사각형 ROI가 아닙니다.")
    mask = np.zeros(shape, bool)
    x0, y0 = np.maximum(0, np.floor(p.min(0)).astype(int))
    x1, y1 = np.minimum([shape[1], shape[0]], np.ceil(p.max(0)).astype(int) + 1)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    inside = np.ones(xx.shape, bool)
    sign = 1 if cv2.contourArea(p.astype("float32"), oriented=True) > 0 else -1
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        inside &= sign * ((b[0]-a[0])*(yy-a[1])-(b[1]-a[1])*(xx-a[0])) >= -1e-6
    mask[y0:y1, x0:x1] = inside
    return mask


def filter_limits(manifest, shot, tarp):
    rule = manifest.get("images", {}).get(shot, {}).get("roi_settings", {}).get(tarp, {}).get("percentile_filter")
    if rule is None:
        return float(manifest.get("percentile_low", 5)), float(manifest.get("percentile_high", 95))
    if not rule.get("enabled"):
        return 0., 100.
    low, high = float(rule["low"]), float(rule["high"])
    if not np.isfinite([low, high]).all() or not 0 <= low < high <= 100:
        raise ValueError("ROI 백분위 필터 범위 오류")
    return low, high


def is_fit_roi(manifest, shot, tarp, library_row):
    role = manifest.get("images", {}).get(shot, {}).get("roi_settings", {}).get(tarp, {}).get("role")
    return int(library_row["use_for_fit"]) == 1 and role != "validation"


class Session:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.cfg = read_json(self.config_path)
        selection, data, shots = latest_candidate_selection(self.cfg)
        if not selection or not shots:
            raise ValueError("02에서 후보 영상을 먼저 선택·저장하세요.")
        if data.get("split_locations"):
            raise ValueError("두 위치의 다른 영상을 합쳐 계수를 추정하는 방식은 아직 지원하지 않습니다.")
        self.selection = Path(selection)
        self.selection_hash = digest(selection)
        self.context_path = self.selection.parent.parent / "context.json"
        self.context = read_json(self.context_path)
        if (self.context.get("source_kind") != "raw_DN_boundary_only"
                or Path(self.context["image_source"]).resolve() != Path(self.cfg["preparation"]["raw_directory"]).resolve()):
            raise ValueError("02에서 현재 회차의 native 원영상 후보를 다시 저장하세요. 정합 프리뷰나 radiance를 원영상으로 간주하지 않습니다.")
        self.folder = Path(self.context["output"]) / "studio_roi"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.draft_path = self.folder / "draft.json"
        self.handoff_path = self.config_path.with_suffix(".roi-handoff.json")
        self.shots = sorted(shots)
        self.provenance = _general_provenance(self.cfg) if is_general(self.cfg) else {}
        self.files = {}
        if is_general(self.cfg):
            self.files = {f"{shot}_{suffix}": path for (shot, suffix), path in
                          scan_raw(self.context["image_source"], shots=self.shots).items() if shot in self.shots}
        else:
            for path in Path(self.context["image_source"]).rglob("*"):
                if path.is_file() and path.suffix.lower() in (".tif", ".tiff"):
                    if path.stem in self.files:
                        raise ValueError(f"중복 원영상: {path.stem}")
                    self.files[path.stem] = path
        self.signatures = {}
        for shot in self.shots:
            for band in range(1, 11):
                key = f"{shot}_{band}"
                if key not in self.files:
                    raise ValueError(f"02 선택 영상의 원본 10밴드가 부족합니다: {key}")
                stat = self.files[key].stat()
                self.signatures[key] = [str(self.files[key].resolve()), stat.st_size, stat.st_mtime_ns]
        self.library = _library(Path(self.cfg["preparation"]["tarp_reflectance_csv"]))
        self.library_hash = digest(self.cfg["preparation"]["tarp_reflectance_csv"])
        saved = read_json(self.draft_path) if self.draft_path.exists() else {}
        same = saved.get("sources") == self.signatures and saved.get("selection_sha256") == self.selection_hash
        if self.provenance:
            # Reconfirming identical candidates creates a new filename, not new imagery/geometry.
            same = same and all(saved.get(k) == v for k, v in self.provenance.items() if k != 'selection_file')
        self.images = saved.get("images", {}) if same else {}
        self.tracking = saved.get("tracking", {}) if same else {}
        reference = saved.get("reference_shot") if same else self.cfg["preparation"].get("reference_shot")
        self.reference = reference if reference in self.shots else self.shots[0]
        self.last_manifest = None
        from .roi_batch_import import import_proposals
        imported, self.import_holds = import_proposals(self)
        if imported or (same and self.provenance and saved.get('selection_file')!=self.provenance['selection_file']):self.save_draft()

    def assert_current(self, scan_registry=True):
        cfg = read_json(self.config_path)
        if self.provenance:
            if not is_general(cfg) or _general_provenance(cfg) != self.provenance:
                raise ValueError("General project identity/target manifest/context changed; reopen ROI Studio")
            if scan_registry:
                registry = scan_raw(cfg["preparation"]["raw_directory"], shots=self.shots)
                if {f"{s}_{b}": p for (s, b), p in registry.items() if s in self.shots} != self.files:
                    raise ValueError("General selected raw source registry changed; reopen ROI Studio")
        elif is_general(cfg):
            raise ValueError("Project edition changed; reopen ROI Studio")
        path, _, _ = latest_candidate_selection(cfg)
        if Path(path or "").resolve() != self.selection.resolve() or digest(self.selection) != self.selection_hash:
            raise ValueError("02 후보가 변경되었습니다. ROI 창을 닫고 03을 다시 여세요. 이전 승인 결과는 사용하지 않습니다.")
        cfg = read_json(self.config_path)
        if cfg.get("dataset") != self.cfg.get("dataset"):
            raise ValueError("프로젝트 날짜·회차가 변경되었습니다. 03을 다시 여세요.")
        if (cfg["preparation"].get("tarp_reflectance_csv") != self.cfg["preparation"]["tarp_reflectance_csv"]
                or digest(self.cfg["preparation"]["tarp_reflectance_csv"]) != self.library_hash):
            raise ValueError("타프 기준 반사도 CSV가 변경되었습니다. ROI 창을 다시 여세요.")
        for key, signature in self.signatures.items():
            path = self.files[key]
            if [str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns] != signature:
                raise ValueError(f"원영상 변경 감지: {key}. ROI를 다시 확인하세요.")

    def save_draft(self):
        self.assert_current()
        atomic_json(self.draft_path, {"schema": "studio_roi_draft_v1", "selection_sha256": self.selection_hash,
                    "sources": self.signatures, "reference_shot": self.reference, "images": self.images,
                    "tracking": self.tracking, **self.provenance})
        # A changed draft invalidates an older publication without deleting its history.
        pointer = Path(self.context["output"]) / "reviewed_manifest_latest.json"
        if pointer.exists():
            old = read_json(pointer)
            if old.get("editor") == "TarpROIStudio":
                old["draft_dirty"] = True
                atomic_json(pointer, old)

    def registration_exclusion_reason(self, shot):
        if shot not in self.shots:return '현재 선택에 없는 영상입니다.'
        if shot==self.reference:return '계수 산출 기준 영상입니다. 기준 영상을 먼저 변경해야 합니다.'
        if len(self.shots)<=2:return '계수 산출용과 검증용 영상을 최소 한 장씩 남겨야 합니다.'
        for name in self.shots:
            if name==shot:continue
            records=self.images.get(name,[])
            if not records or any(not r.get('reviewed') or not r.get('pixel_reviewed') for r in records):
                return '남길 영상의 ROI 확인을 먼저 완료해야 합니다.'
        return ''

    def exclude_rejected(self, rejected, reason=None):
        """Explicit result-screen exclusion; keep history and all retained approvals."""
        import urllib.request
        self.assert_current()
        if reason:
            for shot in rejected:
                blocked=self.registration_exclusion_reason(shot)
                if blocked:raise ValueError(blocked)
        kept=[s for s in self.shots if s not in rejected]
        if len(kept)<2 or any(not self.images.get(s) or any(not r.get('reviewed') or not r.get('pixel_reviewed') for r in self.images[s]) for s in kept):
            raise ValueError('계수 산출용과 검증용으로 확인된 영상이 최소 두 장 필요합니다. 보류 영상은 확인하거나 아니다(제외)로 표시하세요.')
        selection=read_json(self.selection);server=read_json(Path(self.context['output'])/'server.json')
        url=server['url']
        if not url.startswith('http://127.0.0.1:'):raise ValueError('후보 연결 주소가 올바르지 않습니다.')
        with urllib.request.urlopen(url+'/context',timeout=5) as response:
            # /context exposes the documented summary, not the complete on-disk config.
            keys=('identity','label','mosaic','mosaic_role','tarps','source_kind',
                  'candidate_preview_kind','output','reference_timing','code_revision')
            if json.load(response)!={k:self.context[k] for k in keys}:
                raise ValueError('02의 위치·후보 연결이 변경되었습니다. 결과를 다시 여세요.')
        retained=copy.deepcopy({s:self.images[s] for s in kept})
        atomic_json(self.folder/('draft_before_exclusion_'+str(time.time_ns())+'.json'),read_json(self.draft_path))
        payload=dict(ids=[r['id'] for r in selection['selected'] if r['shot'] in kept],selected_pairs=[],
                     method='manual_centers',
                     locations=[{'u':loc['point']['u'],'v':loc['point']['v'],
                                 'radius':loc['radius_m'],'tarps':loc['tarps']} for loc in selection['locations']],
                     regions=selection.get('regions',{}))
        request=urllib.request.Request(url+'/save',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=30) as response:json.load(response)
        previous_reference=self.reference;previous_auto=getattr(self,'auto_result',{})
        previous_selection=str(self.selection)
        updated=Session(self.config_path)
        if any(updated.signatures[k]!=self.signatures[k] for k in updated.signatures):raise ValueError('원영상이 변경되어 기존 ROI 승인을 재사용할 수 없습니다.')
        updated.images=retained
        updated.reference=previous_reference if previous_reference in kept else kept[0]
        for shot,records in retained.items():
            for record in records:record['role']='calibration' if shot==updated.reference else 'validation'
        updated.auto_result=previous_auto
        updated.save_draft()
        if reason:
            atomic_json(self.folder/('registration_exclusion_'+str(time.time_ns())+'.json'),
                        {'action':'user_clicked_exclude_and_continue','excluded_shots':sorted(rejected),
                         'reason':reason,'previous_selection':previous_selection,
                         'new_selection':str(updated.selection),'reference_shot':updated.reference})
        self.__dict__.update(updated.__dict__)

    def propose_tracking(self, progress=lambda text: None):
        """Reuse native registration and center detector; never approve copied geometry."""
        self.assert_current()
        seeds = self.images.get(self.reference, [])
        if not seeds or any(not r.get("reviewed") or not r.get("pixel_reviewed") for r in seeds):
            raise ValueError("기준 영상의 타프 경계·내부 화소를 먼저 확인·확정하세요.")
        from core import Raster, inset, ordered
        tool = self._transforms()
        source = tool.read(self.files[self.reference + "_6"])
        results = {}
        for index, shot in enumerate(self.shots, 1):
            if shot == self.reference:
                continue
            progress(f"영상 간 추적 {index}/{len(self.shots)} · {shot}")
            existing = copy.deepcopy(self.images.get(shot, []))
            retained = {r["tarp_id"] for r in existing if r.get("reviewed")}
            existing = [r for r in existing if r.get("reviewed")]
            pending = [r for r in seeds if r["tarp_id"] not in retained]
            diagnostics = {}
            raster = None
            try:
                target = tool.read(self.files[shot + "_6"])
                # transform maps first array -> second array, in pixel-center coordinates.
                h, quality = tool.transform(source, target, False)
                reverse, _ = tool.transform(target, source, False)
                raster = Raster(self.files[shot + "_6"])
                for seed in pending:
                    name = seed["tarp_id"]
                    try:
                        native = np.float32(seed["outer"]) - .5
                        predicted = cv2.perspectiveTransform(native[None], h)[0]
                        back = cv2.perspectiveTransform(predicted[None], reverse)[0]
                        error = float(np.max(np.linalg.norm(back - native, axis=1)))
                        if error > 2:
                            raise ValueError(f"왕복 정합 오차 {error:.2f}px 초과")
                        if (not cv2.isContourConvex(predicted) or not np.isfinite(predicted).all()
                                or (predicted < 0).any() or (predicted[:, 0] >= raster.width).any()
                                or (predicted[:, 1] >= raster.height).any()):
                            raise ValueError("예측 ROI가 영상 밖이거나 사각형이 아닙니다.")
                        center = predicted.mean(0) + .5
                        size = float(np.sqrt(abs(cv2.contourArea(predicted))))
                        candidates = raster.propose(center, 1, max(32, min(512, int(size * 3))))
                        ranked = []
                        for candidate in candidates:
                            points = np.float32(candidate["points"])
                            intersection = cv2.intersectConvexConvex(predicted, points - .5)[0]
                            union = abs(cv2.contourArea(predicted)) + abs(cv2.contourArea(points)) - intersection
                            overlap = float(intersection / max(union, 1e-9))
                            ranked.append((overlap, points, candidate))
                        if not ranked or max(r[0] for r in ranked) < .5:
                            raise ValueError("예측 위치와 일치하는 타프 경계를 찾지 못했습니다.")
                        overlap, points, candidate = max(ranked, key=lambda r: r[0])
                        # Never transfer source shadow strokes or source approval to another exposure.
                        record = copy.deepcopy(seed)
                        record.update(outer=ordered(points).tolist(), inner=inset(points, seed.get(
                            "margin_percent_shortest_altitude_per_edge", 5)).tolist(), center=center.tolist(),
                            role="validation", reviewed=False, pixel_reviewed=False, exclusion_strokes=[],
                            tracking={"source_shot": self.reference, "source_path": str(self.files[self.reference + "_6"]),
                                      "source_roi": seed["outer"], "source_to_target": h.tolist(),
                                      "roundtrip_max_px": error, "boundary_iou": overlap, "registration": quality,
                                      "boundary_method": candidate["method"], "status": "review_required"})
                        existing.append(record)
                        diagnostics[name] = {"status": "review_required", "boundary_iou": overlap}
                    except (ValueError, cv2.error, np.linalg.LinAlgError) as exc:
                        diagnostics[name] = {"status": "failed", "reason": str(exc)}
            except (ValueError, cv2.error, np.linalg.LinAlgError) as exc:
                diagnostics = {r["tarp_id"]: {"status": "failed", "reason": str(exc)} for r in pending}
            finally:
                if raster is not None:
                    raster.ds = None
            results[shot] = {"records": existing, "diagnostics": diagnostics}
        self.assert_current()
        return results

    def accept_tracking(self, results):
        self.assert_current()
        self.tracking = {"reference_shot": self.reference,
                         "expected_tarps": [r["tarp_id"] for r in self.images[self.reference]],
                         "results": {s: r["diagnostics"] for s, r in results.items()}}
        for shot, result in results.items():
            self.images[shot] = result["records"]
        self.save_draft()
        self.last_manifest = None
        atomic_json(self.folder / ("tracking_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6] + ".json"),
                    {"sources": self.signatures, "selection_sha256": self.selection_hash, **self.tracking})

    def _transforms(self):
        # Reuse the existing, quality-gated native-band registration in this child process.
        os.environ["UAV_TARP_CONTEXT"] = str(self.context_path)
        code = Path(__file__).parent / "tarp_tool" / "code"
        if str(code) not in sys.path:
            sys.path.insert(0, str(code))
        module = importlib.import_module("review_statistics")
        module.CONFIG = self.context
        module.ROOT = Path(self.context["output"])
        return module

    def publish(self, progress=lambda text: None):
        self.assert_current()
        if self.tracking:
            expected = set(self.tracking["expected_tarps"])
            for shot in self.shots:
                missing = expected - {r["tarp_id"] for r in self.images.get(shot, [])}
                if missing:
                    raise ValueError(f"{shot}: 추적 누락 타프 {sorted(missing)}. 직접 지정하거나 02에서 영상을 제외하세요.")
        reference_records = self.images.get(self.reference, [])
        fitting = [r for r in reference_records if r.get("role") == "calibration"
                   and self.library.get(_library_key(r.get("tarp_id", "")), {}).get("use_for_fit") == "1"]
        if len(fitting) < 2:
            raise ValueError("기준 영상에 서로 다른 실측 반사도의 계수 산출용 타프를 최소 2개 확정하세요.")
        selected = {shot: records for shot, records in self.images.items() if records}
        if set(selected) != set(self.shots):
            raise ValueError("02에서 선택한 각 영상에 ROI를 확정하세요. 사용하지 않을 영상은 02에서 제외하세요.")
        for shot, records in selected.items():
            ids = [r.get("tarp_id") for r in records]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{shot}: 같은 타프 ID를 두 번 사용할 수 없습니다.")
            for record in records:
                if record.get("role") not in ("calibration", "validation"):
                    raise ValueError(f"{shot}: ROI 용도를 지정하세요.")
                if not record.get("reviewed") or not record.get("pixel_reviewed"):
                    raise ValueError(f"{shot} {record['name']}: 경계 및 내부 화소 확인이 필요합니다.")
                if _library_key(record.get("tarp_id", "")) not in self.library:
                    raise ValueError(f"실측 반사도 CSV에 없는 타프 ID: {record.get('tarp_id')}")
                if shot != self.reference and record.get("role") != "validation":
                    raise ValueError(f"{shot}: 기준 영상 외의 ROI는 검증용으로 지정하세요.")
        tool = self._transforms()
        out = self.folder / ("approved_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
        out.mkdir()
        (out / "pixel_masks").mkdir()
        entries, images, rows, previews = [], {}, [], []
        for shot, records in selected.items():
            ref = tool.read(self.files[shot + "_6"])
            finite = np.isfinite(ref) & (ref < 65535)
            if not finite.any():
                raise ValueError(f"{shot}: ROI 확인 이미지의 유효 화소가 없습니다.")
            low, high = np.percentile(ref[finite], [1, 99])
            preview = Image.fromarray(np.uint8(np.nan_to_num(np.clip((ref-low)*255/max(high-low, 1e-9), 0, 255)))).convert("RGB")
            draw = ImageDraw.Draw(preview)
            try:
                font = ImageFont.truetype("malgun.ttf", 14)
            except OSError:
                font = ImageFont.load_default()
            for record in records:
                for key, color in (("outer", "#ffc66d"), ("inner", "#75d5b6")):
                    points = [tuple(point) for point in record[key]]
                    draw.line(points + points[:1], fill=color, width=2)
                draw.text(tuple(record["outer"][0]), str(record["tarp_id"]), fill="#ffffff", font=font)
            points = np.concatenate([r["outer"] for r in records])
            x0, y0 = np.maximum(0, points.min(0)-40).astype(int)
            x1, y1 = np.minimum([ref.shape[1], ref.shape[0]], points.max(0)+40).astype(int)
            preview_path = out / (shot + "_approved_roi.png")
            preview.crop((x0, y0, x1, y1)).save(preview_path)
            previews.append({"shot": shot, "path": str(preview_path), "sha256": digest(preview_path),
                             "crop_xyxy": [int(x0), int(y0), int(x1), int(y1)],
                             "source_path": str(self.files[shot + "_6"]), "kind": "native_DN_display_only"})
            tarps = {r["tarp_id"]: (np.asarray(r["inner"], dtype="float32") - .5).tolist() for r in records}
            images[shot] = {"approved": True, "tarps": tarps,
                            "roi_settings": {r["tarp_id"]: r for r in records}}
            masks = {}
            for record in records:
                mask = np.zeros(ref.shape, np.uint8)
                for stroke in record.get("exclusion_strokes", []):
                    x, y, radius, excluded = stroke
                    cv2.circle(mask, (round(x-.5), round(y-.5)), int(radius), 255 if excluded else 0, -1)
                masks[record["tarp_id"]] = mask
            for suffix in range(1, 11):
                progress(f"밴드 정합·좌표 전달 {len(entries)}/{len(selected)*10} 완료 · {shot} 원본 밴드 {suffix} 처리 중")
                source = self.files[f"{shot}_{suffix}"]
                array = tool.read(source)
                with tifffile.TiffFile(source) as tf:
                    tag = tf.pages[0].tags.get("XMP")
                    wave = None
                    if tag:
                        for element in ET.fromstring(tag.value).iter():
                            if element.tag.endswith("}CentralWavelength"):
                                wave = element.text
                            for key, value in element.attrib.items():
                                if key.endswith("}CentralWavelength"):
                                    wave = value
                if wave is None:
                    raise ValueError(f"실제 중심파장 메타데이터 없음: {source.name}")
                band = _band_for_wavelength(float(wave), self.cfg["band_names"])
                try:
                    h, quality = tool.band_transform(shot, suffix, array, ref, tarps)
                except ValueError as exc:
                    if not is_general(self.cfg) or not str(exc).startswith(f'{shot} band {suffix}: 정합 후보 모두 실패'):
                        raise
                    entries.append(dict(shot=shot,band_suffix=suffix,wavelength_nm=float(wave),source_path=str(source),
                                        native_image_size=[array.shape[1],array.shape[0]],registration_hold=str(exc),
                                        native_rois={},native_outer_rois={},native_exclusion_masks={}))
                    continue
                inverse = np.linalg.inv(h)
                entry = {"shot": shot, "band_suffix": suffix, "wavelength_nm": float(wave),
                         "source_path": str(source), "native_image_size": [array.shape[1], array.shape[0]],
                         "native_to_reference": h.tolist(), "quality": quality,
                         "sampling": "pixel_center_v1", "native_rois": {}, "native_outer_rois": {}, "native_exclusion_masks": {}}
                for record in records:
                    tarp = record["tarp_id"]
                    points = cv2.perspectiveTransform(np.float32(tarps[tarp])[None], inverse)[0].tolist()
                    excluded = cv2.warpPerspective(masks[tarp], inverse, (array.shape[1], array.shape[0]), flags=cv2.INTER_NEAREST)
                    mask_path = out / "pixel_masks" / f"{shot}_{suffix}_{len(entry['native_rois']):03d}.png"
                    Image.fromarray(excluded).save(mask_path)
                    info = {"path": str(mask_path), "sha256": digest(mask_path)}
                    entry["native_outer_rois"][tarp] = cv2.perspectiveTransform((np.float32(record["outer"]) - .5)[None], inverse)[0].tolist()
                    entry["native_rois"][tarp] = points
                    entry["native_exclusion_masks"][tarp] = info
                    low, high = filter_limits({"images": images}, shot, tarp)
                    values = _read_values(source, points, entry["native_image_size"], low, high, info, "pixel_center_v1")
                    rows.append({"shot": shot, "tarp": tarp, "band_suffix": suffix, "band": band,
                                 "role": record["role"], "source_kind": self.context["source_kind"],
                                 "n_pixels": len(values), "raw_DN_mean_geometry_QC_only": float(values.mean()),
                                 "percentile_low": low, "percentile_high": high})
                entries.append(entry)
                progress(f"밴드 정합·좌표 전달 {len(entries)}/{len(selected)*10} 완료")
        if not rows:raise ValueError('정합을 통과한 원본 밴드가 없습니다. 기존 확인 결과는 보존됩니다.')
        atomic_json(out / "band_transforms.json", entries)
        with (out / "tarp_statistics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        manifest = {"schema": "studio_native_roi_v1", "editor": "TarpROIStudio",
                    "identity": self.context["identity"], "date": self.context["date"], "flight": self.context["flight"],
                    "label": self.context["label"], "selection_file": str(self.selection), "selection_sha256": self.selection_hash,
                    "reference_shot": self.reference, "reference_selection": "user_confirmed_in_roi_studio",
                    "source_kind": self.context["source_kind"], "images": images, "pixel_mask_review_version": 1,
                    "trim_percent": "per_ROI_edge_inset", "percentile_low": 5, "percentile_high": 95,
                    "sampling": "pixel_center_v1", "draft_path": str(self.draft_path), "draft_sha256": digest(self.draft_path),
                    "library_csv": self.cfg["preparation"]["tarp_reflectance_csv"], "library_sha256": self.library_hash,
                    "preview_images": previews,
                    "statistics_csv": str(out / "tarp_statistics.csv"), "transforms_json": str(out / "band_transforms.json"),
                    "coordinate_transfer": {"reference": "native Band6; source pixel edges converted by -0.5 to pixel centers",
                        "target": "same shot/suffix/native size corrected radiance; not Photos or mosaics",
                        "trim": "each edge inset; no second trim", "percentiles": "per ROI; recomputed on corrected radiance"},
                    "statistics_role": "raw_DN_geometry_QC_only", "elc_ready": False}
        manifest_path = out / "roi_manifest.json"
        if self.provenance:
            manifest.update(self.provenance, transforms_sha256=digest(out / "band_transforms.json"))
        atomic_json(manifest_path, manifest)
        self.assert_current()
        atomic_json(Path(self.context["output"]) / "reviewed_manifest_latest.json",
                    {"path": str(manifest_path), "editor": "TarpROIStudio", "draft_dirty": False,
                     **({"manifest_sha256": digest(manifest_path), "transforms_sha256": manifest["transforms_sha256"],
                         **self.provenance} if self.provenance else {})})
        self.last_manifest = manifest_path
        self.navigate(None)
        return manifest_path

    def navigate(self, step):
        if not self.last_manifest:
            raise ValueError("먼저 승인 ROI를 04·05로 전달하세요.")
        self.assert_current()
        atomic_json(self.handoff_path, {"id": time.time_ns(), "date": self.context["date"], "flight": self.context["flight"],
                    "selection_file": str(self.selection), "selection_sha256": self.selection_hash,
                    "reference_shot": self.reference, "manifest": str(self.last_manifest),
                    "manifest_sha256": digest(self.last_manifest), "step": step})


def open_editor(parent, config_path):
    """Launch source UI with the same Python runtime as Studio, without file dialogs."""
    import subprocess
    app = Path(__file__).resolve().parents[3] / "tarp_center_roi_tool" / "app.py"
    if not app.is_file():
        raise FileNotFoundError(f"ROI Studio 연결 앱이 없습니다: {app}")
    geometry = f"1100x900{parent.winfo_rootx():+d}{parent.winfo_rooty():+d}"
    return subprocess.Popen([sys.executable, str(app), "--studio-config", str(config_path), "--geometry", geometry],
                            cwd=app.parent, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
