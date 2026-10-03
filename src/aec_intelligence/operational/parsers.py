"""Observations with traceable source coordinates; no inferred BIM solids."""
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree

from ..cair import Classification
from ..classifier import (_match, classify, detail_title, drawing_category, element_mark, room_from_text, semantic_class,
                          steel_sections, title_block_fields)
from ..dxf import (_normalize_entity, INSUNITS, NormalizedCADEntity, block_effective_name, decode_dxf_text,
                   effective_block_name, read_dxf)

PIPELINE_VERSION = 'aec-evidence-1'
SUPPORTED = {'.dxf','.dwg','.pdf','.svg','.ifc','.png','.jpg','.jpeg','.tif','.tiff'}
ALIASES = {'Wall':'벽 벽체','Door':'문 출입문','Window':'창 창호','Slab':'슬래브 바닥',
           'Column':'기둥','Beam':'보','Space':'실 공간','Storey':'층','Annotation':'주석 문자',
           'Dimension':'치수','Document':'도면 문서','View':'뷰 평면도','Page':'페이지 시트',
           'Stair':'계단','Furniture':'가구 위생기구','Elevator':'승강기 엘리베이터','Grid':'그리드 통심',
           'BlockDefinition':'블록 도면블록','Layer':'레이어','TitleBlock':'표제란 도곽 도면번호',
           'SteelSection':'형강 단면 철골','CADEntity':'블록참조'}
STRUCTURAL_KINDS = ('Beam','Column')
ROOM_TAG_RE = re.compile(r'ROOM|RM_?NAME|NAME|실명|실이름|공간', re.IGNORECASE)
ROOM_NUMBER_TAG_RE = re.compile(r'ROOM_?NO|RM_?NO|NUMBER|실번호|호수', re.IGNORECASE)
AREA_TAG_RE = re.compile(r'AREA|면적', re.IGNORECASE)
SECTION_TAG_RE = re.compile(r'SIZE|SECTION|PROFILE|MEMBER|규격|부재|단면', re.IGNORECASE)
AREA_TEXT_RE = re.compile(r'^\(?\s*(\d{1,5}(?:[.,]\d{1,3})?)\s*(?:㎡|m2|m²|sqm)\s*\)?$', re.IGNORECASE)
STOREY_RE = re.compile(r'(?<![A-Za-z0-9])(?:(?P<b>지하|B)\s*(?P<bn>\d{1,2})\s*(?:층|F)?|(?P<n>\d{1,3})\s*(?:층|F)(?![A-Za-z])|(?P<roof>옥탑|지붕|(?:PH|ROOF)(?![A-Za-z])))',
                       re.IGNORECASE)


def storey_from_text(text):
    """'1층 평면도' -> '1층', '지하2층' / 'B2F' -> 'B2', '옥탑층' -> '옥탑'; None when no storey is named."""
    found = STOREY_RE.search(str(text or ''))
    if not found:
        return None
    if found['b']:
        return f"B{int(found['bn'])}"
    if found['n']:
        return f"{int(found['n'])}층"
    return '옥탑' if found['roof'] in ('옥탑', '지붕') else found['roof'].upper()


def _iter_nested_insert_texts(insert, max_depth=8):
    """Yield WCS-transformed TEXT/MTEXT/ATTRIB entities nested below an INSERT.

    Top-level INSERT attributes are handled separately by _insert_attributes.
    This walker is for text physically stored inside block definitions, including
    nested blocks, which is common in Korean production drawings.
    """
    def walk(ref, path, depth):
        if depth > max_depth:
            return
        try:
            children = list(ref.virtual_entities())
        except Exception:
            return
        for index, child in enumerate(children):
            child_path = (*path, index)
            kind = child.dxftype()
            if kind in ('TEXT', 'MTEXT', 'ATTRIB'):
                yield child, child_path
            elif kind == 'INSERT':
                # Nested INSERT attributes are not guaranteed to be returned by
                # virtual_entities(), so include them when ezdxf exposes them.
                for attr_index, attrib in enumerate(list(getattr(child, 'attribs', []) or [])):
                    yield attrib, (*child_path, 'a', attr_index)
                yield from walk(child, child_path, depth + 1)

    yield from walk(insert, (), 1)

