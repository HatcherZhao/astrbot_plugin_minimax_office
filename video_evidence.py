"""在既有 Office worker 内检查文档中实际出现的原片截图，不安装依赖。"""
from __future__ import annotations

import io
import json
import math
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def check_embedded_frames(documents, frames):
    """比较实际页面图片与原片缩略图；不把附件或任意图表算作截图。"""
    import fitz
    from PIL import Image, ImageChops, ImageStat

    def thumbnail(raw):
        """统一大小以容忍等比缩放及轻微 JPEG 重编码。"""
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 24000000:
                raise ValueError("图片像素过大")
            ratio = image.width / image.height
            return image.convert("RGB").resize((64, 64)), ratio

    def similar(candidate, reference):
        """要求比例一致和低像素误差，降低流程图误判为原片的概率。"""
        a, ar = candidate
        b, br = reference
        if abs(ar - br) / br > .02:
            return False
        difference = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
        if difference > 8:
            return False
        left, right = list(a.convert("L").getdata()), list(b.convert("L").getdata())
        lm, rm = sum(left) / len(left), sum(right) / len(right)
        lv = sum((x - lm) ** 2 for x in left)
        rv = sum((x - rm) ** 2 for x in right)
        # 空白页不能因为“都是白底”而与文字界面截图匹配。
        if rv < len(right) * 4:
            return difference <= 1 and lv < len(left) * 4
        if not lv:
            return False
        correlation = sum((x - lm) * (y - rm) for x, y in zip(left, right)) / math.sqrt(lv * rv)
        return correlation >= .985

    def document_images(path):
        """只读取 PDF 页面实际显示的图片；其他格式先转换 PDF 后交付。"""
        if path.suffix.lower() in {".docx", ".pptx", ".xlsx"}:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if len(names) > 5000:
                    raise ValueError("文档组件过多")
                seen = set()
                for name in names:
                    if not name.endswith(".xml") or "/_rels/" in name:
                        continue
                    parent, leaf = posixpath.split(name)
                    rels = posixpath.join(parent, "_rels", leaf + ".rels")
                    if rels not in names:
                        continue
                    targets = {r.get("Id"): r.get("Target") for r in ET.fromstring(archive.read(rels))
                               if r.get("TargetMode") != "External"}
                    for node in ET.fromstring(archive.read(name)).iter():
                        if node.tag.rsplit("}", 1)[-1] != "blip":
                            continue
                        rid = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                        target = targets.get(rid)
                        if not target:
                            continue
                        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(parent, target))
                        if target in names and target not in seen:
                            seen.add(target)
                            yield thumbnail(archive.read(target))
            return
        if path.suffix.lower() != ".pdf":
            raise ValueError("截图校验支持 PDF、DOCX、PPTX、XLSX；请转换为相应格式")
        with fitz.open(path) as document:
            if len(document) > 200:
                raise ValueError("文档超过200页")
            seen = set()
            for page in document:
                for image in page.get_image_info(xrefs=True):
                    xref = image.get("xref")
                    if not xref or xref in seen:
                        continue
                    seen.add(xref)
                    if len(seen) > 200:
                        raise ValueError("文档图片过多")
                    yield thumbnail(document.extract_image(xref)["image"])

    base = Path("/work/input")
    def input_path(name):
        """仅使用服务端登记的输入文件名。"""
        if Path(name).name != name:
            raise ValueError("输入文件名无效")
        return base / name

    references = [(frame["file_id"], thumbnail(input_path(frame["input_name"]).read_bytes())) for frame in frames]
    results = []
    for document in documents:
        matched = set()
        for candidate in document_images(input_path(document["input_name"])):
            for frame_id, reference in references:
                if similar(candidate, reference):
                    matched.add(frame_id)
        missing = [frame_id for frame_id, _ in references if frame_id not in matched]
        results.append({"artifact_id": document["artifact_id"], "matched_frames": sorted(matched), "missing_frames": missing})
    return {"passed": bool(references) and bool(results) and all(not x["missing_frames"] for x in results), "documents": results}


def validation_code(documents, frames):
    """生成固定验证程序，模型只能选择产物，不能指定验证逻辑。"""
    source = Path(__file__).read_text(encoding="utf-8")
    return source + "\nprint(json.dumps(check_embedded_frames(" + repr(documents) + ", " + repr(frames) + "), ensure_ascii=False))\n"
