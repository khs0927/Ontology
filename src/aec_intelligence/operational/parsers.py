"""Observations with traceable source coordinates; no inferred BIM solids."""
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree

from ..classifier import classify
from ..dxf import _normalize_entity, INSUNITS

PIPELINE_VERSION = 'aec-evidence-1'
SUPPORTED = {'.dxf','.dwg','.pdf','.svg','.ifc','.png','.jpg','.jpeg','.tif','.tiff'}
ALIASES = {'Wall':'벽 벽체','Door':'문 출입문','Window':'창 창호','Slab':'슬래브 바닥',
           'Column':'기둥','Beam':'보','Space':'실 공간','Storey':'층','Annotation':'주석 문자',
           'Dimension':'치수','Document':'도면 문서','View':'뷰 평면도','Page':'페이지 시트'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def observation(doc, key, kind, text, evidence, bbox=None, state='OBSERVED', properties=None):
    return {'id':'obs_' + digest(doc+'|'+key), 'type':kind, 'label':text[:250] or kind,
            'search_text':f'{text} {kind} {ALIASES.get(kind, "")}', 'state':state,
            'evidence':evidence, 'bbox':bbox or {}, 'properties':properties or {}, 'storey':''}


def relation(subject,predicate,target,state='OBSERVED',**evidence):
    return {'id':'rel_'+digest(subject+'|'+predicate+'|'+target),'subject':subject,
            'predicate':predicate,'object':target,'state':state,'evidence':evidence}


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (ValueError, Exception):
        return str(path)


def parse_source(source, doc, output, settings, source_name=None):
    output.mkdir(parents=True,exist_ok=True)
    name = source_name or source.name
    base = {'source_hash':source.parent.name,'source_path':_safe_relative(source, settings.data_root),
            'source_name':name,'parser_version':PIPELINE_VERSION}
    root = observation(doc,'document','Document',name,base)
    objects, relations, warnings = [root], [], []
    result = {'objects':objects,'relations':relations,'warnings':warnings,'units':'unknown',
              'parser_version':PIPELINE_VERSION,'metrics':{'raw_entities':0,'pages':0,'views':0}}
    def add(obj, parent=root):
        objects.append(obj)
        relations.append(relation(parent['id'],'contains',obj['id'],source_hash=base['source_hash']))
    suffix = source.suffix.lower()
    if suffix == '.dwg':
        from ..dwg import ODAConverter
        converted = ODAConverter(settings.oda_executable or None).convert_to_dxf(source,output/'converted')
        (output/'conversion.json').write_text(json.dumps(converted.to_dict(),ensure_ascii=False),encoding='utf-8')
        if converted.status != 'SUCCESS':
            raise ValueError('; '.join(converted.errors))
        source = Path(converted.output)
        suffix = '.dxf'
    if suffix == '.dxf':
        import ezdxf
        from ezdxf import bbox as ezbbox, disassemble
        from ezdxf.addons.drawing import Frontend,RenderContext,layout,svg
        document = ezdxf.readfile(source)
        result['units'] = INSUNITS.get(int(document.header.get('$INSUNITS',0)), 'unknown')
        for block in document.blocks:
            if block.block.dxf.flags & 4:
                warnings.append(f'XREF requires review: {block.name}')
        for index, sheet in enumerate(document.layouts):
            view = observation(doc,'layout:'+sheet.name,'View',f'{name} {sheet.name}',{**base,'layout':sheet.name})
            add(view)
            result['metrics']['views'] += 1
            preview = output/f'layout-{index}.svg'
            backend = svg.SVGBackend()
            try:
                Frontend(RenderContext(document),backend).draw_layout(sheet,finalize=True)
                preview.write_text(backend.get_string(layout.Page(0,0)),encoding='utf-8')
                view['evidence']['preview_path'] = _safe_relative(preview, settings.data_root)
                warnings.append('SVG coordinates differ from CAD WCS; use the evidence overlay for CAD selection.')
            except Exception as exc:
                warnings.append(f'Preview {sheet.name}: {exc}')
            with (output/f'geometry-{index}.jsonl').open('w',encoding='utf-8') as geometry_file:
                for entity in sheet:
                    result['metrics']['raw_entities'] += 1
                    handle = str(entity.dxf.handle)
                    try:
                        normalized = _normalize_entity(entity)
                        bounds = ezbbox.extents([entity],fast=False)
                        if bounds.has_data:
                            normalized.bbox = dict(zip(('min_x','min_y','min_z','max_x','max_y','max_z'),
                                                      [*bounds.extmin,*bounds.extmax]))
                        # Preserve original DXF values and world-space primitives, including nested INSERT transforms.
                        normalized.properties['dxf_attributes'] = {k:str(v) for k,v in entity.dxf.all_existing_dxf_attribs().items()}
                        if entity.dxftype() == 'LWPOLYLINE':
                            normalized.geometry['xyseb'] = [list(p) for p in entity.get_points('xyseb')]
                            normalized.geometry['elevation'] = entity.dxf.elevation
                            normalized.geometry['extrusion'] = list(entity.dxf.extrusion)
                        primitives = []
                        for child in disassemble.recursive_decompose([entity]):
                            primitive = disassemble.make_primitive(child)
                            primitives.append({'kind':child.dxftype(),'vertices':[list(v) for v in primitive.vertices()]})
                        normalized.geometry['world_primitives'] = primitives
                        geometry_file.write(json.dumps(normalized.to_dict(),ensure_ascii=False,default=str)+'\n')
                        kind, classification = classify(normalized)
                        if kind == 'CADEntity':
                            continue
                        text = str(normalized.properties.get('text') or normalized.properties.get('block_name') or '')
                        evidence = {**base,'layout':sheet.name,'handle':handle,'coordinate_system':'CAD_WCS',
                                    'geometry_path':_safe_relative(output/f'geometry-{index}.jsonl', settings.data_root)}
                        obj = observation(doc,f'{sheet.name}:{handle}',kind,
                            f'{name} {sheet.name} {normalized.layer} {text} {ALIASES.get(kind,kind)}',evidence,
                            normalized.bbox,state='AI_INFERRED' if kind not in ('Annotation','Dimension') else 'OBSERVED',
                            properties={**normalized.properties,'classification':classification.to_dict()})
                        add(obj,view)
                    except Exception as exc:
                        warnings.append(f'{sheet.name}/{handle}: {exc}')
        warnings.append('Layer-based building classes are candidates, not confirmed physical elements.')
    elif suffix == '.ifc':
        from ..formats import IFCParser,normalize_ifc_to_cair
        parsed = IFCParser().parse(source)
        cair = normalize_ifc_to_cair(parsed,doc,'artifact_'+base['source_hash'],base['source_hash'])
        result['metrics']['raw_entities'] = len(parsed.entities)
        mapping = {}
        for item in cair.objects:
            obj = observation(doc,str(item.source.entity_id),item.type,
                f'{name} {item.type} {json.dumps(item.properties,ensure_ascii=False)}',
                {**base,'ifc_global_id':item.source.entity_id},item.bbox,properties=item.properties)
            mapping[item.id] = obj['id']
            add(obj)
        for rel in cair.relations:
            data = rel.to_dict()
            if data['subject'] in mapping and data['object'] in mapping:
                relations.append(relation(mapping[data['subject']],data['predicate'],mapping[data['object']],source_hash=base['source_hash']))
        (output/'ifc-geometry.jsonl').write_text(''.join(json.dumps(r,default=str)+'\n' for r in parsed.geometry_rows),encoding='utf-8')
        warnings.extend(parsed.warnings)
    elif suffix == '.pdf':
        import fitz
        with fitz.open(source) as pdf:
            for i,page in enumerate(pdf):
                page_obj = observation(doc,f'page:{i+1}','Page',f'{name} {i+1}페이지',
                    {**base,'page':i+1,'coordinate_system':'PDF_POINTS','page_size':[page.rect.width,page.rect.height]})
                add(page_obj)
                result['metrics']['pages'] += 1
                blocks = page.get_text('blocks')
                for j,block in enumerate(blocks):
                    x,y,X,Y,text,*_ = block
                    if not str(text).strip(): continue
                    add(observation(doc,f'page:{i+1}:block:{j}','Annotation',str(text),
                        {**page_obj['evidence'],'bbox':[x,y,X,Y]},dict(min_x=x,min_y=y,max_x=X,max_y=Y)),page_obj)
                (output/f'page-{i+1}-vectors.json').write_text(json.dumps(page.get_drawings(),default=str),encoding='utf-8')
                if not any(str(b[4]).strip() for b in blocks):
                    image = output/f'page-{i+1}.png'
                    page.get_pixmap(matrix=fitz.Matrix(2,2)).save(image)
                    ocr_items = ocr(image,doc,f'page:{i+1}',{**page_obj['evidence'],'ocr_scale':2})
                    for obj in ocr_items: add(obj,page_obj)
        # Docling augments text/table content; coordinate-bearing blocks above remain the primary citations.
        try:
            from docling.document_converter import DocumentConverter
            converted = DocumentConverter().convert(source)
            (output/'docling.json').write_text(json.dumps(converted.document.export_to_dict(),ensure_ascii=False),encoding='utf-8')
        except ImportError:
            warnings.append('Docling unavailable; PDF text and drawing extraction completed without table reconstruction.')
    elif suffix in {'.png','.jpg','.jpeg','.tif','.tiff'}:
        for obj in ocr(source,doc,'image',{**base,'coordinate_system':'IMAGE_PIXELS'}): add(obj)
        result['metrics']['pages'] = 1
    elif suffix == '.svg':
        # Never render untrusted SVG directly in the application origin.
        tree = ElementTree.parse(source)
        for i,element in enumerate(tree.iter()):
            if element.tag.rsplit('}',1)[-1] in ('text','tspan'):
                text = ''.join(element.itertext()).strip()
                if text: add(observation(doc,f'svg:{i}','Annotation',text,{**base,'svg_element':element.get('id',str(i))}))
        warnings.append('SVG text indexed; element-to-world coordinate mapping requires review.')
    else:
        raise ValueError(f'Unsupported format: {suffix}')
    return result


def ocr(source,doc,key,evidence):
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError('OCR_REQUIRED: run this job with the ocr-worker profile (PaddleOCR)') from exc
    engine = PaddleOCR(lang='korean',use_angle_cls=True,show_log=False)
    pages = engine.ocr(str(source),cls=True)
    objects = []
    for p,rows in enumerate(pages or []):
        for i,(polygon,(text,confidence)) in enumerate(rows or []):
            scale = evidence.get('ocr_scale',1)
            xs,ys = [v[0]/scale for v in polygon],[v[1]/scale for v in polygon]
            bbox = [min(xs),min(ys),max(xs),max(ys)]
            objects.append(observation(doc,f'{key}:ocr:{p}:{i}','Annotation',text,
                {**evidence,'bbox':bbox,'ocr_confidence':float(confidence),'method':'PaddleOCR'},
                dict(zip(('min_x','min_y','max_x','max_y'),bbox))))
    return objects