def _finite_bbox(bounds):
    """ezdxf BoundingBox -> bbox dict, or {} when empty or not finite."""
    if bounds is None or not bounds.has_data:
        return {}
    values = [*bounds.extmin, *bounds.extmax]
    if not all(math.isfinite(v) for v in values):
        return {}
    return dict(zip(('min_x','min_y','min_z','max_x','max_y','max_z'), values))


class _DXFSemantics:
    """Document-level CAD semantics: block catalog, layer inventory, rooms, sheets, details and steel.

    Everything here is linear in the number of entities; proximity uses a uniform grid per layout.
    """

    def __init__(self, doc, name, base, document, result, add, relations, warnings):
        self.doc, self.name, self.base, self.document = doc, name, base, document
        self.result, self.add, self.relations, self.warnings = result, add, relations, warnings
        self.root = result['objects'][0]
        self.blocks = {}
        self.block_lookup = {}
        self.insert_counts = Counter()
        self.layer_counts = Counter()
        self.block_layer_counts = Counter()
        self.metrics = Counter()
        self._relation_ids = set()

    # ----------------------------------------------------------------- helpers
    def relate(self, subject, predicate, target, state='OBSERVED', **evidence):
        rel = relation(subject, predicate, target, state, source_hash=self.base['source_hash'], **evidence)
        if rel['id'] not in self._relation_ids:
            self._relation_ids.add(rel['id'])
            self.relations.append(rel)

    # ----------------------------------------------------------------- blocks
    def catalog_blocks(self, ezbbox):
        cache = ezbbox.Cache()
        for block in self.document.blocks:
            if block.is_any_layout:
                continue
            dxf_name = str(block.name)
            block_name = decode_dxf_text(dxf_name)
            flags = int(block.block.dxf.get('flags', 0) or 0)
            is_xref = bool(flags & 4 or flags & 8)
            is_anonymous = block_name.startswith('*') or bool(flags & 1)
            if is_xref:
                self.warnings.append(f'XREF requires review: {block_name}')
            entity_counts, layers, nested, attdefs, texts = Counter(), Counter(), Counter(), [], []
            for entity in block:
                kind = entity.dxftype()
                entity_counts[kind] += 1
                layer = decode_dxf_text(entity.dxf.get('layer', '0'))
                layers[layer] += 1
                if kind == 'INSERT':
                    nested[decode_dxf_text(effective_block_name(entity))] += 1
                elif kind == 'ATTDEF':
                    attdefs.append({'tag': decode_dxf_text(entity.dxf.get('tag', '')),
                                    'prompt': decode_dxf_text(entity.dxf.get('prompt', '')),
                                    'default': decode_dxf_text(entity.dxf.get('text', '')),
                                    'constant': bool(int(entity.dxf.get('flags', 0) or 0) & 2)})
                elif kind in ('TEXT', 'MTEXT') and len(texts) < 20:
                    try:
                        value = entity.plain_text() if kind == 'MTEXT' else entity.dxf.get('text', '')
                    except Exception:
                        value = entity.dxf.get('text', '')
                    value = decode_dxf_text(value).strip()
                    if value:
                        texts.append(value[:120])
            self.block_layer_counts.update(layers)
            effective = decode_dxf_text(block_effective_name(block.block_record) or dxf_name)
            properties = {
                'name': block_name, 'dxf_name': dxf_name, 'effective_name': effective, 'is_anonymous': is_anonymous,
                'anonymous_kind': block_name[1:2].upper() if block_name.startswith('*') else '',
                'is_xref': is_xref, 'is_dynamic_representation': effective != block_name,
                'entity_count': sum(entity_counts.values()), 'entity_count_by_type': dict(sorted(entity_counts.items())),
                'attribute_defs': attdefs, 'nested_block_names': sorted(nested),
                'nested_reference_counts': dict(sorted(nested.items())), 'layers_used': sorted(layers),
                'texts': texts, 'insert_count': 0, 'insert_count_by_layout': {}, 'effective_names': [effective],
            }
            try:
                properties['base_point'] = [float(v) for v in block.block.dxf.get('base_point', (0, 0, 0))]
            except Exception:
                pass
            if xref_path := (block.block.dxf.get('xref_path', '') if is_xref else ''):
                properties['xref_path'] = str(xref_path)
            if not is_xref and entity_counts:
                try:
                    properties['block_bbox'] = _finite_bbox(ezbbox.extents(block, fast=True, cache=cache))
                except Exception:
                    pass
            # Same rules as INSERT classification so "every door block" is answerable without instances.
            probe = NormalizedCADEntity(block_name, 'INSERT', '0', properties={
                'block_name': block_name, 'effective_name': effective,
                'attributes': {a['tag']: a['default'] for a in attdefs}})
            semantic, classification = classify(probe)
            properties['semantic_type'] = semantic if semantic != 'CADEntity' else ''
            properties['classification'] = classification.to_dict()
            if title_block_fields({a['tag']: a['default'] or a['tag'] for a in attdefs}):
                properties['semantic_type'] = 'TitleBlock'
            label = ' '.join(filter(None, (block_name, effective if effective != block_name else '',
                                           properties['semantic_type'], ' '.join(a['tag'] for a in attdefs),
                                           ' '.join(texts[:5]))))
            obj = observation(self.doc, 'block:' + dxf_name, 'BlockDefinition', f'{self.name} {label}',
                              {**self.base, 'block': block_name}, properties=properties)
            self.blocks[dxf_name] = obj
            self.block_lookup[dxf_name] = self.block_lookup[block_name] = obj
            self.add(obj)
        # Dynamic block representations point back to their source definition.
        for obj in self.blocks.values():
            block_name, effective = obj['properties']['name'], obj['properties']['effective_name']
            source = self.block_lookup.get(effective)
            if effective != block_name and source is not None:
                source['properties']['effective_names'].append(block_name)
                self.relate(obj['id'], 'derivedFrom', source['id'], method='AcDbBlockRepBTag')
        self.metrics['blocks'] = len(self.blocks)
        self.metrics['anonymous_blocks'] = sum(o['properties']['is_anonymous'] for o in self.blocks.values())

    # ----------------------------------------------------------------- layouts
    def begin_layout(self, sheet, view):
        self.sheet, self.view = sheet, view
        self.is_paper = not sheet.is_modelspace
        self.title_blocks, self.title_texts, self.members, self.sections = [], [], [], []
        self.area_texts, self.text_spaces = [], []
        self.layout_objects = []

    def count_entity(self, entity):
        self.layer_counts[decode_dxf_text(entity.dxf.get('layer', '0'))] += 1
        if entity.dxftype() == 'INSERT':
            name = str(entity.dxf.get('name', ''))
            self.insert_counts[name] += 1
            block = self.blocks.get(name)
            if block is not None:
                per_layout = block['properties']['insert_count_by_layout']
                per_layout[self.sheet.name] = per_layout.get(self.sheet.name, 0) + 1

    def enrich(self, obj, normalized, evidence):
        props = normalized.properties
        kind = obj['type']
        obj['properties']['layer'] = normalized.layer
        self.layout_objects.append(obj)
        if normalized.entity_type == 'INSERT':
            self.metrics['inserts'] += 1
            raw = str(props.get('block_name', ''))
            effective = str(props.get('effective_name') or raw)
            target = self.block_lookup.get(effective) or self.block_lookup.get(raw)
            if target is not None:
                self.relate(obj['id'], 'instanceOf', target['id'], block=raw, effective_block=effective)
            if kind == 'TitleBlock':
                self.metrics['title_blocks'] += 1
                self.title_blocks.append(obj)
            self._insert_attributes(obj, props.get('attributes') or {}, evidence)
            if kind in STRUCTURAL_KINDS:
                self.members.append(obj)
            return
        if kind in STRUCTURAL_KINDS:
            self.members.append(obj)
        if normalized.entity_type not in ('TEXT', 'MTEXT', 'ATTRIB'):
            return
        text = str(props.get('text') or '').strip()
        if not text:
            return
        mark = element_mark(text)
        if mark:
            obj['properties'].update(mark)
        finer, room = semantic_class(normalized, kind)
        if finer == 'Space':
            space = self._space(obj, room, evidence, 'text')
            self.text_spaces.append((space, normalized))
        elif area := AREA_TEXT_RE.match(text):
            self.area_texts.append((float(area.group(1).replace(',', '.')), obj, normalized))
        for i, section in enumerate(steel_sections(text)):
            self._section(obj, section, evidence, i, normalized)
        detail = detail_title(text)
        if detail:
            obj['properties']['detail_title'] = detail['detail_title']
            obj['properties']['detail_category'] = detail['drawing_category']
            if detail['strong']:
                self._detail_view(obj, detail, evidence)
        if len(text) <= 40 and drawing_category(('text', text))['drawing_category'] != '기타':
            self.title_texts.append((float(props.get('height') or 0.0), text))

    def _insert_attributes(self, obj, attributes, evidence):
        room, number, area = None, None, None
        for tag, value in attributes.items():
            value = str(value).strip()
            if not value:
                continue
            if ROOM_NUMBER_TAG_RE.search(tag):
                number = value
            elif AREA_TAG_RE.search(tag):
                found = re.search(r'\d+(?:[.,]\d+)?', value)
                area = float(found.group(0).replace(',', '.')) if found else None
            elif ROOM_TAG_RE.search(tag) and room is None:
                room = room_from_text(value)
            if SECTION_TAG_RE.search(tag):
                for i, section in enumerate(steel_sections(value)):
                    sec = self._section(obj, section, evidence, f'{tag}:{i}', None)
                    if obj['type'] in STRUCTURAL_KINDS:
                        # The designation is an attribute of this very member: direct evidence.
                        self.relate(obj['id'], 'hasSection', sec['id'], 'AI_INFERRED', method='member_attribute', tag=tag)
        if room:
            if number and 'roomNumber' not in room:
                room['roomNumber'] = number
            if area and area > 0 and 'area' not in room:
                room['area'] = area
            self._space(obj, room, evidence, 'block_attribute')

    def _space(self, source, room, evidence, method):
        self.metrics['spaces'] += 1
        label = ' '.join(str(room[k]) for k in ('roomNumber', 'roomName') if k in room)
        aliases = ' '.join(str(value) for value in room.get('roomNameAliases', ()) if value)
        search_label = ' '.join(value for value in (label, aliases) if value)
        space = observation(self.doc, f"{evidence['layout']}:{evidence['handle']}:space", 'Space',
                            f"{self.name} {self.sheet.name} {search_label} {ALIASES['Space']}",
                            {**evidence, 'derived_from': source['id'], 'method': method}, source['bbox'],
                            state='AI_INFERRED',
                            properties={**room, 'source_annotation': source['id'], 'layer': source['properties'].get('layer', ''),
                                        'classification': {'label': 'Space', 'confidence': 0.8, 'method': f'room_vocabulary_{method}',
                                                           'evidence': [f'room name: {room["roomName"]}'],
                                                           'state': 'ACCEPT_WITH_WARNING'}})
        self.add(space, self.view)
        self.layout_objects.append(space)
        self.relate(space['id'], 'derivedFrom', source['id'], method=method)
        return space

    def _section(self, source, section, evidence, index, normalized):
        self.metrics['steel_sections'] += 1
        sec = observation(self.doc, f"{evidence['layout']}:{evidence['handle']}:section:{index}", 'SteelSection',
                          f"{self.name} {self.sheet.name} {section['sectionDesignation']} {ALIASES['SteelSection']}",
                          {**evidence, 'derived_from': source['id']}, source['bbox'], state='OBSERVED',
                          properties={**section, 'source_annotation': source['id'],
                                      'layer': source['properties'].get('layer', '')})
        self.add(sec, self.view)
        self.layout_objects.append(sec)
        self.relate(sec['id'], 'derivedFrom', source['id'], method='section_designation_text')
        if normalized is not None and source['bbox']:
            point = normalized.geometry.get('location') or [source['bbox']['min_x'], source['bbox']['min_y']]
            height = float(normalized.properties.get('height') or 0.0)
            self.sections.append((sec, source['bbox'], point, height))
        return sec

    def _detail_view(self, source, detail, evidence):
        self.metrics['detail_views'] += 1
        title = detail['detail_title']
        view = observation(self.doc, f"detail:{evidence['layout']}:{evidence['handle']}", 'View',
                           f"{self.name} {self.sheet.name} {title} {detail['drawing_category']}",
                           {**evidence, 'derived_from': source['id']}, source['bbox'], state='AI_INFERRED',
                           properties={'view_kind': 'detail', 'detail_title': title,
                                       'drawing_category': detail['drawing_category'], 'source_annotation': source['id'],
                                       'region': 'title_text_bbox', 'layout': self.sheet.name,
                                       'layer': source['properties'].get('layer', '')})
        self.add(view, self.view)
        self.relate(view['id'], 'derivedFrom', source['id'], method='detail_title_text')

    def end_layout(self):
        view, sheet = self.view, self.sheet
        title = self.title_blocks[0] if self.title_blocks else None
        fields = title['properties'] if title else {}
        texts = [text for _, text in sorted(self.title_texts, key=lambda item: -item[0])]
        candidates = [('title_block', fields.get('drawingTitle', ''))]
        if self.is_paper:
            candidates += [('drawing_title_text', texts[0] if texts else ''), ('layout_name', sheet.name), ('file_name', self.name)]
        else:
            candidates += [('file_name', self.name), ('drawing_title_text', texts[0] if texts else '')]
        view['properties'].update(drawing_category(*candidates))
        view['properties']['layout_kind'] = 'paper' if self.is_paper else 'model'
        if title:
            view['properties'].update({k: fields[k] for k in ('drawingNumber', 'drawingTitle', 'scale', 'revisionLabel', 'date')
                                       if k in fields})
            view['properties']['title_block'] = title['id']
            self.relate(view['id'], 'hasTitleBlock', title['id'], 'AI_INFERRED', method='title_block_attributes')
        view['search_text'] = f"{view['search_text']} {view['properties']['drawing_category']} {fields.get('drawingTitle', '')}"
        storey = next(filter(None, (storey_from_text(value) for _, value in candidates)), None)
        if storey and view['properties']['drawing_category'] == '평면도':
            view['properties']['storey'] = storey
        category = view['properties']['drawing_category']
        for obj in self.layout_objects:
            # Elements inherit their sheet's category; detail-view candidates keep their own.
            obj['properties'].setdefault('drawing_category', category)
            if storey and obj['type'] == 'Space':
                obj['properties'].setdefault('storey', storey)
        self._link_area_texts()
        self._link_sections()

    def _link_area_texts(self):
        """Give a room-name Space the area written as a separate text right next to it (nearest, mutual, within 3 heights)."""
        def loc(normalized):
            point = normalized.geometry.get('location') or [0.0, 0.0]
            return float(point[0]), float(point[1])
        rooms = [(s, n) for s, n in self.text_spaces if 'area' not in s['properties']]
        if not rooms or not self.area_texts:
            return
        def nearest(point, pool, key):
            best = min(pool, key=lambda item: math.dist(point, loc(key(item))))
            return best, math.dist(point, loc(key(best)))
        for space, normalized in rooms:
            limit = 3.0 * max(float(normalized.properties.get('height') or 0.0), 1e-6)
            (value, area_obj, area_norm), distance = nearest(loc(normalized), self.area_texts, lambda item: item[2])
            back, _ = nearest(loc(area_norm), self.text_spaces, lambda item: item[1])
            if distance <= limit and back[0] is space:
                space['properties']['area'] = value
                space['properties']['area_source'] = area_obj['id']
                self.relate(space['id'], 'derivedFrom', area_obj['id'], method='adjacent_area_text', distance=round(distance, 3))

    def _link_sections(self):
        """Relate a section text to a Beam/Column only when exactly one member's bbox (plus a margin) holds it."""
        members = [m for m in self.members if m['bbox'] and all(k in m['bbox'] for k in ('min_x', 'min_y', 'max_x', 'max_y'))]
        if not members or not self.sections:
            return
        spans = sorted(max(m['bbox']['max_x'] - m['bbox']['min_x'], m['bbox']['max_y'] - m['bbox']['min_y']) for m in members)
        cell = max(spans[len(spans) // 2], 4.0 * max(h for *_, h in self.sections), 1.0)
        grid = defaultdict(list)
        for i, member in enumerate(members):
            b = member['bbox']
            x0, x1 = int(math.floor(b['min_x'] / cell)), int(math.floor(b['max_x'] / cell))
            y0, y1 = int(math.floor(b['min_y'] / cell)), int(math.floor(b['max_y'] / cell))
            if (x1 - x0 + 1) * (y1 - y0 + 1) > 4096:
                continue  # pathological extents (e.g. a grid line across the site) are never direct evidence
            for gx in range(x0, x1 + 1):
                for gy in range(y0, y1 + 1):
                    grid[(gx, gy)].append(i)
        for sec, _, point, height in self.sections:
            margin = max(2.0 * height, 1e-6)
            x, y = float(point[0]), float(point[1])
            hits = set()
            for gx in range(int(math.floor((x - margin) / cell)), int(math.floor((x + margin) / cell)) + 1):
                for gy in range(int(math.floor((y - margin) / cell)), int(math.floor((y + margin) / cell)) + 1):
                    for i in grid.get((gx, gy), ()):
                        b = members[i]['bbox']
                        if b['min_x'] - margin <= x <= b['max_x'] + margin and b['min_y'] - margin <= y <= b['max_y'] + margin:
                            hits.add(i)
            if len(hits) == 1:
                member = members[hits.pop()]
                self.relate(member['id'], 'hasSection', sec['id'], 'AI_INFERRED', method='text_within_member_bbox',
                            margin=margin)
                sec['properties']['member'] = member['id']

    # ----------------------------------------------------------------- document
    def finish(self):
        for block_name, obj in self.blocks.items():
            obj['properties']['insert_count'] = self.insert_counts.get(block_name, 0)
        for obj in self.blocks.values():
            obj['properties']['effective_insert_count'] = obj['properties']['insert_count']
        for obj in self.blocks.values():
            effective = obj['properties']['effective_name']
            source = self.block_lookup.get(effective)
            if effective != obj['properties']['name'] and source is not None:
                source['properties']['effective_insert_count'] += obj['properties']['insert_count']
        layers = {decode_dxf_text(layer.dxf.name): layer for layer in self.document.layers}
        for layer_name in sorted(set(self.layer_counts) | set(self.block_layer_counts)):
            entry = layers.get(layer_name)
            properties = {'name': layer_name, 'entity_count': self.layer_counts.get(layer_name, 0),
                          'block_entity_count': self.block_layer_counts.get(layer_name, 0), 'declared': entry is not None}
            if entry is not None:
                try:
                    properties.update({'color': int(entry.dxf.get('color', 7)), 'linetype': str(entry.dxf.get('linetype', 'Continuous')),
                                       'lineweight': int(entry.dxf.get('lineweight', -3)), 'is_off': bool(entry.is_off()),
                                       'is_frozen': bool(entry.is_frozen()), 'is_locked': bool(entry.is_locked())})
                    if entry.dxf.hasattr('true_color'):
                        properties['true_color'] = int(entry.dxf.true_color)
                except Exception:
                    pass
            found = _match(layer_name.lower())
            properties['semantic_type'] = found[0] if found else ''
            obj = observation(self.doc, 'layer:' + layer_name, 'Layer',
                              f"{self.name} {layer_name} {properties['semantic_type']}", {**self.base, 'layer_name': layer_name},
                              properties=properties)
            self.add(obj)
        self.metrics['layers'] = len(set(self.layer_counts) | set(self.block_layer_counts))
        self.result['metrics'].update(self.metrics)


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
        from ..dwg import select_dwg_converter
        converter = select_dwg_converter(getattr(settings,'dwg_converter','auto'),
                                         settings.oda_executable or None,
                                         getattr(settings,'libredwg_executable','') or None)
        converted = converter.convert_to_dxf(source,output/'converted')
        (output/'conversion.json').write_text(json.dumps(converted.to_dict(),ensure_ascii=False),encoding='utf-8')
        if converted.status != 'SUCCESS':
            raise ValueError('; '.join(converted.errors))
        source = Path(converted.output)
        suffix = '.dxf'
    if suffix == '.dxf':
        from ezdxf import bbox as ezbbox, disassemble
        from ezdxf.addons.drawing import Frontend,RenderContext,layout,svg
        document, read_warnings = read_dxf(source)
        warnings.extend(read_warnings)
        result['units'] = INSUNITS.get(int(document.header.get('$INSUNITS',0) or 0), 'unknown')
        semantics = _DXFSemantics(doc, name, base, document, result, add, relations, warnings)
        semantics.catalog_blocks(ezbbox)
        for index, sheet in enumerate(document.layouts):
            view = observation(doc,'layout:'+sheet.name,'View',f'{name} {sheet.name}',{**base,'layout':sheet.name})
            add(view)
            result['metrics']['views'] += 1
            semantics.begin_layout(sheet, view)
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
                    semantics.count_entity(entity)
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
                        is_insert = normalized.entity_type == 'INSERT'
                        title_fields = title_block_fields(normalized.properties.get('attributes')) if is_insert else None
                        if title_fields:
                            kind = 'TitleBlock'
                            classification = Classification('TitleBlock', 0.9, 'attribute_rules',
                                ('attribute tags indicate title block: ' + ', '.join(sorted(title_fields)),
                                 f'block={normalized.properties.get("block_name")}'), 'ACCEPT_WITH_WARNING')
                        if kind == 'CADEntity' and not is_insert:
                            continue
                        props = normalized.properties
                        if is_insert:
                            attribute_text = ' '.join(str(v) for v in (props.get('attributes') or {}).values())
                            text = f"{props.get('effective_name') or props.get('block_name') or ''} {attribute_text}".strip()
                        else:
                            text = str(props.get('text') or props.get('block_name') or '')
                        evidence = {**base,'layout':sheet.name,'handle':handle,'coordinate_system':'CAD_WCS',
                                    'geometry_path':_safe_relative(output/f'geometry-{index}.jsonl', settings.data_root)}
                        state = 'OBSERVED' if kind in ('Annotation','Dimension','CADEntity') else 'AI_INFERRED'
                        obj = observation(doc,f'{sheet.name}:{handle}',kind,
                            f'{name} {sheet.name} {normalized.layer} {text} {ALIASES.get(kind,kind)}',evidence,
                            normalized.bbox,state=state,
                            properties={**props,'classification':classification.to_dict()})
                        if title_fields:
                            obj['properties'].update(title_fields)
                        add(obj,view)
                        semantics.enrich(obj, normalized, evidence)
                        if is_insert:
                            nested_count = 0
                            for nested_entity, nested_path in _iter_nested_insert_texts(entity):
                                try:
                                    nested = _normalize_entity(nested_entity)
                                    nested_bounds = ezbbox.extents([nested_entity], fast=False)
                                    if nested_bounds.has_data:
                                        nested.bbox = dict(zip(
                                            ('min_x','min_y','min_z','max_x','max_y','max_z'),
                                            [*nested_bounds.extmin,*nested_bounds.extmax],
                                        ))
                                    nested_kind, nested_classification = classify(nested)
                                    if nested_kind == 'CADEntity':
                                        nested_kind = 'Annotation'
                                    nested_text = str(nested.properties.get('text') or '').strip()
                                    if not nested_text:
                                        continue
                                    nested_handle = f"{handle}:nested:{'.'.join(map(str, nested_path))}"
                                    nested_evidence = {
                                        **base,
                                        'layout': sheet.name,
                                        'handle': nested_handle,
                                        'source_handle': handle,
                                        'coordinate_system': 'CAD_WCS',
                                        'geometry_path': _safe_relative(output/f'geometry-{index}.jsonl', settings.data_root),
                                        'nested_path': list(nested_path),
                                        'method': 'recursive_insert_virtual_entities',
                                    }
                                    nested_obj = observation(
                                        doc,
                                        f'{sheet.name}:{nested_handle}',
                                        nested_kind,
                                        f'{name} {sheet.name} {nested.layer} {nested_text} {ALIASES.get(nested_kind,nested_kind)}',
                                        nested_evidence,
                                        nested.bbox,
                                        state='OBSERVED' if nested_kind in ('Annotation','Dimension','CADEntity') else 'AI_INFERRED',
                                        properties={**nested.properties,'classification':nested_classification.to_dict()},
                                    )
                                    add(nested_obj, view)
                                    semantics.enrich(nested_obj, nested, nested_evidence)
                                    semantics.relate(
                                        nested_obj['id'], 'derivedFrom', obj['id'], 'OBSERVED',
                                        method='nested_block_text', source_handle=handle,
                                    )
                                    nested_count += 1
                                except Exception as exc:
                                    warnings.append(f'{sheet.name}/{handle}/nested:{nested_path}: {exc}')
                            semantics.metrics['nested_text_entities'] += nested_count
                    except Exception as exc:
                        warnings.append(f'{sheet.name}/{handle}: {exc}')
            semantics.end_layout()
        semantics.finish()
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
        from .pdf_drawings import analyse_page, page_objects
        for key in ('titleblocks', 'dimensions', 'grids', 'walls', 'textless_pages'):
            result['metrics'].setdefault('pdf_' + key, 0)
        with fitz.open(source) as pdf:
            for i,page in enumerate(pdf):
                page_obj = observation(doc,f'page:{i+1}','Page',f'{name} {i+1}페이지',
                    {**base,'page':i+1,'coordinate_system':'PDF_POINTS','page_size':[page.rect.width,page.rect.height],
                     'rotation':page.rotation})
                add(page_obj)
                result['metrics']['pages'] += 1
                blocks = page.get_text('blocks')
                for j,block in enumerate(blocks):
                    x,y,X,Y,text,*_ = block
                    if not str(text).strip(): continue
                    add(observation(doc,f'page:{i+1}:block:{j}','Annotation',str(text),
                        {**page_obj['evidence'],'bbox':[x,y,X,Y]},dict(min_x=x,min_y=y,max_x=X,max_y=Y)),page_obj)
                drawings = page.get_drawings()
                vectors_path = output/f'page-{i+1}-vectors.json'
                vectors_path.write_text(json.dumps(drawings,default=str),encoding='utf-8')
                try:
                    analysis = analyse_page(page, drawings)
                except Exception as exc:
                    warnings.append(f'Page {i+1}: vector analysis failed: {exc}')
                    analysis = {'title_block':None,'dimensions':[],'grids':[],'walls':[]}
                def make(key, kind, text, bbox, state, props, extra, _page=page_obj, _i=i):
                    return observation(doc,f'page:{_i+1}:{key}',kind,f'{text} {ALIASES.get(kind,kind)}',
                        {**_page['evidence'],'bbox':[bbox['min_x'],bbox['min_y'],bbox['max_x'],bbox['max_y']],
                         'vectors_path':_safe_relative(vectors_path, settings.data_root),**extra},
                        bbox,state=state,properties=props)
                title = None
                for obj in page_objects(analysis, i+1, name, make, warnings):
                    add(obj, page_obj)
                    metric = {'TitleBlock':'titleblocks','Dimension':'dimensions','Grid':'grids','Wall':'walls'}[obj['type']]
                    result['metrics']['pdf_'+metric] += 1
                    if obj['type'] == 'TitleBlock':
                        title = obj
                fields = title['properties'] if title else {}
                page_obj['properties'].update(drawing_category(('title_block', fields.get('drawingTitle', '')),
                                                               ('file_name', name)))
                if title:
                    page_obj['properties'].update({k: fields[k] for k in ('drawingNumber','drawingTitle','scale','revisionLabel','date')
                                                   if k in fields})
                    page_obj['properties']['title_block'] = title['id']
                    page_obj['search_text'] += f" {fields.get('drawingNumber','')} {fields.get('drawingTitle','')}"
                    relations.append(relation(page_obj['id'],'hasTitleBlock',title['id'],'AI_INFERRED',
                                              source_hash=base['source_hash'],method='pdf_label_value'))
                if not any(str(b[4]).strip() for b in blocks):
                    result['metrics']['pdf_textless_pages'] += 1
                    image = output/f'page-{i+1}.png'
                    page.get_pixmap(matrix=fitz.Matrix(2,2)).save(image)
                    try:
                        ocr_items = ocr(image,doc,f'page:{i+1}',{**page_obj['evidence'],'ocr_scale':2})
                    except RuntimeError as exc:
                        if 'OCR_REQUIRED' not in str(exc): raise
                        page_obj['properties']['ocr_required'] = True
                        warnings.append(f'OCR_REQUIRED: page {i+1} has no text layer; indexed page and vectors only '
                                        '(run the ocr-worker profile with PaddleOCR for text).')
                        ocr_items = []
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
