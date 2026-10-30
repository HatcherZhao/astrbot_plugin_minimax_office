"""在一次性控制器中验证真实 worker 的创建、编辑、转换与隔离。"""

import hashlib
import io
import json
import os
import textwrap
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from service.app import app

OWNER = 'a' * 64
OTHER = 'b' * 64
HEADERS = {'Authorization': 'Bearer ' + os.environ['OFFICE_RUNTIME_TOKEN']}
RESULTS = []


def run_job(client, label, language, code, inputs=None, expected='succeeded', timeout=180):
    """提交真实任务、等待终态并保存脱敏结果，失败立即暴露诊断。"""
    response = client.post('/v1/jobs', headers=HEADERS, json={'owner': OWNER, 'language': language, 'code': textwrap.dedent(code), 'input_file_ids': inputs or [], 'timeout_seconds': timeout})
    assert response.status_code == 200, response.text
    job_id = response.json()['job_id']
    deadline = time.monotonic() + timeout + 35
    while time.monotonic() < deadline:
        detail = client.get('/v1/jobs/' + job_id, headers=HEADERS, params={'owner': OWNER}).json()
        if detail['state'] in {'succeeded', 'failed', 'timed_out'}:
            break
        time.sleep(0.5)
    assert detail['state'] == expected, json.dumps({'case': label, **detail}, ensure_ascii=False)
    artifacts = {}
    for artifact in detail.get('artifacts', []):
        url = f"/v1/jobs/{job_id}/artifacts/{artifact['artifact_id']}"
        assert client.get(url, headers=HEADERS, params={'owner': OTHER}).status_code == 404
        download = client.get(url, headers=HEADERS, params={'owner': OWNER})
        assert download.status_code == 200
        assert hashlib.sha256(download.content).hexdigest() == artifact['sha256']
        artifacts[artifact['filename']] = download.content
        (Path('/verification') / artifact['filename']).write_bytes(download.content)
    row = {'case': label, 'state': detail['state'], 'artifacts': list(artifacts), 'stdout': detail.get('stdout', '')[-3000:], 'stderr': detail.get('stderr', '')[-1000:], 'error': detail.get('error')}
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return artifacts


def upload(client, name, content):
    """将上一轮产物再次作为输入，模拟用户要求继续修改文档。"""
    response = client.post('/v1/files', headers=HEADERS, data={'owner': OWNER}, files={'file': (name, content)})
    assert response.status_code == 200, response.text
    return response.json()


