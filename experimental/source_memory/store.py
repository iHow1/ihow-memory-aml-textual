"""Durable raw messages, verbatim span annotations, and non-destructive links.

Inspired by AMI's raw/fact dual retrieval. Independently implemented. Annotation
semantics remain model judgments; exact spans and IDs prove provenance, not truth.
"""
import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class Conflict(ValueError):pass
class Incomplete(RuntimeError):pass


SCHEMA='''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(owner TEXT,request TEXT,hash TEXT,payload TEXT,state TEXT,
 PRIMARY KEY(owner,request));
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,owner TEXT,session TEXT,request TEXT,
 ordinal INTEGER,role TEXT,body TEXT,stamp INTEGER,hash TEXT);
CREATE INDEX IF NOT EXISTS messages_owner ON messages(owner);
CREATE TABLE IF NOT EXISTS units(id TEXT PRIMARY KEY,owner TEXT,parent TEXT,kind TEXT,
 body TEXT,start INTEGER,end INTEGER,label TEXT,vector TEXT,
 FOREIGN KEY(parent) REFERENCES messages(id));
CREATE INDEX IF NOT EXISTS units_owner ON units(owner);
CREATE TABLE IF NOT EXISTS links(source TEXT,target TEXT,relation TEXT,
 PRIMARY KEY(source,target,relation),FOREIGN KEY(source) REFERENCES units(id),
 FOREIGN KEY(target) REFERENCES units(id));
'''


