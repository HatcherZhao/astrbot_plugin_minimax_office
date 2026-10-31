"""验证 PDF/Office 图片引用及白底误匹配，依赖使用运行镜像已安装的库。"""
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import video_evidence
try:
    import fitz
    from PIL import Image, ImageDraw
except ImportError:
    fitz = None


@unittest.skipIf(fitz is None, "需要运行环境中的 PyMuPDF 和 Pillow")
class EmbeddedFrameTests(unittest.TestCase):
    """真实文档图片测试，不以文件名或图片计数替代图片比对。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        picture = Image.new("RGB", (320, 180), "white")
        draw = ImageDraw.Draw(picture)
        draw.rectangle((12, 12, 180, 160), fill="#1672c4")
        draw.rectangle((45, 50, 140, 110), fill="yellow")
        picture.save(self.root / "frame.jpg")
        self.frames = [{"file_id": "source", "input_name": "frame.jpg"}]

    def tearDown(self):
        self.temp.cleanup()

    def check(self, filename):
        """仅把验证器的固定输入根映射到临时目录。"""
        with patch.object(video_evidence, "Path", side_effect=lambda value: self.root if value == "/work/input" else Path(value)):
            return video_evidence.check_embedded_frames([{"artifact_id": "doc", "input_name": filename}], self.frames)

    def pdf(self, image=None):
        """构造含图片或只有矢量矩形的成品。"""
        doc = fitz.open()
        page = doc.new_page()
        if image:
            page.insert_image(fitz.Rect(30, 30, 350, 210), filename=str(self.root / image))
        else:
            page.draw_rect(fitz.Rect(30, 30, 350, 210), color=(0, 0, 1))
        doc.save(self.root / "out.pdf")
        doc.close()

    def test_embedded_original_passes(self):
        self.pdf("frame.jpg")
        self.assertTrue(self.check("out.pdf")["passed"])

    def test_diagram_does_not_pass(self):
        self.pdf()
        self.assertFalse(self.check("out.pdf")["passed"])

    def test_white_image_does_not_replace_sparse_screenshot(self):
        picture = Image.new("RGB", (320, 180), "white")
        ImageDraw.Draw(picture).rectangle((20, 20, 25, 150), fill="black")
        picture.save(self.root / "frame.jpg")
        Image.new("RGB", (320, 180), "white").save(self.root / "blank.jpg")
        self.pdf("blank.jpg")
        self.assertFalse(self.check("out.pdf")["passed"])

    def test_office_orphan_media_is_not_a_displayed_image(self):
        media = (self.root / "frame.jpg").read_bytes()
        for referenced in [False, True]:
            with zipfile.ZipFile(self.root / "out.docx", "w") as z:
                z.writestr("word/media/frame.jpg", media)
                node = '<a:blip r:embed="img"/>' if referenced else ""
                z.writestr("word/document.xml", '<w:document xmlns:w="w" xmlns:a="a" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'+node+'</w:document>')
                z.writestr("word/_rels/document.xml.rels", '<Relationships><Relationship Id="img" Target="media/frame.jpg"/></Relationships>')
            self.assertEqual(self.check("out.docx")["passed"], referenced)