def main():
    """验收四种文档与执行边界，不向 QQ 发送任何文件。"""
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.post('/v1/jobs', json={}).status_code == 401
        doc = run_job(client, 'DOCX_CSharp_Create', 'csharp', '''
            using DocumentFormat.OpenXml;
            using DocumentFormat.OpenXml.Packaging;
            using DocumentFormat.OpenXml.Wordprocessing;
            using DocumentFormat.OpenXml.Validation;
            using MiniMaxAIDocx.Core.Samples;
            DocumentCreationSamples.CreateMinimalDocument("/work/output/sample.docx");
            var path = "/work/output/office-original.docx";
            using (var document = WordprocessingDocument.Create(path, WordprocessingDocumentType.Document)) {
                var part = document.AddMainDocumentPart();
                part.Document = new Document(new Body(
                    new Paragraph(new Run(new RunProperties(new RunFonts { Ascii="Noto Sans CJK SC", EastAsia="Noto Sans CJK SC" }), new Text("待修改标题"))),
                    new Paragraph(new Run(new Text("正文保持不变：中文办公文档验收。"))),
                    new SectionProperties(new PageSize { Width=11906U, Height=16838U })));
                part.Document.Save();
            }
            using var verify = WordprocessingDocument.Open(path, false);
            var errors = new OpenXmlValidator().Validate(verify).ToList();
            if (errors.Count > 0) throw new Exception(errors[0].Description);
            Console.WriteLine("OpenXML validation passed");
        ''')
        record = upload(client, 'office-original.docx', doc['office-original.docx'])
        inp = '/work/input/' + record['input_name']
        edited = run_job(client, 'DOCX_Edit_And_PDF', 'python', f'''
            import subprocess,zipfile,xml.etree.ElementTree as E
            src={inp!r};out='/work/output/office-edited.docx'
            with zipfile.ZipFile(src) as reader,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as writer:
                for item in reader.infolist():
                    data=reader.read(item.filename)
                    if item.filename=='word/document.xml':
                        root=E.fromstring(data)
                        for node in root.iter('{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}t'):
                            if node.text=='待修改标题':node.text='已修改标题'
                        data=E.tostring(root,encoding='utf-8',xml_declaration=True)
                    writer.writestr(item,data)
            subprocess.run(['minimax-docx-cli','--help'],check=True,stdout=subprocess.DEVNULL)
            subprocess.run(['soffice','-env:UserInstallation=file:///tmp/lo-docx','--headless','--convert-to','pdf','--outdir','/work/output',out],check=True)
            print('DOCX edit and PDF conversion passed')
        ''', [record['file_id']])
        with zipfile.ZipFile(io.BytesIO(edited['office-edited.docx'])) as archive:
            xml = archive.read('word/document.xml').decode()
            assert '已修改标题' in xml and '正文保持不变' in xml
        assert edited['office-edited.pdf'].startswith(b'%PDF')
        excel = run_job(client, 'XLSX_XML_Create_Recalculate', 'python', '''
            import pathlib,shutil,subprocess,xml.etree.ElementTree as E
            root=pathlib.Path('/tmp/book');shutil.copytree('/opt/skills/minimax-xlsx/templates/minimal_xlsx',root)
            ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
            sheet=f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>金额</t></is></c><c r="B1"><v>10</v></c></row><row r="2"><c r="B2"><v>20</v></c></row><row r="3"><c r="B3"><f>SUM(B1:B2)</f><v></v></c></row></sheetData></worksheet>'
            (root/'xl/worksheets/sheet1.xml').write_text(sheet)
            (root/'xl/worksheets/sheet2.xml').write_text(f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>保持不变</t></is></c><c r="B1"><v>99</v></c></row></sheetData></worksheet>')
            p=root/'xl/workbook.xml';tree=E.parse(p);sheets=tree.find(f'{{{ns}}}sheets');sheets[0].set('name','明细');E.SubElement(sheets,f'{{{ns}}}sheet',{'name':'保留页','sheetId':'2','{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id':'rId20'});tree.write(p,encoding='utf-8',xml_declaration=True)
            p=root/'xl/_rels/workbook.xml.rels';tree=E.parse(p);E.SubElement(tree.getroot(),'{http://schemas.openxmlformats.org/package/2006/relationships}Relationship',{'Id':'rId20','Type':'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet','Target':'worksheets/sheet2.xml'});tree.write(p,encoding='utf-8',xml_declaration=True)
            p=root/'[Content_Types].xml';tree=E.parse(p);E.SubElement(tree.getroot(),'{http://schemas.openxmlformats.org/package/2006/content-types}Override',{'PartName':'/xl/worksheets/sheet2.xml','ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'});tree.write(p,encoding='utf-8',xml_declaration=True)
            scripts='/opt/skills/minimax-xlsx/scripts/'
            subprocess.run(['python3',scripts+'xlsx_pack.py',str(root),'/work/output/original.xlsx'],check=True)
            subprocess.run(['python3',scripts+'formula_check.py','/work/output/original.xlsx','--json'],check=True)
            subprocess.run(['python3',scripts+'libreoffice_recalc.py','/work/output/original.xlsx','/work/output/recalculated.xlsx'],check=True)
            import openpyxl
            w=openpyxl.load_workbook('/work/output/recalculated.xlsx',data_only=True)
            assert w.sheetnames==['明细','保留页'] and w['明细']['B3'].value==30 and w['保留页']['B1'].value==99
            print('Formula cache=30, both sheets preserved')
        ''')
        record = upload(client, 'original.xlsx', excel['original.xlsx'])
        inp = '/work/input/' + record['input_name']
        run_job(client, 'XLSX_Edit_Preserve', 'python', f'''
            import zipfile,xml.etree.ElementTree as E
            src={inp!r};out='/work/output/edited.xlsx'
            with zipfile.ZipFile(src) as before,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as after:
                for item in before.infolist():
                    data=before.read(item.filename)
                    if item.filename=='xl/worksheets/sheet1.xml':
                        root=E.fromstring(data);ns={{'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}}
                        root.find('.//s:c[@r="B1"]/s:v',ns).text='15';data=E.tostring(root,encoding='utf-8',xml_declaration=True)
                    after.writestr(item,data)
            with zipfile.ZipFile(src) as a,zipfile.ZipFile(out) as b:
                for n in a.namelist():
                    if n!='xl/worksheets/sheet1.xml':assert a.read(n)==b.read(n)
            print('Only target worksheet changed; all other ZIP members preserved')
        ''', [record['file_id']])
        slides = run_job(client, 'PPTX_Create', 'javascript', '''
            const PptxGenJS=require('pptxgenjs');
            const p=new PptxGenJS();p.layout='LAYOUT_WIDE';p.author='MiniMax Office';
            p.addSlide().addText('中文演示文稿验收',{x:0.5,y:1,w:11,h:1,fontFace:'Noto Sans CJK SC',fontSize:30});
            p.addSlide().addText('第二页保持不变',{x:0.5,y:1,w:11,h:1,fontFace:'Noto Sans CJK SC',fontSize:24});
            p.writeFile({fileName:'/work/output/original.pptx'});
        ''')
        record = upload(client, 'original.pptx', slides['original.pptx'])
        inp = '/work/input/' + record['input_name']
        run_job(client, 'PPTX_Edit_And_Preview', 'python', f'''
            import zipfile,subprocess
            src={inp!r};out='/work/output/edited.pptx'
            with zipfile.ZipFile(src) as a,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as b:
                for item in a.infolist():
                    data=a.read(item.filename)
                    if item.filename=='ppt/slides/slide1.xml':data=data.replace('中文演示文稿验收'.encode(),'更新后的演示标题'.encode())
                    b.writestr(item,data)
            with zipfile.ZipFile(src) as a,zipfile.ZipFile(out) as b:assert a.read('ppt/slides/slide2.xml')==b.read('ppt/slides/slide2.xml')
            subprocess.run(['soffice','-env:UserInstallation=file:///tmp/lo-ppt','--headless','--convert-to','pdf','--outdir','/work/output',out],check=True)
            subprocess.run(['pdftoppm','-f','1','-singlefile','-scale-to','1200','-png','/work/output/edited.pdf','/work/output/slide-preview'],check=True)
            print('PPTX title edited, second slide unchanged, PDF preview rendered')
        ''', [record['file_id']])
        run_job(client, 'PDF_Create_And_Form_Edit', 'python', '''
            import json,pathlib,subprocess
            content=[{'type':'h1','text':'中文文档能力验收'},{'type':'body','text':'本文件由 MiniMax 文档技能在隔离环境生成，支持中文与表格。'},{'type':'table','headers':['项目','结果'],'rows':[['中文字体','通过'],['文档编辑','通过']]},{'type':'chart','chart_type':'bar','labels':['第一项','第二项'],'datasets':[{'label':'验收数量','values':[2,3]}],'title':'中文图表'}]
            pathlib.Path('/tmp/content.json').write_text(json.dumps(content,ensure_ascii=False))
            subprocess.run(['bash','/opt/skills/minimax-pdf/scripts/make.sh','run','--title','中文报告验收','--type','minimal','--author','Office Demo','--date','2026-10-31','--content','/tmp/content.json','--out','/work/output/report.pdf'],check=True)
            from reportlab.pdfgen.canvas import Canvas
            c=Canvas('/tmp/form.pdf');c.drawString(50,800,'Name');c.acroForm.textfield(name='Name',value='original',x=50,y=760,width=200,height=20);c.showPage();c.save()
            subprocess.run(['python3','/opt/skills/minimax-pdf/scripts/fill_write.py','--input','/tmp/form.pdf','--out','/work/output/filled.pdf','--values','{"Name":"updated"}'],check=True)
            from pypdf import PdfReader
            assert PdfReader('/work/output/filled.pdf').get_fields()['Name']['/V']=='updated'
            text=''.join(p.extract_text() or '' for p in PdfReader('/work/output/report.pdf').pages)
            assert '中文' in text and '隔离环境' in text
            subprocess.run(['pdftoppm','-f','2','-singlefile','-scale-to','1200','-png','/work/output/report.pdf','/work/output/pdf-preview'],check=True)
            print('Chinese PDF text extraction and form fill passed')
        ''')
        run_job(client, 'Network_And_Private_Data_Isolation', 'python', '''
            import socket,os,pathlib
            assert not pathlib.Path('/var/run/docker.sock').exists()
            assert not pathlib.Path('/AstrBot/data/cmd_config.json').exists()
            assert not any('TOKEN' in key or 'SECRET' in key for key in os.environ)
            try:socket.create_connection(('1.1.1.1',443),timeout=2)
            except OSError:pass
            else:raise AssertionError('Network unexpectedly reachable')
            try:pathlib.Path('/etc/unwanted').write_text('x')
            except OSError:pass
            else:raise AssertionError('Root filesystem writable')
            pathlib.Path('/work/output/isolation.txt').write_text('隔离检查通过')
        ''')
        run_job(client, 'Symlink_Output_Rejected', 'python', "import os;os.symlink('/etc/passwd','/work/output/leak.txt')", expected='failed')
        run_job(client, 'Timeout_Enforced', 'python', 'import time;time.sleep(90)', expected='timed_out', timeout=10)
    Path('/verification/runtime-smoke.json').write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    print(json.dumps({'all_passed': True, 'cases': len(RESULTS)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
