from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


MIGRATIONS_DIR = Path(__file__).with_name('migrations')
CYPHER_TAG = '$aec_cypher$'


def graph_name(project: str) -> str:
    return "aec_" + hashlib.sha256(project.encode()).hexdigest()[:24]


class Database:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def connect(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            conn.execute("LOAD 'age'")
            conn.execute('SET search_path = ag_catalog, aec, public')
            conn.execute("SET statement_timeout = '30s'")
            yield conn

    def initialize(self):
        """Apply pending numbered migrations in order; each runs once, in its own transaction."""
        applied = []
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute('CREATE SCHEMA IF NOT EXISTS aec')
            conn.execute('''CREATE TABLE IF NOT EXISTS aec.schema_migrations (
                version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())''')
            done = {row[0] for row in conn.execute('SELECT version FROM aec.schema_migrations')}
            for path in sorted(MIGRATIONS_DIR.glob('[0-9][0-9][0-9][0-9]_*.sql')):
                if path.stem in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding='utf-8-sig'))
                    conn.execute('INSERT INTO aec.schema_migrations(version) VALUES(%s)', (path.stem,))
                applied.append(path.stem)
        return applied

    def enqueue(self, payload, dedup_key):
        with self.connect() as conn:
            row = conn.execute("""INSERT INTO aec.jobs(id,dedup_key,payload) VALUES(%s,%s,%s)
                ON CONFLICT(dedup_key) DO UPDATE SET dedup_key=EXCLUDED.dedup_key RETURNING *""",
                (uuid.uuid4(), dedup_key, Jsonb(payload))).fetchone()
        return row

    def claim(self, owner, lease_seconds=300, max_attempts=3, queue='cad'):
        with self.connect() as conn:
            conn.execute("""UPDATE aec.jobs SET state='FAILED',error='Lease expired; retry limit reached',lease_owner=NULL
                WHERE state='RUNNING' AND lease_until<now() AND attempts>=%s""", (max_attempts,))
            return conn.execute("""WITH candidate AS (
                SELECT id FROM aec.jobs WHERE (state='QUEUED' OR (state='RUNNING' AND lease_until<now()))
                AND attempts<%s AND COALESCE(payload->>'queue','cad')=%s
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE aec.jobs j SET state='RUNNING',attempts=attempts+1,lease_owner=%s,
                lease_until=now()+make_interval(secs=>%s),updated_at=now()
                FROM candidate c WHERE j.id=c.id RETURNING j.*""", (max_attempts,queue,owner,lease_seconds)).fetchone()

    def heartbeat(self, job_id, owner, lease_seconds=300):
        with self.connect() as conn:
            return conn.execute("""UPDATE aec.jobs SET lease_until=now()+make_interval(secs=>%s),updated_at=now()
                WHERE id=%s AND lease_owner=%s AND state='RUNNING' AND lease_until>now()""",
                (lease_seconds,job_id,owner)).rowcount == 1

    def finish(self, job_id, owner, result=None, error=None):
        with self.connect() as conn:
            return conn.execute("""UPDATE aec.jobs SET state=%s,result=%s,error=%s,progress=%s,
                stage=%s,lease_owner=NULL,lease_until=NULL,updated_at=now()
                WHERE id=%s AND lease_owner=%s AND state='RUNNING' AND lease_until>now()""",
                ('FAILED' if error else 'SUCCEEDED',Jsonb(result),error,0 if error else 100,
                 'failed' if error else 'complete',job_id,owner)).rowcount == 1

    def cypher(self, conn, graph, query):
        # AGE only accepts the Cypher text as a dollar-quoted constant. The graph name stays a SQL
        # literal; the query is wrapped in a fixed tag that it may not contain, so it cannot escape.
        if CYPHER_TAG in query:
            raise ValueError('Cypher text contains the reserved dollar-quote tag')
        return conn.execute(sql.SQL('SELECT * FROM cypher({}, {}) AS (value agtype)').format(
            sql.Literal(graph),sql.SQL(CYPHER_TAG + query + CYPHER_TAG))).fetchall()

    def project_graph(self, conn, snapshot):
        graph = graph_name(snapshot['project_id'])
        # Workers ingesting the same new project would otherwise race on create_graph and on the
        # implicit Entity/Rel label creation; serialise per graph until this transaction ends.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('aec_graph:' || %s))",(graph,))
        if not conn.execute('SELECT 1 FROM ag_catalog.ag_graph WHERE name=%s',(graph,)).fetchone():
            conn.execute('SELECT create_graph(%s)',(graph,))
        doc = json.dumps(snapshot['document_id'])
        self.cypher(conn,graph,f'MATCH (n:Entity) WHERE n.document_id={doc} DETACH DELETE n RETURN count(n)')
        for obj in snapshot['objects']:
            props = ', '.join(f'{k}: {json.dumps(v,ensure_ascii=False)}' for k,v in {
                'id':obj['id'],'document_id':snapshot['document_id'],'kind':obj['type'],
                'revision':snapshot['revision'],'state':obj['state']}.items())
            self.cypher(conn,graph,f'CREATE (n:Entity {{{props}}}) RETURN n.id')
        for rel in snapshot['relations']:
            # Candidate links remain in SQL evidence, not the authoritative graph.
            if rel['state'] not in ('OBSERVED','USER_CONFIRMED','CALCULATED'):
                continue
            a,b,kind = (json.dumps(rel[k]) for k in ('subject','object','predicate'))
            self.cypher(conn,graph,f'MATCH (a:Entity),(b:Entity) WHERE a.id={a} AND b.id={b} '
                        f'CREATE (a)-[r:Rel {{kind:{kind}}}]->(b) RETURN r.kind')
        conn.execute('UPDATE aec.index_state SET graph_revision=%s WHERE document_id=%s',
                     (snapshot['revision'],snapshot['document_id']))

    def project(self, conn, snapshot, relative_path):
        doc, rev = snapshot['document_id'], snapshot['revision']
        conn.execute("""INSERT INTO aec.documents(id,project_id,source_key,name,revision,source_hash,snapshot_path)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET revision=EXCLUDED.revision,
            source_hash=EXCLUDED.source_hash,snapshot_path=EXCLUDED.snapshot_path,updated_at=now()""",
            (doc,snapshot['project_id'],snapshot['source_key'],snapshot['name'],rev,snapshot['source_hash'],relative_path))
        conn.execute('INSERT INTO aec.snapshots VALUES(%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING',
                     (doc,rev,relative_path,snapshot['source_hash']))
        conn.execute('DELETE FROM aec.objects WHERE document_id=%s',(doc,))
        conn.execute('DELETE FROM aec.relations WHERE document_id=%s',(doc,))
        for obj in snapshot['objects']:
            bbox = obj.get('bbox') or {}
            bounds = None
            if all(k in bbox for k in ('min_x','min_y','max_x','max_y')):
                x,y,X,Y = (bbox[k] for k in ('min_x','min_y','max_x','max_y'))
                bounds = f'POLYGON(({x} {y},{X} {y},{X} {Y},{x} {Y},{x} {y}))'
            conn.execute("""INSERT INTO aec.objects(id,project_id,document_id,revision,kind,discipline,storey,
                label,search_text,payload,bounds,units) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,ST_GeomFromText(%s,0),%s)""",
                (obj['id'],snapshot['project_id'],doc,rev,obj['type'],snapshot.get('discipline',''),
                 obj.get('storey',''),obj['label'],obj['search_text'],Jsonb(obj),bounds,snapshot.get('units','unknown')))
        for rel in snapshot['relations']:
            conn.execute('INSERT INTO aec.relations VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (rel['id'],snapshot['project_id'],doc,rev,rel['subject'],rel['predicate'],rel['object'],rel['state'],Jsonb(rel)))
        conn.execute("""INSERT INTO aec.index_state(document_id,sql_revision) VALUES(%s,%s)
            ON CONFLICT(document_id) DO UPDATE SET sql_revision=EXCLUDED.sql_revision,embedding_revision=0,rag_revision=0,error=NULL""", (doc,rev))
        self.project_graph(conn,snapshot)