class Memory:
    # reader_tokens=None: no local evidence cap; the platform Answer budget applies.
    # Pass 6000 to reproduce the pre-2026-10-01 local experiments.
    def __init__(self,path,annotator,embedder,encoding,reader_tokens=None):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.annotator=annotator;self.embedder=embedder;self.encoding=encoding
        self.reader_tokens=reader_tokens;self.lock=threading.RLock()
        from .model import EXTRACT,PLAN
        # embedder=None is the model-free lexical mode: no vectors, BM25 ranking only.
        identity=digest({'schema':1,'annotator':annotator.identity,'embedder':None if embedder is None else embedder.identity,
                         'prompts':[EXTRACT,PLAN],'reader_tokens':reader_tokens})
        with self.connect() as db:
            db.executescript(SCHEMA)
            old=db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if old and old[0]!=identity:raise ValueError('database configuration mismatch')
            db.execute("INSERT OR IGNORE INTO meta VALUES ('identity',?)",(identity,))

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def validate(payload):
        if not isinstance(payload,dict) or set(payload)!={'user_id','session_id','request_id','messages'}:
            raise ValueError('invalid Add fields')
        for k in ('user_id','session_id','request_id'):
            if not isinstance(payload[k],str) or not 0<len(payload[k])<=512:raise ValueError('invalid identity')
        messages=payload['messages']
        if not isinstance(messages,list) or not 1<=len(messages)<=20:raise ValueError('invalid messages')
        words=0
        for m in messages:
            if not isinstance(m,dict) or not {'role','content'}<=set(m) or set(m)-{'role','content','timestamp'}:
                raise ValueError('invalid message fields')
            if m['role'] not in ('user','assistant','system') or not isinstance(m['content'],str) or not m['content'].strip():
                raise ValueError('invalid message')
            words+=len(m['content'].split())
            if m.get('timestamp') is not None:
                if type(m['timestamp']) is not int:raise ValueError('timestamp must be Unix milliseconds')
                try:datetime.fromtimestamp(m['timestamp']/1000,timezone.utc)
                except (ValueError,OverflowError,OSError):raise ValueError('invalid timestamp') from None
        if words>2000:raise ValueError('Add word limit exceeded')

    def vectors(self,texts,query=False):
        if self.embedder is None:return [None]*len(texts)
        vectors=self.embedder.encode(texts,query=query)
        if len(vectors)!=len(texts):raise ValueError('embedding count mismatch')
        result=[]
        for v in vectors:
            if not v or any(type(x) not in (float,int) or not math.isfinite(x) for x in v):
                raise ValueError('invalid embedding')
            norm=math.sqrt(sum(x*x for x in v))
            if norm<=0:raise ValueError('zero embedding')
            result.append([x/norm for x in v])
        if len({len(v) for v in result})>1:raise ValueError('embedding dimensions differ')
        return result

    def add(self,payload):
        self.validate(payload);owner=payload['user_id'];request=payload['request_id'];h=digest(payload)
        result={k:payload[k] for k in ('user_id','session_id','request_id')};result['success']=True
        # First candidate serializes writes in one process. SQLite claims also
        # prevent a second process from reissuing the same uncertain model call.
        with self.lock:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                prior_request=db.execute('SELECT hash,state FROM requests WHERE owner=? AND request=?',(owner,request)).fetchone()
                if prior_request:
                    if prior_request['hash']!=h:raise Conflict('request payload changed')
                    if prior_request['state']!='complete':raise Incomplete('request requires explicit recovery; no automatic model retry')
                    return result
                db.execute('INSERT INTO requests VALUES (?,?,?,?,?)',(owner,request,h,json.dumps(payload,ensure_ascii=False),'pending'))
                prior=[dict(r) for r in db.execute("SELECT id,body,kind FROM units WHERE owner=? AND kind!='raw' ORDER BY rowid DESC LIMIT 12",(owner,))]
            try:
                annotations=self.annotator.extract(payload['messages'],prior)
                if not isinstance(annotations,dict) or set(annotations)!={'atoms'} or not isinstance(annotations['atoms'],list) or len(annotations['atoms'])>12:
                    raise ValueError('invalid annotations')
                records=[];units=[];edges=[];prior_ids={r['id'] for r in prior}
                for n,m in enumerate(payload['messages']):
                    mid='m_'+digest([owner,request,n])
                    records.append((mid,owner,payload['session_id'],request,n,m['role'],m['content'],m.get('timestamp'),digest(m['content'])))
                    units.append({'id':'r_'+mid,'owner':owner,'parent':mid,'kind':'raw','body':m['content'],
                                  'start':0,'end':len(m['content']),'label':''})
                used=set()
                for n,a in enumerate(annotations['atoms']):
                    if not isinstance(a,dict) or set(a)!={'message','quote','kind','key','links'}:raise ValueError('invalid atom fields')
                    i=a['message'];q=a['quote']
                    if type(i) is not int or not 0<=i<len(records) or not isinstance(q,str) or not q.strip():raise ValueError('invalid source pointer')
                    body=payload['messages'][i]['content'];start=body.find(q)
                    if start<0 or body.find(q,start+1)>=0:raise ValueError('quote must match exactly once')
                    if (i,start,len(q)) in used:raise ValueError('duplicate annotation')
                    used.add((i,start,len(q)))
                    if a['kind'] not in ('fact','preference','recommendation','event','correction'):raise ValueError('invalid atom kind')
                    if not isinstance(a['key'],str) or not 0<len(a['key'])<=200:raise ValueError('invalid index label')
                    if not isinstance(a['links'],list) or len(a['links'])>4:raise ValueError('invalid links')
                    uid='a_'+digest([owner,request,n])
                    units.append({'id':uid,'owner':owner,'parent':records[i][0],'kind':a['kind'],'body':q,
                                  'start':start,'end':start+len(q),'label':a['key']})
                    for link in a['links']:
                        if not isinstance(link,dict) or set(link)!={'target','relation'} or link['target'] not in prior_ids or link['relation'] not in ('updates','corrects','supports'):
                            raise ValueError('link not grounded in supplied owner history')
                        edges.append((uid,link['target'],link['relation']))
                vectors=self.vectors([u['label']+'\n'+u['body'] if u['label'] else u['body'] for u in units])
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if self.embedder is not None:
                        dimension=db.execute("SELECT value FROM meta WHERE key='dimension'").fetchone()
                        if dimension and int(dimension[0])!=len(vectors[0]):raise ValueError('embedding dimension changed')
                        db.execute("INSERT OR IGNORE INTO meta VALUES ('dimension',?)",(str(len(vectors[0])),))
                    db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)',records)
                    for u,v in zip(units,vectors):
                        db.execute('INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?)',tuple(u[k] for k in ('id','owner','parent','kind','body','start','end','label'))+(None if v is None else json.dumps(v),))
                    db.executemany('INSERT INTO links VALUES (?,?,?)',edges)
                    db.execute("UPDATE requests SET state='complete' WHERE owner=? AND request=?",(owner,request))
            except Exception:
                with self.connect() as db:db.execute("UPDATE requests SET state='failed' WHERE owner=? AND request=?",(owner,request))
                raise Incomplete('Add incomplete; source retained in request journal, nothing published') from None
        return result

    @staticmethod
    def render(message):
        date='unknown' if message['stamp'] is None else datetime.fromtimestamp(message['stamp']/1000,timezone.utc).isoformat().replace('+00:00','Z')
        return f'[{message["id"]} | {message["role"]} | said_at={date}]\n{message["body"]}'

    def search(self,payload):
        if not isinstance(payload,dict) or not {'user_id','query','top_k'}<=set(payload) or set(payload)-{'user_id','query','top_k','options'}:raise ValueError('invalid Search fields')
        owner=payload['user_id'];query=payload['query'];top=payload['top_k'];options=payload.get('options',[])
        if not isinstance(owner,str) or not owner or not isinstance(query,str) or not query.strip() or len(query)>20000 or type(top) is not int or not 1<=top<=100:raise ValueError('invalid Search')
        if not isinstance(options,list) or any(not isinstance(x,str) for x in options) or len(json.dumps(options))>20000:raise ValueError('invalid options')
        with self.connect() as db:
            db.execute('BEGIN')
            units=[dict(r) for r in db.execute('SELECT * FROM units WHERE owner=? ORDER BY rowid',(owner,))]
            if not units:return {'data':[]}
            messages={r['id']:dict(r) for r in db.execute('SELECT rowid AS seq,* FROM messages WHERE owner=? ORDER BY rowid',(owner,))}
            for u in units:
                m=messages[u['parent']]
                if digest(m['body'])!=m['hash'] or m['body'][u['start']:u['end']]!=u['body']:raise Incomplete('stored source binding mismatch')
            extra=self.annotator.plan(query,options)
            if not isinstance(extra,list) or len(extra)>3 or any(not isinstance(q,str) or not q.strip() or len(q)>500 for q in extra):raise ValueError('invalid query plan')
            queries=list(dict.fromkeys([query]+extra))
            vectors=self.vectors(queries,query=True);scores={u['id']:0.0 for u in units}
            # All lexical statistics are owner-local; another benchmark user's
            # data must not influence ranking through global FTS IDF values.
            counts={u['id']:Counter(re.findall(r'\w+',(u['label']+' '+u['body']).casefold())) for u in units}
            average=sum(sum(c.values()) for c in counts.values())/len(counts) or 1
            for q,v in zip(queries,vectors):
                dense=[]
                for u in units if v is not None else ():
                    stored=json.loads(u['vector'])
                    if len(stored)!=len(v):raise Incomplete('embedding dimension mismatch')
                    dense.append((sum(a*b for a,b in zip(stored,v)),u['id']))
                for rank,(_,uid) in enumerate(sorted(dense,key=lambda x:(-x[0],x[1]))[:100]):scores[uid]+=1/(60+rank+1)
                terms=list(dict.fromkeys(re.findall(r'\w+',q.casefold())))[:32]
                if terms:
                    idf={t:math.log(1+(len(counts)-sum(t in c for c in counts.values())+.5)/(sum(t in c for c in counts.values())+.5)) for t in terms}
                    lexical=[]
                    for uid,c in counts.items():
                        length=sum(c.values());value=sum(idf[t]*c[t]*2.5/(c[t]+1.5*(.25+.75*length/average)) for t in terms if c[t])
                        if value>0:lexical.append((value,uid))
                    for rank,(_,uid) in enumerate(sorted(lexical,key=lambda x:(-x[0],x[1]))[:100]):scores[uid]+=.5/(60+rank+1)
            # Link graph is undirected for evidence coverage, not truth/state.
            byid={u['id']:u for u in units};adj={mid:set() for mid in messages}
            for e in db.execute('SELECT source,target FROM links WHERE source IN (SELECT id FROM units WHERE owner=?)',(owner,)):
                if e['source'] not in byid or e['target'] not in byid:raise Incomplete('cross-owner graph corruption')
                a,b=byid[e['source']]['parent'],byid[e['target']]['parent'];adj[a].add(b);adj[b].add(a)
            sessions={}
            for m in messages.values():sessions.setdefault(m['session'],[]).append(m['id'])
            neighbors={}
            for seq in sessions.values():
                for n,mid in enumerate(seq):neighbors[mid]=seq[max(0,n-1):n+2]
            parent_scores={}
            for uid,score in scores.items():
                mid=byid[uid]['parent'];parent_scores[mid]=max(parent_scores.get(mid,0),score)
            emitted=set();data=[]
            for mid in sorted(parent_scores,key=lambda x:(-parent_scores[x],messages[x]['seq'])):
                if mid in emitted:continue
                component={mid};pending=[mid]
                while pending:
                    for other in adj[pending.pop()]-component:component.add(other);pending.append(other)
                # Keep the linked set and one adjacent message on each side.
                group=set(component)
                for member in component:group.update(neighbors[member])
                ordered=sorted(group,key=lambda x:messages[x]['seq'])
                content='\n\n'.join(self.render(messages[x]) for x in ordered)
                if self.reader_tokens is not None:
                    trial='\n\n'.join([r['content'] for r in data]+[content])
                    if len(self.encoding.encode_ordinary(trial))>self.reader_tokens:continue
                data.append({'id':'g_'+digest(ordered),'content':content,'score':parent_scores[mid],
                             'created_at':None if messages[mid]['stamp'] is None else datetime.fromtimestamp(messages[mid]['stamp']/1000,timezone.utc).isoformat().replace('+00:00','Z')})
                emitted.update(group)
                if len(data)==top:break
            return {'data':data}

    def health(self):
        with self.connect() as db:
            return {'status':'ok','mode':'source-memory-experimental-v1','annotator':self.annotator.identity,
                    'embedding':None if self.embedder is None else self.embedder.identity,'messages':db.execute('SELECT count(*) FROM messages').fetchone()[0],
                    'atoms':db.execute("SELECT count(*) FROM units WHERE kind!='raw'").fetchone()[0],
                    'links':db.execute('SELECT count(*) FROM links').fetchone()[0],
                    'incomplete_requests':db.execute("SELECT count(*) FROM requests WHERE state!='complete'").fetchone()[0]}
